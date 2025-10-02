import discord
from discord import app_commands
from discord.ext import commands, tasks
from binance.client import Client
from binance.exceptions import BinanceAPIException
import asyncio
from datetime import datetime, timezone, timedelta
from enum import Enum
from sqlalchemy import select
import pandas as pd

# 1. 모듈 임포트
from core.config_manager import config
from core.event_bus import event_bus
from database.manager import db_manager, Signal, Trade
from execution.trading_engine import TradingEngine
from analysis.confluence_engine import ConfluenceEngine
from risk_management.position_sizer import PositionSizer
from ui.views import ControlPanelView

# 2. 초기화 (기존과 동일)
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

try:
    binance_client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
    if config.is_testnet:
        binance_client.FUTURES_URL = 'https://testnet.binancefuture.com'
    binance_client.ping()
    print(f"바이낸스 연결 성공. (환경: {config.trade_mode})")
except Exception as e:
    print(f"바이낸스 연결 실패: {e}")
    exit()

# 3. 엔진 초기화
trading_engine = TradingEngine(binance_client)
confluence_engine = ConfluenceEngine(binance_client)
position_sizer = PositionSizer(binance_client)
# analyzer는 현재 청산 로직이 없어 리포트를 생성하지 않으므로, 추후 활성화
# analyzer = PerformanceAnalyzer()

# 4. 전역 변수

class MarketRegime(Enum):
    BULL_TREND = "강세 추세"
    BEAR_TREND = "약세 추세"
    SIDEWAYS = "횡보"

current_aggr_level = config.aggr_level

# --- 백그라운드 작업 (V3) ---

@tasks.loop(minutes=1)
async def data_collector_loop():
    print(f"\n--- [Data Collector] 분석 시작 ---")
    session = db_manager.get_session()
    try:
        for symbol in config.symbols:
            final_score, tf_scores, tf_rows = confluence_engine.analyze(symbol)
            if not tf_rows: continue
            
            # 1일봉 ATR 추출 및 추가 지표 저장
            atr_1d_val = confluence_engine.extract_atr(tf_rows, primary_tf='1d')
            adx_4h_val = None
            is_above_ema200 = None

            four_hour_row = tf_rows.get("4h")
            if isinstance(four_hour_row, pd.Series):
                adx_4h_val = four_hour_row.get("adx_value")

            daily_row = tf_rows.get("1d")
            if isinstance(daily_row, pd.Series):
                is_above_ema200 = daily_row.get("is_above_ema200")

            new_signal = Signal(
                symbol=symbol, final_score=final_score,
                score_1d=tf_scores.get("1d"), score_4h=tf_scores.get("4h"),
                score_1h=tf_scores.get("1h"), score_15m=tf_scores.get("15m"),
                atr_1d=atr_1d_val,
                adx_4h=adx_4h_val,
                is_above_ema200_1d=is_above_ema200
            )
            session.add(new_signal)
        session.commit()
    except Exception as e:
        print(f"🚨 데이터 수집 중 오류: {e}")
        session.rollback()
    finally:
        session.close()

def diagnose_market_regime(session, symbol: str) -> MarketRegime:
    """최근 신호를 기반으로 시장 체제를 추정한다."""
    latest_signal = (
        session.execute(
            select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc())
        ).scalar_one_or_none()
    )

    if (
        not latest_signal
        or latest_signal.adx_4h is None
        or latest_signal.is_above_ema200_1d is None
    ):
        return MarketRegime.SIDEWAYS

    adx_value = latest_signal.adx_4h
    is_above_ema = bool(latest_signal.is_above_ema200_1d)

    if adx_value > config.market_regime_adx_th:
        return MarketRegime.BULL_TREND if is_above_ema else MarketRegime.BEAR_TREND
    return MarketRegime.SIDEWAYS

def update_adaptive_aggression_level():
    global current_aggr_level
    base_aggr_level = config.aggr_level
    session = db_manager.get_session()
    try:
        # BTC의 최신 1일봉 ATR 데이터로 변동성 판단
        latest_signal = session.execute(select(Signal).where(Signal.symbol == "BTCUSDT").order_by(Signal.id.desc())).scalar_one_or_none()
        if not latest_signal or not latest_signal.atr_1d: return

        mark_price_info = binance_client.futures_mark_price(symbol="BTCUSDT")
        current_price = float(mark_price_info['markPrice'])
        volatility = latest_signal.atr_1d / current_price

        if volatility > config.adaptive_volatility_threshold:
            new_level = max(1, base_aggr_level - 2)
            if new_level != current_aggr_level:
                print(f"[Adaptive] 변동성 증가 감지! 공격성 레벨 조정: {current_aggr_level} -> {new_level}")
                current_aggr_level = new_level
        else:
            if current_aggr_level != base_aggr_level:
                print(f"[Adaptive] 시장 안정. 공격성 레벨 복귀: {current_aggr_level} -> {base_aggr_level}")
                current_aggr_level = base_aggr_level
    except Exception as e:
        print(f"🚨 적응형 레벨 조정 중 오류: {e}")
    finally:
        session.close()

