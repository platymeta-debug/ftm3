# main.py (V2: 분석/매매 로직 분리)

import discord
from discord import app_commands
from discord.ext import commands, tasks
from binance.client import Client
from binance.exceptions import BinanceAPIException
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select

# 1. 모든 핵심 모듈 임포트
from core.config_manager import config
from core.event_bus import event_bus
from database.manager import db_manager, Signal, Trade
from execution.trading_engine import TradingEngine
from analysis.confluence_engine import ConfluenceEngine
from risk_management.position_sizer import PositionSizer
from ui.views import ControlPanelView
from analysis.performance_analyzer import PerformanceAnalyzer

# 2. 각 모듈 초기화
intents = discord.Intents.default()
intents.message_content = True
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

trading_engine = TradingEngine(binance_client)
confluence_engine = ConfluenceEngine(binance_client)
position_sizer = PositionSizer(binance_client)
analyzer = PerformanceAnalyzer()

# --- 대시보드 함수 (오류 수정) ---
def create_dashboard_embed() -> discord.Embed:
    embed = discord.Embed(title="📈 실시간 트레이딩 대시보드", color=discord.Color.blue())
    try:
        account_info = binance_client.futures_account()
        positions = binance_client.futures_position_information() # 라이브러리 버전 문제 해결
        # ... (이하 대시보드 코드는 기존과 동일)
    except Exception as e:
        embed.add_field(name="⚠️ 데이터 조회 오류", value=f"알 수 없는 오류가 발생했습니다: {e}", inline=False)
    # ... (이하 대시보드 코드는 기존과 동일)
    return embed

# --- V2 백그라운드 작업 ---

@tasks.loop(minutes=1)
async def data_collector_loop():
    """[데이터 수집가] 1분마다 시장을 분석하고 결과를 DB에 저장합니다."""
    print(f"\n--- [Data Collector] 분석 시작 ---")
    session = db_manager.get_session()
    try:
        for symbol in config.symbols:
            final_score, tf_scores, tf_rows = confluence_engine.analyze(symbol)
            print(f"분석 완료: {symbol} | 최종 점수: {final_score:.2f}")

            new_signal = Signal(
                symbol=symbol,
                final_score=final_score,
                score_1d=tf_scores.get("1d"),
                score_4h=tf_scores.get("4h"),
                score_1h=tf_scores.get("1h"),
                score_15m=tf_scores.get("15m"),
            )
            session.add(new_signal)
        session.commit()
    except Exception as e:
        print(f"🚨 데이터 수집 중 오류: {e}")
        session.rollback()
    finally:
        session.close()


@tasks.loop(minutes=5)
async def trading_decision_loop():
    """[트레이딩 결정가] 5분마다 DB 데이터를 기반으로 매매를 결정합니다."""
    if not config.exec_active:
        print("--- [Trading Decision] 자동매매 비활성 상태 ---")
        return

    print(f"\n--- [Trading Decision] 매매 결정 시작 ---")
    session = db_manager.get_session()
    try:
        # 현재 오픈된 포지션이 있는지 확인
        open_trade = session.execute(select(Trade).where(Trade.status == "OPEN")).scalar_one_or_none()

        if open_trade:
            # --- B. 포지션이 있을 경우 (청산 결정) ---
            print(f"오픈된 포지션 관리 중: {open_trade.symbol} {open_trade.side}")
            pnl_pct = 0.0
            current_price_info = binance_client.futures_mark_price(symbol=open_trade.symbol)
            current_price = float(current_price_info['markPrice'])

            if open_trade.side == "BUY":
                pnl_pct = (current_price - open_trade.entry_price) / open_trade.entry_price
            else: # SELL
                pnl_pct = (open_trade.entry_price - current_price) / open_trade.entry_price

            # 조건 1: 수익 실현
            if pnl_pct >= config.take_profit_pct:
                await trading_engine.close_position(open_trade, f"수익 실현 ({pnl_pct:+.2%})")
                return

            # 조건 2: 손절
            if pnl_pct <= -config.stop_loss_pct:
                await trading_engine.close_position(open_trade, f"손절 ({pnl_pct:+.2%})")
                return

            # 조건 3: 추세 반전
            lookback_time = datetime.utcnow() - timedelta(minutes=10)
            recent_signals = session.execute(
                select(Signal)
                .where(Signal.symbol == open_trade.symbol)
                .where(Signal.timestamp >= lookback_time)
                .order_by(Signal.timestamp.desc())
            ).scalars().all()

            reversal_signals = 0
            for signal in recent_signals:
                is_buy_signal = signal.final_score > config.open_threshold
                is_sell_signal = signal.final_score < -config.open_threshold
                if (open_trade.side == "BUY" and is_sell_signal) or \
                   (open_trade.side == "SELL" and is_buy_signal):
                    reversal_signals += 1
                else:
                    break # 연속된 반대 신호만 카운트

            if reversal_signals >= config.reversal_confirm_count:
                await trading_engine.close_position(open_trade, f"추세 반전 감지 ({reversal_signals}회)")
                return

            print(f"포지션 유지. 현재 PnL: {pnl_pct:+.2%}")

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
                    .limit(config.entry_confirm_count)
                ).scalars().all()

                if len(recent_signals) < config.entry_confirm_count:
                    continue # 데이터 부족

                # 모든 신호가 매수/매도 임계값을 넘었는지 확인
                is_buy_condition = all(s.final_score > config.open_threshold for s in recent_signals)
                is_sell_condition = all(s.final_score < -config.open_threshold for s in recent_signals)

                if is_buy_condition or is_sell_condition:
                    side = "BUY" if is_buy_condition else "SELL"
                    print(f"🚀 거래 신호 발생: {symbol} {side} ({config.entry_confirm_count}회 연속)")

                    final_signal = recent_signals[0]
                    atr_row = {"ATR_14": confluence_engine.extract_atr({final_signal.timestamp.isoformat(): final_signal.tf_rows})}
                    quantity = position_sizer.calculate_position_size(symbol, 0, atr_row["ATR_14"])

                    if quantity and quantity > 0:
                        analysis_context = {'final_score': final_signal.final_score, 'tf_scores': {
                            '1d': final_signal.score_1d, '4h': final_signal.score_4h,
                            '1h': final_signal.score_1h, '15m': final_signal.score_15m
                        }}
                        await trading_engine.place_order(symbol, side, quantity, analysis_context)
                        return # 한 번에 하나의 포지션만 진입

    except Exception as e:
        print(f"🚨 매매 결정 중 오류: {e}")
    finally:
        session.close()


# --- 봇 준비 이벤트 및 나머지 코드 ---
@bot.event
async def on_ready():
    bot.add_view(ControlPanelView())
    await tree.sync()
    print(f'{bot.user.name} 봇이 준비되었습니다.')
    print('------------------------------------')
    # 기존 루프를 새로운 루프로 교체
    data_collector_loop.start()
    await asyncio.sleep(5) # 데이터가 먼저 쌓일 시간을 줍니다.
    trading_decision_loop.start()
    # dashboard_update_loop.start() # 필요 시 활성화
    # periodic_analysis_report.start() # 필요 시 활성화

# ... (summon_panel, test_order_slash, 봇 실행 코드는 기존과 동일)
