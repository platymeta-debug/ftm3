# 파일명: execution/trading_engine.py (V4.2 — 단일 실행정책 + JSON 기반 설정)

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException

# 프로젝트 컴포넌트
from core.config_manager import config        # ▶ 옵티마이저가 만든 JSON을 여기서 로드
from core.event_bus import event_bus
from database.manager import db_manager
from database.models import Signal, Trade

# -----------------------------------------------------------------------------
# 실행 정책 설정 (모든 값은 config에서만 읽는다 — ENV 사용 안 함)
# -----------------------------------------------------------------------------

@dataclass
class ExecPolicy:
    """실행 레벨 설정(실거래/백테스트 공용) — 전부 config에서 공급"""
    sl_atr_mult: float = 1.5
    rr: float = 2.0
    partial: List[float] = None            # 예: [0.3, 0.3, 0.4] (없으면 단일 TP)
    time_stop_bars: int = 0               # 0이면 비활성
    trailing_mode: str = "off"            # "off" | "atr" | "percent"
    trailing_k: float = 0.0               # atr 배수 또는 percent 값

    @staticmethod
    def from_config(symbol: str) -> "ExecPolicy":
        """
        config에서 해당 심볼의 실행정책을 불러온다.
        - 기대 키: sl_atr_multiplier, risk_reward_ratio, exec_partial, exec_time_stop_bars,
                  exec_trailing_mode, exec_trailing_k
        - 모두 없으면 안전한 기본값 사용
        """
        p = config.get_exec_policy(symbol) if hasattr(config, "get_exec_policy") else {}
        # 과거 설정 키와 호환
        sl_mult = p.get("sl_atr_multiplier", getattr(config, "sl_atr_multiplier", 1.5))
        rr = p.get("risk_reward_ratio", getattr(config, "risk_reward_ratio", 2.0))
        partial = p.get("exec_partial", [])
        if isinstance(partial, str):
            partial = [float(x) for x in partial.split(",") if x.strip()]
        time_stop = int(p.get("exec_time_stop_bars", 0))
        trailing_mode = p.get("exec_trailing_mode", "off")
        trailing_k = float(p.get("exec_trailing_k", 0.0))
        return ExecPolicy(
            sl_atr_mult=float(sl_mult),
            rr=float(rr),
            partial=partial or [],
            time_stop_bars=time_stop,
            trailing_mode=trailing_mode,
            trailing_k=trailing_k,
        )

# -----------------------------------------------------------------------------
# TradingEngine — 단일 소스: 멀티 TP, 트레일링, 타임스탑, 브래킷 동기화
# -----------------------------------------------------------------------------