@tasks.loop(minutes=5)
async def trading_decision_loop():
    if not config.exec_active:
        return

    if config.adaptive_aggr_enabled:
        update_adaptive_aggression_level()

    print(f"\n--- [Trading Decision (Lvl:{current_aggr_level})] 매매 결정 시작 ---")
    session = db_manager.get_session()
    try:
        open_trades = (
            session.execute(select(Trade).where(Trade.status == "OPEN")).scalars().all()
        )
        open_positions_count = len(open_trades)

        if open_positions_count > 0:
            print(f"총 {open_positions_count}개의 오픈된 포지션 관리 중...")
            for trade in list(open_trades):
                try:
                    current_price_info = binance_client.futures_mark_price(symbol=trade.symbol)
                    current_price = float(current_price_info["markPrice"])
                except Exception as price_err:
                    print(f"가격 조회 실패({trade.symbol}): {price_err}")
                    continue

                if config.trailing_stop_enabled and trade.entry_atr:
                    if trade.side == "BUY":
                        if (
                            trade.highest_price_since_entry is None
                            or current_price > trade.highest_price_since_entry
                        ):
                            trade.highest_price_since_entry = current_price
                            session.commit()
                            print(f"📈 최고가 갱신: ${current_price}")
                        trailing_stop_price = (
                            trade.highest_price_since_entry
                            - (trade.entry_atr * config.sl_atr_multiplier)
                        )
                        if current_price < trailing_stop_price:
                            await trading_engine.close_position(
                                trade, f"트레일링 스탑 (TS: ${trailing_stop_price:.2f})"
                            )
                            open_positions_count = max(0, open_positions_count - 1)
                            continue
                    else:
                        if (
                            trade.highest_price_since_entry is None
                            or current_price < trade.highest_price_since_entry
                        ):
                            trade.highest_price_since_entry = current_price
                            session.commit()
                            print(f"📉 최저가 갱신: ${current_price}")
                        trailing_stop_price = (
                            trade.highest_price_since_entry
                            + (trade.entry_atr * config.sl_atr_multiplier)
                        )
                        if current_price > trailing_stop_price:
                            await trading_engine.close_position(
                                trade, f"트레일링 스탑 (TS: ${trailing_stop_price:.2f})"
                            )
                            open_positions_count = max(0, open_positions_count - 1)
                            continue

                pnl_pct = (
                    (current_price - trade.entry_price) / trade.entry_price
                    if trade.side == "BUY"
                    else (trade.entry_price - current_price) / trade.entry_price
                )
                if pnl_pct >= config.take_profit_pct:
                    await trading_engine.close_position(
                        trade, f"수익 실현 ({pnl_pct:+.2%})"
                    )
                    open_positions_count = max(0, open_positions_count - 1)

        if open_positions_count < config.max_open_positions:
            print(
                f"신규 진입 기회 탐색 중... (현재 {open_positions_count}/{config.max_open_positions} 슬롯 사용 중)"
            )

            symbols_in_trade = {t.symbol for t in open_trades}
            symbols_to_scan = [s for s in config.symbols if s not in symbols_in_trade]

            for symbol in symbols_to_scan:
                market_regime = diagnose_market_regime(session, symbol)
                print(f"[{symbol}] 현재 시장 체제: {market_regime.value}")

                if market_regime in (MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND):
                    recent_signals = (
                        session.execute(
                            select(Signal)
                            .where(Signal.symbol == symbol)
                            .order_by(Signal.timestamp.desc())
                            .limit(config.trend_entry_confirm_count)
                        ).scalars().all()
                    )

                    if len(recent_signals) < config.trend_entry_confirm_count:
                        continue

                    entry_signals = recent_signals
                    scores = [s.final_score for s in entry_signals]

                    is_buy_base = all(score > config.open_th for score in scores)
                    is_sell_base = all(score < -config.open_th for score in scores)

                    if not is_buy_base and not is_sell_base:
                        continue

                    std_series = pd.Series(scores).std() if len(scores) > 1 else 0.0
                    std_dev = float(std_series) if not pd.isna(std_series) else 0.0
                    avg_score = sum(scores) / len(scores)
                    is_momentum_positive = (
                        scores[0] > scores[-1] if len(scores) > 1 else True
                    )

                    print(
                        f"[{symbol}] 추세 신호 평가: Avg={avg_score:.2f}, StdDev={std_dev:.2f}, "
                        f"Momentum={'OK' if is_momentum_positive else 'Not Good'}"
                    )

                    is_quality_buy = (
                        market_regime == MarketRegime.BULL_TREND
                        and is_buy_base
                        and avg_score >= config.quality_min_avg_score
                        and std_dev <= config.quality_max_std_dev
                        and is_momentum_positive
                    )

                    is_quality_sell = (
                        market_regime == MarketRegime.BEAR_TREND
                        and is_sell_base
                        and abs(avg_score) >= config.quality_min_avg_score
                        and std_dev <= config.quality_max_std_dev
                        and is_momentum_positive
                    )

                    if not (is_quality_buy or is_quality_sell):
                        continue

                    side = "BUY" if is_quality_buy else "SELL"
                    final_signal = entry_signals[0]
                    entry_atr = final_signal.atr_1d or 0.0

                    if entry_atr <= 0:
                        print(f"[{symbol}] ATR 값이 유효하지 않아 주문을 실행할 수 없습니다.")
                        continue

                    quantity = position_sizer.calculate_position_size(
                        symbol, entry_atr, current_aggr_level, open_positions_count
                    )
                    if not quantity or quantity <= 0:
                        continue

                    leverage = position_sizer.get_leverage_for_symbol(
                        symbol, current_aggr_level
                    )
                    analysis_context = {
                        "symbol": symbol,
                        "side": side,
                        "final_score": final_signal.final_score,
                        "tf_scores": {
                            "1d": final_signal.score_1d,
                            "4h": final_signal.score_4h,
                            "1h": final_signal.score_1h,
                            "15m": final_signal.score_15m,
                        },
                        "entry_atr": entry_atr,
                        "signal_id": final_signal.id,
                        "leverage": leverage,
                        "market_regime": market_regime.value,
                    }
                    await trading_engine.place_order(symbol, side, quantity, analysis_context)
                    return

                if market_regime == MarketRegime.SIDEWAYS:
                    recent_signals = (
                        session.execute(
                            select(Signal)
                            .where(Signal.symbol == symbol)
                            .order_by(Signal.timestamp.desc())
                            .limit(config.sideways_rsi_confirm_count)
                        ).scalars().all()
                    )

                    if len(recent_signals) < config.sideways_rsi_confirm_count:
                        continue

                    entry_signals = recent_signals
                    is_oversold = all(-5 < s.final_score < 0 for s in entry_signals)
                    is_overbought = all(0 < s.final_score < 5 for s in entry_signals)

                    if not is_oversold and not is_overbought:
                        continue

                    side = "BUY" if is_oversold else "SELL"
                    final_signal = entry_signals[0]
                    entry_atr = final_signal.atr_1d or 0.0

                    if entry_atr <= 0:
                        print(f"[{symbol}] ATR 값이 유효하지 않아 주문을 실행할 수 없습니다.")
                        continue

                    quantity = position_sizer.calculate_position_size(
                        symbol, entry_atr, current_aggr_level, open_positions_count
                    )
                    if not quantity or quantity <= 0:
                        continue

                    leverage = position_sizer.get_leverage_for_symbol(
                        symbol, current_aggr_level
                    )
                    analysis_context = {
                        "symbol": symbol,
                        "side": side,
                        "final_score": final_signal.final_score,
                        "tf_scores": {
                            "1d": final_signal.score_1d,
                            "4h": final_signal.score_4h,
                            "1h": final_signal.score_1h,
                            "15m": final_signal.score_15m,
                        },
                        "entry_atr": entry_atr,
                        "signal_id": final_signal.id,
                        "leverage": leverage,
                        "market_regime": market_regime.value,
                    }

                    if is_oversold:
                        print(f"횡보장 저점 포착! [평균 회귀 매수]: {symbol}")
                    else:
                        print(f"횡보장 고점 포착! [평균 회귀 매도]: {symbol}")

                    await trading_engine.place_order(symbol, side, quantity, analysis_context)
                    return
        else:
            print(
                f"최대 포지션 개수({config.max_open_positions})에 도달하여 신규 진입을 탐색하지 않습니다."
            )

    except Exception as e:
        print(f"🚨 매매 결정 중 오류: {e}")
    finally:
        session.close()



# --- 봇 준비 및 실행 ---
@bot.event
async def on_ready():
    print(f'{bot.user.name} 봇이 준비되었습니다.')
    data_collector_loop.start()
    await asyncio.sleep(5)
    trading_decision_loop.start()

# ... (Discord 명령어 관련 코드는 기존과 동일)
# ... (봇 실행 코드는 기존과 동일)
