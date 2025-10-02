import discord
from discord import app_commands
from discord.ext import commands, tasks
from binance.client import Client
from binance.exceptions import BinanceAPIException
import asyncio
from datetime import datetime, timezone, timedelta
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
            
            # 1일봉 ATR 추출
            atr_1d_val = confluence_engine.extract_atr(tf_rows, primary_tf='1d')

            new_signal = Signal(
                symbol=symbol, final_score=final_score,
                score_1d=tf_scores.get("1d"), score_4h=tf_scores.get("4h"),
                score_1h=tf_scores.get("1h"), score_15m=tf_scores.get("15m"),
                atr_1d=atr_1d_val
            )
            session.add(new_signal)
        session.commit()
    except Exception as e:
        print(f"🚨 데이터 수집 중 오류: {e}")
        session.rollback()
    finally:
        session.close()

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
    if not config.exec_active: return

    if config.adaptive_aggr_enabled:
        update_adaptive_aggression_level()

    print(f"\n--- [Trading Decision (Lvl:{current_aggr_level})] 매매 결정 시작 ---")
    session = db_manager.get_session()
    try:
        open_trade = session.execute(select(Trade).where(Trade.status == "OPEN")).scalar_one_or_none()

        if open_trade:
            # --- B. 포지션이 있을 경우 (청산 결정) ---
            print(f"오픈된 포지션 관리 중: {open_trade.symbol} {open_trade.side}")
            current_price_info = binance_client.futures_mark_price(symbol=open_trade.symbol)
            current_price = float(current_price_info['markPrice'])

            # 1. 진입 후 최고가/최저가 업데이트
            if config.trailing_stop_enabled and open_trade.entry_atr:
                if open_trade.side == "BUY" and (open_trade.highest_price_since_entry is None or current_price > open_trade.highest_price_since_entry):
                    open_trade.highest_price_since_entry = current_price
                    session.commit()
                    print(f"📈 최고가 갱신: ${current_price}")
                elif open_trade.side == "SELL" and (open_trade.highest_price_since_entry is None or current_price < open_trade.highest_price_since_entry):
                    open_trade.highest_price_since_entry = current_price
                    session.commit()
                    print(f"📉 최저가 갱신: ${current_price}")

                # 2. 동적 트레일링 스탑 라인 계산
                if open_trade.side == "BUY":
                    trailing_stop_price = open_trade.highest_price_since_entry - (open_trade.entry_atr * config.sl_atr_multiplier)
                    if current_price < trailing_stop_price:
                        await trading_engine.close_position(open_trade, f"트레일링 스탑 (TS: ${trailing_stop_price:.2f})")
                        return
                else:
                    trailing_stop_price = open_trade.highest_price_since_entry + (open_trade.entry_atr * config.sl_atr_multiplier)
                    if current_price > trailing_stop_price:
                        await trading_engine.close_position(open_trade, f"트레일링 스탑 (TS: ${trailing_stop_price:.2f})")
                        return

            # 보조적 익절 로직 유지
            pnl_pct = (current_price - open_trade.entry_price) / open_trade.entry_price if open_trade.side == "BUY" else (open_trade.entry_price - current_price) / open_trade.entry_price
            if pnl_pct >= config.take_profit_pct:
                await trading_engine.close_position(open_trade, f"수익 실현 ({pnl_pct:+.2%})")
                return

            # ... (추세 반전 로직은 기존과 동일)

        else:
            # --- A. 포지션이 없을 경우 (신규 진입 결정) ---
            print("신규 진입 기회 탐색 중...")
            for symbol in config.symbols:
                lookback_time = datetime.utcnow() - timedelta(minutes=10)
                recent_signals = session.execute(
                    select(Signal)
                    .where(Signal.symbol == symbol)
                    .where(Signal.timestamp >= lookback_time)
                    .order_by(Signal.timestamp.desc())
                ).scalars().all()

                if len(recent_signals) < config.entry_confirm_count:
                    continue

                entry_signals = recent_signals[:config.entry_confirm_count]
                scores = [s.final_score for s in entry_signals]

                is_buy_base = all(score > config.open_threshold for score in scores)
                is_sell_base = all(score < -config.open_threshold for score in scores)

                if not is_buy_base and not is_sell_base:
                    continue

                avg_score = sum(scores) / len(scores)
                std_series = pd.Series(scores).std()
                std_dev = float(std_series) if not pd.isna(std_series) else 0.0
                is_momentum_positive = scores[0] > scores[-1] if is_buy_base else scores[0] < scores[-1]

                print(
                    f"[{symbol}] 신호 품질 평가: Avg={avg_score:.2f}, StdDev={std_dev:.2f}, "
                    f"Momentum={'OK' if is_momentum_positive else 'Not Good'}"
                )

                is_quality_buy = (
                    is_buy_base and
                    avg_score >= config.quality_min_avg_score and
                    std_dev <= config.quality_max_std_dev and
                    is_momentum_positive
                )

                is_quality_sell = (
                    is_sell_base and
                    abs(avg_score) >= config.quality_min_avg_score and
                    std_dev <= config.quality_max_std_dev and
                    is_momentum_positive
                )

                if not (is_quality_buy or is_quality_sell):
                    continue

                side = "BUY" if is_quality_buy else "SELL"
                print(f"🚀 고품질 거래 신호 포착!: {symbol} {side}")

                final_signal = entry_signals[0]
                atr_source = {}
                if final_signal.atr_1d is not None:
                    atr_source["1d"] = {"ATR_14": final_signal.atr_1d}
                atr = confluence_engine.extract_atr(atr_source, primary_tf="1d") if atr_source else 0.0

                if not atr or atr <= 0:
                    print(f"[{symbol}] ATR 값이 유효하지 않아 주문을 실행할 수 없습니다.")
                    continue

                quantity = position_sizer.calculate_position_size(symbol, current_aggr_level, atr)
                if not quantity or quantity <= 0:
                    continue

                leverage = position_sizer.get_leverage_for_symbol(symbol, current_aggr_level)
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
                    "entry_atr": atr,
                    "signal_id": final_signal.id,
                    "leverage": leverage,
                }
                await trading_engine.place_order(symbol, side, quantity, analysis_context)
                return
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
