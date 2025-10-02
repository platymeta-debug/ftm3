import discord
from discord import app_commands
from discord.ext import commands, tasks
from binance.client import Client
from binance.exceptions import BinanceAPIException
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
import pandas as pd
from enum import Enum

# 1. 모듈 임포트
from core.config_manager import config
from core.event_bus import event_bus
from database.manager import db_manager, Signal, Trade
from execution.trading_engine import TradingEngine
from analysis.confluence_engine import ConfluenceEngine
from risk_management.position_sizer import PositionSizer
from ui.views import ControlPanelView, ConfirmView # ConfirmView 임포트 추가

# 2. 초기화 (기존과 동일)
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

# 3. 엔진 및 전역 변수 초기화
trading_engine = TradingEngine(binance_client)
confluence_engine = ConfluenceEngine(binance_client)
position_sizer = PositionSizer(binance_client)

current_aggr_level = config.aggr_level
panel_message: discord.Message = None
analysis_message: discord.Message = None # 분석 메시지 객체

# --- 콜백 및 UI 생성 함수 (기존과 동일) ---
def on_aggr_level_change(new_level: int):
    # ... (기존과 동일)
    pass

def get_panel_embed() -> discord.Embed:
    # ... (기존과 동일)
    pass

# --- V3 백그라운드 작업 ---

@tasks.loop(seconds=15)
async def panel_update_loop():
    # ... (기존과 동일)
    pass


def generate_sparkline(scores: list) -> str:
    """점수 리스트로 텍스트 스파크라인 차트를 생성합니다."""
    if not scores: return ""
    bar_chars = [' ', '▂', '▃', '▄', '▅', '▆', '▇', '█']
    min_score, max_score = min(scores), max(scores)
    score_range = max_score - min_score if max_score > min_score else 1
    
    sparkline = []
    for score in scores:
        index = int((score - min_score) / score_range * (len(bar_chars) - 1))
        sparkline.append(bar_chars[index])
        
    trend_emoji = "📈" if scores[-1] > scores[0] else "📉" if scores[-1] < scores[0] else "➡️"
    return "".join(sparkline) + f" {scores[-1]:.1f} {trend_emoji}"


