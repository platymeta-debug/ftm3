# 파일명: main.py (전체 수정안)

import discord
from discord import app_commands # 슬래시 명령어를 위한 임포트
from discord.ext import commands, tasks
from binance.client import Client
import asyncio
from datetime import datetime

# 1. 모든 핵심 모듈 임포트
from core.config_manager import config
from core.event_bus import event_bus
from database.manager import db_manager
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
    binance_client.ping()
    print(f"바이낸스 연결 성공. (환경: {config.trade_mode})")
except Exception as e:
    print(f"바이낸스 연결 실패: {e}")
    exit()

trading_engine = TradingEngine(binance_client)
confluence_engine = ConfluenceEngine(binance_client)
position_sizer = PositionSizer(binance_client)
analyzer = PerformanceAnalyzer()

# --- 전역 변수 ---
dashboard_message = None

# --- UI 생성 헬퍼 함수 ---
def create_dashboard_embed() -> discord.Embed:
    embed = discord.Embed(title="📈 실시간 트레이딩 대시보드", color=discord.Color.blue())
    system_status = "🟢 활성" if config.exec_active else "🔴 비활성"
    pnl_today = "+$125.34 (+1.25%)" # Placeholder
    total_equity = "$10,125.34" # Placeholder
    
    embed.add_field(name="시스템 상태", value=system_status, inline=True)
    embed.add_field(name="총 자산", value=total_equity, inline=True)
    embed.add_field(name="금일 손익", value=pnl_today, inline=True)
    
    btc_position = "LONG | 0.1 BTC @ $65,000\n> PnL: +$50.00 (+0.7%)" # Placeholder
    eth_position = "없음" # Placeholder
    
    embed.add_field(name="--- BTCUSDT 포지션 ---", value=btc_position, inline=False)
    embed.add_field(name="--- ETHUSDT 포지션 ---", value=eth_position, inline=False)
    
    embed.set_footer(text=f"마지막 업데이트: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    return embed

# --- 백그라운드 작업 ---
@tasks.loop(seconds=10)
async def dashboard_update_loop():
    global dashboard_message
    channel = bot.get_channel(config.dashboard_channel_id)
    if not channel:
        if dashboard_update_loop.current_loop == 0:
            print(f"경고: 대시보드 채널 ID({config.dashboard_channel_id})를 찾을 수 없습니다.")
        return

    embed = create_dashboard_embed()
    
    if dashboard_message is None:
        dashboard_message = await channel.send(embed=embed)
    else:
        try:
            await dashboard_message.edit(embed=embed)
        except discord.NotFound:
            dashboard_message = await channel.send(embed=embed)

@tasks.loop(seconds=1)
async def event_listener():
    try:
        event = await asyncio.wait_for(event_bus.subscribe(), timeout=1.0)
        channel = bot.get_channel(config.alerts_channel_id)
        if not channel: return

        if event['type'] == 'ORDER_SUCCESS':
            data = event['data']
            embed = discord.Embed(title="✅ 주문 체결 성공", color=discord.Color.green())
            embed.add_field(name="출처", value=f"`{data.get('source', 'N/A')}`", inline=False)
            embed.add_field(name="심볼", value=data.get('symbol'), inline=True)
            embed.add_field(name="방향", value=data.get('side'), inline=True)
            embed.add_field(name="수량", value=data.get('quantity'), inline=True)
            embed.set_footer(text=f"체결 가격: ${data.get('price')}")
            await channel.send(embed=embed)

        elif event['type'] == 'ORDER_FAILURE':
            data = event['data']
            embed = discord.Embed(title="❌ 주문 체결 실패", color=discord.Color.red())
            embed.add_field(name="출처", value=f"`{data.get('source', 'N/A')}`", inline=False)
            embed.add_field(name="오류 메시지", value=f"```{data.get('error')}```", inline=False)
            await channel.send(embed=embed)
        
        event_bus.task_done()
    except asyncio.TimeoutError:
        pass
    except Exception as e:
        print(f"이벤트 리스너 오류: {e}")

@tasks.loop(hours=24)
async def periodic_analysis_report():
    print(f"[{datetime.utcnow().isoformat()}] 일일 성과 분석 리포트 생성 시작...")
    report = analyzer.generate_report()
    channel = bot.get_channel(config.analysis_channel_id)
    if not channel:
        print(f"경고: 분석 채널 ID({config.analysis_channel_id})를 찾을 수 없습니다.")
        return
    if report is None:
        await channel.send("📈 **일일 성과 리포트**\n> 아직 분석할 만큼 충분한 데이터가 쌓이지 않았습니다.")
        return
    embed = discord.Embed(title="📈 일일 성과 분석 리포트", color=discord.Color.purple())
    embed.add_field(name="총 거래 수", value=report['total_trades'], inline=True)
    embed.add_field(name="승률", value=report['win_rate'], inline=True)
    embed.add_field(name="손익비", value=report['profit_factor'], inline=True)
    if report['insights']:
        embed.add_field(name="💡 주요 인사이트", value="\n".join(report['insights']), inline=False)
    embed.set_footer(text="이 리포트는 'CLOSED' 상태의 거래만을 기준으로 합니다.")
    await channel.send(embed=embed)

@tasks.loop(minutes=5)
async def analysis_loop():
    print(f"\n[{datetime.utcnow().isoformat()}] 계층적 컨플루언스 분석 시작...")
    symbol = "BTCUSDT"
    final_score, tf_scores, tf_rows = confluence_engine.analyze(symbol)
    print(f"분석 완료: {symbol} | 최종 점수: {final_score:.2f}")
    print(f"타임프레임별 점수: {tf_scores}")
    open_threshold = config._get_float('OPEN_TH', 10.0)
    side = None
    if final_score > open_threshold:
        side = 'BUY'
    elif final_score < -open_threshold:
        side = 'SELL'
    if side:
        print(f"🚀 거래 신호 발생: {symbol} {side} (점수: {final_score:.2f})")
        atr = confluence_engine.extract_atr(tf_rows)
        quantity = position_sizer.calculate_position_size(symbol, 0, atr)
        analysis_context = {'final_score': final_score, 'tf_scores': tf_scores}
        await trading_engine.place_order(symbol, side, quantity, analysis_context)
    else:
        print("거래 신호 없음 (임계값 미달).")

# --- 봇 준비 이벤트 및 슬래시 명령어 ---
@bot.event
async def on_ready():
    bot.add_view(ControlPanelView())
    await tree.sync()
    print(f'{bot.user.name} 봇이 준비되었습니다. 슬래시 명령어가 동기화되었습니다.')
    print('------------------------------------')
    event_listener.start()
    analysis_loop.start()
    dashboard_update_loop.start()
    periodic_analysis_report.start()

@tree.command(name="panel", description="시스템 제어 패널을 소환합니다.")
@app_commands.is_owner() # 수정된 부분
async def summon_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="⚙️ 시스템 제어 패널", description="아래 버튼과 메뉴를 사용하여 시스템을 제어하세요.", color=discord.Color.dark_gold())
    await interaction.response.send_message(embed=embed, view=ControlPanelView())

@tree.command(name="test_order", description="테스트 주문을 실행하여 이벤트 흐름을 확인합니다.")
@app_commands.is_owner() # 수정된 부분
async def test_order_slash(interaction: discord.Interaction):
    await interaction.response.send_message("테스트 주문 실행을 요청합니다...", ephemeral=True)
    analysis_context = {'final_score': 99.9, 'tf_scores': {'test': 1}} # 테스트용 컨텍스트
    await trading_engine.place_order("BTCUSDT", "BUY", 0.01, analysis_context)

# --- 봇 실행 ---
if __name__ == "__main__":
    if not all([config.discord_bot_token, config.api_key, config.api_secret, config.alerts_channel_id]):
        print("오류:.env 파일에 필수 설정(토큰, API키, 채널ID)이 모두 있는지 확인하세요.")
    else:
        bot.run(config.discord_bot_token)
