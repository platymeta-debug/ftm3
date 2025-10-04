# 파일명: execution/trading_engine.py
# V5.0 — 공통 리스크 사이징(calc_order_qty) 통합 + JSON 기반 실행정책 + 브래킷(멀티TP/SL/트레일/타임스탑)
# - quantity 미지정 시 calc_order_qty()로 절대 수량 자동 계산 (하드코딩 제거)
# - 레버리지→margin(=1/leverage) 반영, 거래소 필터(minNotional/stepSize/minQty) 준수
# - 기존 DB/이벤트/브래킷/트레일링/타임스탑 로직은 유지

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Dict, List, Any
from datetime import datetime, timezone

from binance.client import Client
from binance.exceptions import BinanceAPIException

# 프로젝트 컴포넌트
from core.config_manager import config        # ▶ 옵티마이저가 만든 JSON을 여기서 로드
from core.event_bus import event_bus
from database.manager import db_manager
from database.models import Signal, Trade

# 공통 리스크 사이징 유틸
from analysis.risk_sizing import calc_order_qty


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
    # 리스크 사이징(절대 수량 계산용)
    risk_per_trade: float = 0.01          # 자본 1%
    max_exposure_frac: float = 0.30       # 총노출 = 자본의 30% (레버리지 전 기준)

    @staticmethod
    def from_config(symbol: str) -> "ExecPolicy":
        """
        config에서 해당 심볼의 실행정책을 불러온다.
        기대 키:
          - sl_atr_multiplier, risk_reward_ratio
          - exec_partial, exec_time_stop_bars, exec_trailing_mode, exec_trailing_k
          - risk_per_trade, max_exposure_frac
        모두 없으면 안전한 기본값 사용
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
        # 리스크 사이징
        rpt = float(p.get("risk_per_trade", getattr(config, "risk_per_trade", 0.01)))
        mef = float(p.get("max_exposure_frac", getattr(config, "max_exposure_frac", 0.30)))
        return ExecPolicy(
            sl_atr_mult=float(sl_mult),
            rr=float(rr),
            partial=partial or [],
            time_stop_bars=time_stop,
            trailing_mode=trailing_mode,
            trailing_k=trailing_k,
            risk_per_trade=rpt,
            max_exposure_frac=mef,
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
    - quantity가 미지정이면 calc_order_qty()로 **절대 수량** 자동 계산
    """

    def __init__(self, client: Client):
        self.client = client
        self._live_brackets: Dict[str, Dict] = {}  # symbol -> {sl_id, tp_ids[], entry_price, side, bars_held, quantity}
        # 레버리지 맵(심볼별), 기본 10x
        self._leverage_by_symbol: Dict[str, float] = {}
        # 거래소 심볼 필터 캐시
        self._filters_cache: Dict[str, Dict[str, float]] = {}
        print("🚚 [V5.0] 트레이딩 엔진 초기화(리스크 사이징 통합, 멀티TP/트레일/타임스탑, JSON 설정).")

    # -------------------------------------------------------------------------
    # 외부에서 호출: 신규 진입 + 브래킷 자동 부착
    # -------------------------------------------------------------------------
    def open_with_bracket(
        self,
        symbol: str,
        side: str,                      # "BUY" | "SELL"
        entry_atr: float,
        quantity: Optional[float] = None,   # None 또는 <=0이면 calc_order_qty()로 자동 계산
        entry_type: str = "MARKET",         # "MARKET"|"LIMIT"
        entry_price: Optional[float] = None,
        client_order_id_prefix: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> Optional[dict]:
        """
        1) (필요 시) 리스크 기반 절대 수량 계산
        2) 진입 주문
        3) 체결 기준가(entry_price) 확보
        4) SL/TP 브래킷 부착(부분익절 가능)
        5) DB 기록/이벤트 발행
        """
        policy = ExecPolicy.from_config(symbol)
        side = side.upper()
        close_side = "BUY" if side == "SELL" else "SELL"

        # --- 0) 리스크 기반 수량 계산 (quantity가 없거나 ≤0일 때만) -------------------------
        qty = float(quantity or 0.0)
        if qty <= 0:
            # 필수 값 체크
            if entry_atr is None or float(entry_atr) <= 0:
                print(f"⚠️ {symbol} 리스크 사이징 실패: entry_atr가 필요합니다.")
                return None
            last_px = self._fetch_last_price(symbol) if entry_type != "LIMIT" or not entry_price else float(entry_price)
            if last_px <= 0:
                print(f"⚠️ {symbol} 리스크 사이징 실패: 참조 가격이 유효하지 않음.")
                return None

            filt = self._get_symbol_filters(symbol)
            equity = self.get_equity_usdt()
            lev = self._get_leverage(symbol)
            margin = 1.0 / max(1.0, lev)

            qty = calc_order_qty(
                price=float(last_px),
                atr=float(entry_atr),
                sl_atr_mult=float(policy.sl_atr_mult),
                equity=float(equity),
                risk_per_trade=float(policy.risk_per_trade),
                max_exposure_frac=float(policy.max_exposure_frac),
                margin=float(margin),
                min_notional=float(filt["min_notional"]),
                qty_step=float(filt["qty_step"]),
                min_qty=float(filt["min_qty"]),
            )
            if qty <= 0:
                print(f"⚠️ {symbol} sizing=0 → 진입 스킵 (equity={equity:.2f}, price={last_px:.4f})")
                return None

        # --- 1) 진입 주문 ----------------------------------------------------------------
        try:
            if entry_type == "LIMIT":
                assert entry_price is not None and entry_price > 0
                entry = self.client.futures_create_order(
                    symbol=symbol, side=side, type="LIMIT",
                    price=round(float(entry_price), 6),
                    quantity=qty, timeInForce="GTC",
                    newClientOrderId=self._cid(symbol, client_order_id_prefix, "ENTRYL")
                )
            else:
                entry = self.client.futures_create_order(
                    symbol=symbol, side=side, type="MARKET",
                    quantity=qty,
                    newClientOrderId=self._cid(symbol, client_order_id_prefix, "ENTRYM")
                )
            print(f"➡️  {symbol} {side} {qty} 진입 전송 OK")
        except BinanceAPIException as e:
            print(f"🚨 진입 주문 실패: {symbol} {side} x{qty} — {e}")
            return None

        # --- 2) 체결가 산정 --------------------------------------------------------------
        try:
            avg_px = float(entry.get("avgPrice") or 0) or self._fetch_last_price(symbol)
            entry_px = round(avg_px, 6)
        except Exception:
            entry_px = round(self._fetch_last_price(symbol), 6)

        # --- 3) SL/TP 계산 및 주문 전송 ---------------------------------------------------
        sl_d = float(entry_atr) * policy.sl_atr_mult
        if sl_d <= 0:
            print("⚠️ ATR 기반 SL 거리가 0 이하 — 브래킷 생략")
            return entry

        if side == "BUY":
            sl_px = round(entry_px - sl_d, 6)
            tp_base = round(entry_px + (sl_d * policy.rr), 6)
        else:
            sl_px = round(entry_px + sl_d, 6)
            tp_base = round(entry_px - (sl_d * policy.rr), 6)

        # 3-1) SL(손절): reduceOnly + 수량 지정
        try:
            sl_order = self.client.futures_create_order(
                symbol=symbol,
                side=close_side,
                type="STOP_MARKET",
                stopPrice=sl_px,
                reduceOnly=True,
                quantity=qty,
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
                    quantity=qty,
                    newClientOrderId=self._cid(symbol, client_order_id_prefix, "TP")
                )
                if "orderId" in tp_single:
                    tp_ids.append(str(tp_single["orderId"]))
            else:
                # 멀티 TP: 0.5R/1.0R/1.5R 스텝 — 비중은 policy.partial에 따름
                steps = [0.5, 1.0, 1.5] if len(policy.partial) == 3 else [1.0] * len(policy.partial)
                # 수량 분할: 마지막 조각은 잔량 보정
                remain = qty
                for i, (w, m) in enumerate(zip(policy.partial, steps)):
                    if i < len(policy.partial) - 1:
                        sub_qty = round(qty * float(w), 6)
                    else:
                        sub_qty = round(remain, 6)  # 잔량 모두
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
        except BinanceAPIException as e:
            print(f"🚨 TP 주문 실패: {e}")

        # 상태 저장 (트레일/타임스탑/정리용)
        self._live_brackets[symbol] = {
            "sl_id": sl_order.get("orderId"),
            "tp_ids": tp_ids,
            "entry_price": entry_px,
            "side": side,
            "bars_held": 0,
            "quantity": float(qty),
        }

        # --- 4) DB / 이벤트 --------------------------------------------------------------
        self._record_trade_open(symbol, side, entry_px, float(qty), extra)
        event_bus.safe_publish("ORDER_OPEN_SUCCESS", {
            "symbol": symbol, "side": side, "qty": float(qty), "entry": entry_px,
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
                    new_sl = round(max(entry, float(last_price) - trail), 6)  # BE 보호
                else:
                    new_sl = round(min(entry, float(last_price) + trail), 6)

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
    # 레버리지/심볼 필터/자산 평가
    # -------------------------------------------------------------------------
    def set_leverage(self, symbol: str, leverage: float):
        try:
            lev = max(1.0, float(leverage))
        except Exception:
            lev = 10.0
        self._leverage_by_symbol[symbol] = lev
        try:
            self.client.futures_change_leverage(symbol=symbol, leverage=int(round(lev)))
        except Exception:
            # 권한 없거나 현물계정이면 무시
            pass

    def _get_leverage(self, symbol: str) -> float:
        return float(self._leverage_by_symbol.get(symbol, 10.0))

    def _get_symbol_filters(self, symbol: str) -> Dict[str, float]:
        """
        거래소 심볼 필터(최소 주문가치, 스텝, 최소수량)를 캐싱하여 리턴.
        """
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]

        min_notional = 5.0
        qty_step = 1e-6
        min_qty = 1e-6
        try:
            info = self.client.get_symbol_info(symbol)
            for flt in info.get("filters", []):
                t = flt.get("filterType")
                if t in ("NOTIONAL", "MIN_NOTIONAL"):
                    mn = flt.get("minNotional") or flt.get("notional")
                    if mn is not None:
                        min_notional = float(mn)
                if t == "LOT_SIZE":
                    if flt.get("stepSize"): qty_step = float(flt["stepSize"])
                    if flt.get("minQty"):   min_qty  = float(flt["minQty"])
        except Exception as e:
            print(f"[{symbol}] 심볼 필터 조회 실패: {e} → 기본값 사용")

        self._filters_cache[symbol] = {
            "min_notional": min_notional,
            "qty_step": qty_step,
            "min_qty": min_qty,
        }
        return self._filters_cache[symbol]

    def get_equity_usdt(self) -> float:
        """
        계좌 평가금액(USDT). 거래소 API에 맞게 구현하세요.
        (바이낸스 선물 예시)
        """
        try:
            bal = self.client.futures_account_balance()
            for b in bal:
                if (b.get("asset") or "").upper() in ("USDT", "BUSD", "USDC", "FDUSD", "TUSD"):
                    return float(b.get("balance", 0) or 0.0)
        except Exception:
            pass
        return 0.0

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
            return round(entry_px + r_unit * mult, 6)
        else:
            r_unit = entry_px - tp_base
            return round(entry_px - r_unit * mult, 6)

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
