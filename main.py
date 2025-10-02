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
            # 포지션 관리 로직
            current_price = float(binance_client.futures_mark_price(symbol=open_trade.symbol)['markPrice'])
            pnl_pct = (current_price / open_trade.entry_price - 1) if open_trade.side == "BUY" else (1 - current_price / open_trade.entry_price)

            # 1. 익절
            if pnl_pct >= config.take_profit_pct:
                await trading_engine.close_position(open_trade, f"수익 실현 ({pnl_pct:+.2%})")
                return

            # 2. 기술적 손절 (ATR 기반)
            sl_price = (open_trade.entry_price - open_trade.entry_atr * config.sl_atr_multiplier) if open_trade.side == "BUY" else (open_trade.entry_price + open_trade.entry_atr * config.sl_atr_multiplier)
            if (open_trade.side == "BUY" and current_price <= sl_price) or \
               (open_trade.side == "SELL" and current_price >= sl_price):
                await trading_engine.close_position(open_trade, f"ATR 손절 (SL: {sl_price})")
                return
            
            # ... (추세 반전 청산 로직은 이전과 동일하게 유지)

        else:
            # 신규 진입 로직
            for symbol in config.symbols:
                recent_signals = session.execute(select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc()).limit(config.entry_confirm_count)).scalars().all()
                if len(recent_signals) < config.entry_confirm_count: continue

                is_buy = all(s.final_score > config.open_th for s in recent_signals)
                is_sell = all(s.final_score < -config.open_th for s in recent_signals)

                if is_buy or is_sell:
                    side = "BUY" if is_buy else "SELL"
                    print(f"🚀 거래 신호 포착 (Lvl:{current_aggr_level}): {symbol} {side}")

                    leverage = position_sizer.get_leverage_for_symbol(symbol, current_aggr_level)
                    entry_atr = confluence_engine.extract_atr(tf_rows, primary_tf='4h') # 4시간봉 ATR을 기준으로
                    quantity = position_sizer.calculate_position_size(symbol, entry_atr, current_aggr_level)

                    if quantity and quantity > 0:
                        final_signal = recent_signals[0]
                        analysis_context = {
                            "symbol": symbol, "final_score": final_signal.final_score,
                            "score_1d": final_signal.score_1d, "score_4h": final_signal.score_4h,
                            "score_1h": final_signal.score_1h, "score_15m": final_signal.score_15m,
                            "atr_1d": final_signal.atr_1d
                        }
                        await trading_engine.place_order(symbol, side, quantity, leverage, entry_atr, analysis_context)
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