class TradingEngine:
    """
    - 진입은 시장가(or 지정가) 체결 → SL/TP(브래킷) 자동 부착
    - 부분익절: TAKE_PROFIT_MARKET 여러 장으로 분할, 모두 reduceOnly
    - 트레일링: SL 주문을 '취소 후 재발행' 방식으로 끌어올림/내림
    - 타임스탑: 보유 봉수 k 이상이면 전량 마켓 청산
    - 모든 수치/토글은 config(JSON)에서 공급 (ENV 미사용)
    """

    def __init__(self, client: Client):
        self.client = client
        self._live_brackets: Dict[str, Dict] = {}  # symbol -> {sl_id, tp_ids[], entry_price, side, bars_held}
        print("🚚 [V4.2] 트레이딩 엔진 초기화(멀티TP/트레일/타임스탑 통합, JSON 설정).")

    # -------------------------------------------------------------------------
    # 외부에서 호출: 신규 진입 + 브래킷 자동 부착
    # -------------------------------------------------------------------------
    def open_with_bracket(
        self,
        symbol: str,
        side: str,                 # "BUY" | "SELL"
        quantity: float,
        entry_atr: float,
        entry_type: str = "MARKET",  # "MARKET"|"LIMIT"
        entry_price: Optional[float] = None,
        client_order_id_prefix: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        1) 진입 주문
        2) 체결 기준가(entry_price) 확보
        3) SL/TP 브래킷 부착(부분익절 가능)
        4) DB 기록/이벤트 발행
        """
        policy = ExecPolicy.from_config(symbol)
        close_side = "BUY" if side == "SELL" else "SELL"

        # --- 1) 진입 주문 ----------------------------------------------------------------
        try:
            if entry_type == "LIMIT":
                assert entry_price is not None and entry_price > 0
                entry = self.client.futures_create_order(
                    symbol=symbol, side=side, type="LIMIT",
                    price=round(float(entry_price), 4),
                    quantity=quantity, timeInForce="GTC",
                    newClientOrderId=self._cid(symbol, client_order_id_prefix, "ENTRYL")
                )
            else:
                entry = self.client.futures_create_order(
                    symbol=symbol, side=side, type="MARKET",
                    quantity=quantity,
                    newClientOrderId=self._cid(symbol, client_order_id_prefix, "ENTRYM")
                )
            print(f"➡️  {symbol} {side} {quantity} 진입 전송 OK")
        except BinanceAPIException as e:
            print(f"🚨 진입 주문 실패: {symbol} {side} x{quantity} — {e}")
            return None

        # --- 2) 체결가 산정 --------------------------------------------------------------
        try:
            # 평균 체결가를 응답에서 우선 읽고, 없으면 최근 가격 조회
            avg_px = float(entry.get("avgPrice") or 0) or self._fetch_last_price(symbol)
            entry_px = round(avg_px, 4)
        except Exception:
            entry_px = round(self._fetch_last_price(symbol), 4)

        # --- 3) SL/TP 계산 및 주문 전송 ---------------------------------------------------
        sl_d = float(entry_atr) * policy.sl_atr_mult
        if sl_d <= 0:
            print("⚠️ ATR 기반 SL 거리가 0 이하 — 브래킷 생략")
            return entry

        if side == "BUY":
            sl_px = round(entry_px - sl_d, 4)
            tp_base = round(entry_px + (sl_d * policy.rr), 4)
        else:
            sl_px = round(entry_px + sl_d, 4)
            tp_base = round(entry_px - (sl_d * policy.rr), 4)

        # 3-1) SL(손절): reduceOnly + 수량 지정
        try:
            sl_order = self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=sl_px,
                reduceOnly=True,
                quantity=quantity,
                newClientOrderId=self._cid(symbol, client_order_id_prefix, "SL")
            )
        except BinanceAPIException as e:
            print(f"🚨 SL 주문 실패: {e}")
            sl_order = {}

        # 3-2) TP(익절): 단일 또는 멀티
        tp_ids: List[str] = []
        try:
            if not policy.partial:
                tp_single = self.client.futures_create_order(
                    symbol=symbol,
                    side=close_side,
                    type="TAKE_PROFIT_MARKET",
                    stopPrice=tp_base,
                    reduceOnly=True,
                    quantity=quantity,
                    newClientOrderId=self._cid(symbol, client_order_id_prefix, "TP")
                )
                if "orderId" in tp_single:
                    tp_ids.append(str(tp_single["orderId"]))
            else:
                # 멀티 TP: 0.5R/1.0R/1.5R 스텝 — 비중은 policy.partial에 따름
                steps = [0.5, 1.0, 1.5] if len(policy.partial) == 3 else [1.0] * len(policy.partial)
                remain = quantity
                for w, m in zip(policy.partial, steps):
                    sub_qty = round(quantity * float(w), 6)
                    remain -= sub_qty
                    sub_tp = self._scale_tp(entry_px, tp_base, side, mult=m)
                    tp = self.client.futures_create_order(
                        symbol=symbol,
                        side=close_side,
                        type="TAKE_PROFIT_MARKET",
                        stopPrice=sub_tp,
                        reduceOnly=True,
                        quantity=sub_qty,
                        newClientOrderId=self._cid(symbol, client_order_id_prefix, f"TP{m}")
                    )
                    if "orderId" in tp:
                        tp_ids.append(str(tp["orderId"]))
                # 남은 잔량 보정이 필요하면 마지막 TP에 합쳐도 됨(상황에 따라)
        except BinanceAPIException as e:
            print(f"🚨 TP 주문 실패: {e}")

        # 상태 저장 (트레일/타임스탑/정리용)
        self._live_brackets[symbol] = {
            "sl_id": sl_order.get("orderId"),
            "tp_ids": tp_ids,
            "entry_price": entry_px,
            "side": side,
            "bars_held": 0,
            "quantity": quantity,
        }

        # --- 4) DB / 이벤트 --------------------------------------------------------------
        self._record_trade_open(symbol, side, entry_px, quantity, extra)
        event_bus.safe_publish("ORDER_OPEN_SUCCESS", {
            "symbol": symbol, "side": side, "qty": quantity, "entry": entry_px,
            "sl": sl_px, "tp": tp_base, "tp_ids": tp_ids
        })
        return entry

    # -------------------------------------------------------------------------
    # 외부에서 호출: 포지션 종료(전량/부분) → 잔여 브래킷 정리
    # -------------------------------------------------------------------------
    def close_position(
        self,
        symbol: str,
        reason: str = "manual",
        quantity: Optional[float] = None,   # None이면 전체
        client_order_id_prefix: Optional[str] = None,
    ) -> Optional[dict]:
        """
        시장가 청산 → 잔여 SL/TP 취소/정리 → DB/이벤트
        """
        try:
            pos = self._fetch_position(symbol)
            amt = abs(float(pos.get("positionAmt", 0)))
            if amt <= 0:
                print(f"ℹ️ {symbol} 현재 보유 없음")
                return None

            # 청산 수량 결정
            q = amt if quantity is None else min(float(quantity), amt)
            close_side = "BUY" if float(pos["positionAmt"]) < 0 else "SELL"

            res = self.client.futures_create_order(
                symbol=symbol, side=close_side, type="MARKET", quantity=q,
                newClientOrderId=self._cid(symbol, client_order_id_prefix, "CLS")
            )

            # 잔량 확인 후 브래킷 정리
            left = amt - q
            if left <= 1e-12:
                self._cancel_brackets(symbol)  # 전량 청산 시 전부 취소
            else:
                # 부분청산이면 수량 동기화가 필요할 수 있음(상황에 따라 TP 수량을 재발행/유지)
                pass

            # DB / 이벤트
            last_px = self._fetch_last_price(symbol)
            self._record_trade_close(symbol, last_px, reason, is_partial=(left > 0))
            event_bus.safe_publish("ORDER_CLOSE_SUCCESS", {
                "symbol": symbol, "reason": reason, "is_partial": left > 0
            })
            return res
        except BinanceAPIException as e:
            print(f"🚨 청산 실패: {e}")
            return None

    # -------------------------------------------------------------------------
    # 주기 호출: 타임스탑/트레일링 스탑 업데이트 (캔들 close 혹은 틱마다)
    # -------------------------------------------------------------------------
    def on_tick(self, symbol: str, last_price: float, last_atr: Optional[float] = None):
        """
        캔들 close 또는 틱 업데이트 때 호출해 브래킷을 갱신한다.
        last_atr를 넘겨주면 ATR 트레일링에 사용한다.
        """
        st = self._live_brackets.get(symbol)
        if not st:
            return
        policy = ExecPolicy.from_config(symbol)

        # 1) 타임스탑
        if policy.time_stop_bars and policy.time_stop_bars > 0:
            st["bars_held"] = int(st.get("bars_held", 0)) + 1
            if st["bars_held"] >= policy.time_stop_bars:
                print(f"⏱️  타임스탑: {symbol} k={policy.time_stop_bars}")
                self.close_position(symbol, reason="time_stop")
                return

        # 2) 트레일링
        if policy.trailing_mode != "off":
            try:
                if policy.trailing_mode == "atr":
                    atr = float(last_atr or 0.0)
                    if atr <= 0:
                        # 필요한 경우 외부 지표 캐시에서 가져오도록 구성 가능
                        return
                    trail = atr * max(0.0, policy.trailing_k)
                else:
                    trail = float(last_price) * max(0.0, policy.trailing_k) / 100.0

                entry = float(st["entry_price"])
                side = st["side"]
                close_side = "BUY" if side == "SELL" else "SELL"

                # SL 주문 취소 → 재발행
                if st.get("sl_id"):
                    try:
                        self.client.futures_cancel_order(symbol=symbol, orderId=st["sl_id"])
                    except BinanceAPIException:
                        pass

                if side == "BUY":
                    new_sl = round(max(entry, float(last_price) - trail), 4)  # BE 보호
                else:
                    new_sl = round(min(entry, float(last_price) + trail), 4)

                # 현재 보유 수량
                pos = self._fetch_position(symbol)
                amt = abs(float(pos.get("positionAmt", 0)))
                if amt > 0:
                    new_sl_order = self.client.futures_create_order(
                        symbol=symbol, side=close_side, type="STOP_MARKET",
                        stopPrice=new_sl, reduceOnly=True, quantity=amt,
                        newClientOrderId=self._cid(symbol, None, "SLtrail")
                    )
                    st["sl_id"] = new_sl_order.get("orderId")
                    self._live_brackets[symbol] = st
                    print(f"🧵 트레일 SL 갱신: {symbol} → {new_sl}")
            except BinanceAPIException as e:
                print(f"🚨 트레일링 실패: {e}")

    # -------------------------------------------------------------------------
    # 내부 유틸
    # -------------------------------------------------------------------------
    def _cid(self, symbol: str, prefix: Optional[str], tag: str) -> str:
        t = int(datetime.now(timezone.utc).timestamp() * 1000)
        return f"{(prefix or 'TE')}-{symbol}-{tag}-{t}"

    def _scale_tp(self, entry_px: float, tp_base: float, side: str, mult: float) -> float:
        """
        멀티 TP용 — 0.5R/1.0R/1.5R 같은 배수를 tp_base를 기준으로 스케일링
        """
        if mult == 1.0:
            return tp_base
        if side == "BUY":
            r_unit = tp_base - entry_px
            return round(entry_px + r_unit * mult, 4)
        else:
            r_unit = entry_px - tp_base
            return round(entry_px - r_unit * mult, 4)

    def _cancel_brackets(self, symbol: str):
        st = self._live_brackets.pop(symbol, None)
        if not st:
            return
        # SL 취소
        sl_id = st.get("sl_id")
        if sl_id:
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=sl_id)
            except BinanceAPIException:
                pass
        # TP 취소
        for oid in st.get("tp_ids", []):
            try:
                self.client.futures_cancel_order(symbol=symbol, orderId=oid)
            except BinanceAPIException:
                pass

    def _fetch_last_price(self, symbol: str) -> float:
        try:
            ob = self.client.futures_symbol_ticker(symbol=symbol)
            return float(ob["price"])
        except Exception:
            return 0.0

    def _fetch_position(self, symbol: str) -> dict:
        try:
            info = self.client.futures_position_information(symbol=symbol)
            if info:
                return info[0]
        except Exception:
            pass
        return {"positionAmt": 0}

    # -------------------------------------------------------------------------
    # DB 기록(프로젝트 스키마에 맞춰 최소한만)
    # -------------------------------------------------------------------------
    def _record_trade_open(self, symbol: str, side: str, entry_px: float, qty: float, extra: Optional[dict]):
        try:
            session = db_manager.session()
            trade = Trade(
                symbol=symbol, side=side, status="OPEN",
                entry_price=entry_px, entry_time=datetime.utcnow(),
                quantity=qty, meta=extra or {}
            )
            session.add(trade)
            session.commit()
        except Exception as e:
            # DB 실패는 치명적이지 않게 로그만
            print(f"⚠️ DB open 기록 실패: {e}")

    def _record_trade_close(self, symbol: str, exit_px: float, reason: str, is_partial: bool):
        try:
            session = db_manager.session()
            trade: Trade = session.query(Trade).filter(
                Trade.symbol == symbol, Trade.status.in_(["OPEN", "PARTIAL"])
            ).order_by(Trade.entry_time.desc()).first()
            if not trade:
                return
            # PnL 근사(롱/숏 구분)
            if trade.side == "BUY":
                pnl = (exit_px - float(trade.entry_price)) * float(trade.quantity or 0)
            else:
                pnl = (float(trade.entry_price) - exit_px) * float(trade.quantity or 0)

            trade.pnl = (trade.pnl or 0) + pnl
            trade.exit_price = exit_px
            trade.exit_time = datetime.utcnow()

            if is_partial:
                trade.status = "PARTIAL"
            else:
                trade.status = "CLOSED"

            session.commit()
        except Exception as e:
            print(f"⚠️ DB close 기록 실패: {e}")