def get_analysis_embed(session) -> discord.Embed:
    """'라이브 종합 상황판' Embed를 생성합니다."""
    embed = discord.Embed(title="📊 라이브 종합 상황판", color=0x4A90E2)
    
    for symbol in config.symbols:
        # 시장 체제 진단
        market_regime = diagnose_market_regime(session, symbol)
        
        # 스코어 흐름 (최근 10분)
        lookback_time = datetime.utcnow() - timedelta(minutes=10)
        recent_signals = session.execute(
            select(Signal.final_score)
            .where(Signal.symbol == symbol, Signal.timestamp >= lookback_time)
            .order_by(Signal.timestamp.asc())
        ).scalars().all()
        
        sparkline = generate_sparkline(recent_signals)
        
        # 현재 분석 스냅샷
        latest_signal = session.execute(select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc())).scalar_one_or_none()
        score_text = f"**{latest_signal.final_score:.2f}**" if latest_signal else "N/A"
        
        embed.add_field(
            name=f"{symbol} | {market_regime.value}",
            value=f"스코어 흐름: `{sparkline}`\n현재 점수: {score_text}",
            inline=False
        )
    
    embed.set_footer(text=f"최종 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return embed


@tasks.loop(minutes=1)
async def data_collector_loop():
    global analysis_message
    # ... (기존 데이터 수집 로직은 동일)
    
    # --- ▼▼▼ [Discord V3] 분석 상황판 업데이트 로직 ▼▼▼ ---
    try:
        analysis_channel = bot.get_channel(config.analysis_channel_id)
        if not analysis_channel: return

        with db_manager.get_session() as session:
            analysis_embed = get_analysis_embed(session)

        if analysis_message:
            await analysis_message.edit(embed=analysis_embed)
        else:
            # 기존 메시지 탐색 또는 새로 생성
            async for msg in analysis_channel.history(limit=5):
                if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "📊 라이브- 종합 상황판":
                    analysis_message = msg
                    await analysis_message.edit(embed=analysis_embed)
                    return
            analysis_message = await analysis_channel.send(embed=analysis_embed)
    except Exception as e:
        print(f"🚨 분석 상황판 업데이트 중 오류: {e}")
    # --- ▲▲▲ [Discord V3] 분석 상황판 업데이트 로직 ▲▲▲ ---


@tasks.loop(minutes=5)
async def trading_decision_loop():
    # ... (기존 매매 결정 로직은 그대로 유지)
    pass


# --- 한글 슬래시 명령어 (V3) ---

@tree.command(name="패널", description="인터랙티브 제어실을 소환합니다.")
async def summon_panel_kr(interaction: discord.Interaction):
    # ... (기존과 동일)
    pass


@tree.command(name="상태", description="봇의 현재 핵심 상태를 비공개로 요약합니다.")
async def status_kr(interaction: discord.Interaction):
    embed = get_panel_embed()
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="매수", description="지정한 코인을 즉시 시장가로 매수(LONG)합니다.")
@app_commands.describe(코인="매수할 코인 심볼 (예: BTCUSDT)", 수량="주문할 수량 (예: 0.01)")
async def manual_buy_kr(interaction: discord.Interaction, 코인: str, 수량: float):
    symbol = 코인.upper()
    view = ConfirmView()
    await interaction.response.send_message(f"**⚠️ 경고: 수동 주문**\n`{symbol}`을(를) `{수량}` 만큼 시장가 매수(LONG) 하시겠습니까?", view=view, ephemeral=True)
    await view.wait()
    if view.value:
        # trading_engine에 수동 주문 기능이 필요. 임시로 직접 호출
        try:
            order = binance_client.futures_create_order(symbol=symbol, side='BUY', type='MARKET', quantity=수량)
            await interaction.followup.send(f"✅ **수동 매수 주문 성공**\n`{symbol}` {수량} @ ${order.get('avgPrice', 'N/A')}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ **수동 매수 주문 실패**\n`{e}`", ephemeral=True)


@tree.command(name="매도", description="지정한 코인을 즉시 시장가로 매도(SHORT)합니다.")
@app_commands.describe(코인="매도할 코인 심볼 (예: BTCUSDT)", 수량="주문할 수량 (예: 0.01)")
async def manual_sell_kr(interaction: discord.Interaction, 코인: str, 수량: float):
    symbol = 코인.upper()
    view = ConfirmView()
    await interaction.response.send_message(f"**⚠️ 경고: 수동 주문**\n`{symbol}`을(를) `{수량}` 만큼 시장가 매도(SHORT) 하시겠습니까?", view=view, ephemeral=True)
    await view.wait()
    if view.value:
        try:
            order = binance_client.futures_create_order(symbol=symbol, side='SELL', type='MARKET', quantity=수량)
            await interaction.followup.send(f"✅ **수동 매도 주문 성공**\n`{symbol}` {수량} @ ${order.get('avgPrice', 'N/A')}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ **수동 매도 주문 실패**\n`{e}`", ephemeral=True)


@tree.command(name="청산", description="보유 중인 특정 코인의 포지션을 즉시 청산합니다.")
@app_commands.describe(코인="청산할 코인 심볼 (예: BTCUSDT)")
async def close_position_kr(interaction: discord.Interaction, 코인: str):
    symbol = 코인.upper()
    # ... (DB에서 해당 심볼의 open_trade를 찾아 trading_engine.close_position 호출하는 로직)
    await interaction.response.send_message(f"`{symbol}` 포지션 청산 기능은 구현 예정입니다.", ephemeral=True)


# ... (on_ready, 봇 실행 코드는 기존과 동일하게 유지)
# on_ready에서 data_collector_loop, trading_decision_loop를 start() 해야 합니다.
