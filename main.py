# 파일명: main.py (전체 최종 수정안)

import discord
from discord import app_commands
from discord.ext import commands, tasks
from binance.client import Client
from binance.exceptions import BinanceAPIException
import asyncio
from datetime import datetime, timezone # timezone 임포트 추가

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
    # API 엔드포인트를 직접 지정하여 연결 안정성 확보
    if config.is_testnet:
        # 선물 테스트넷 주소 명시
        binance_client = Client(
            config.api_key, 
            config.api_secret, 
            tld='com', 
            testnet=True
        )
        binance_client.API_URL = "https://testnet.binancefuture.com"
    else:
        # 실거래 서버 주소는 기본값 사용
        binance_client = Client(config.api_key, config.api_secret)

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

# --- 슬래시 명령어 권한 체크 함수 ---
async def is_owner_check(interaction: discord.Interaction) -> bool:
    return await bot.is_owner(interaction.user)

# --- UI 생성 헬퍼 함수 ---
def create_dashboard_embed() -> discord.Embed:
    """실시간 대시보드 임베드를 생성합니다."""
    embed = discord.Embed(title="📈 실시간 트레이딩 대시보드", color=discord.Color.blue())

    try:
        # --- 실제 계좌 정보 조회 ---
        account_info = binance_client.futures_account()
        positions = binance_client.futures_position_information()

        total_balance = float(account_info.get('totalWalletBalance', 0))
        total_pnl = float(account_info.get('totalUnrealizedProfit', 0))

        # 분모가 0이 되는 경우 방지
        effective_balance = total_balance - total_pnl
        pnl_percent = (total_pnl / effective_balance) * 100 if effective_balance != 0 else 0

        system_status = "🟢 활성" if config.exec_active else "🔴 비활성"

        embed.add_field(name="시스템 상태", value=system_status, inline=True)
        embed.add_field(name="총 자산", value=f"${total_balance:,.2f}", inline=True)
        embed.add_field(name="총 미실현손익", value=f"${total_pnl:,.2f} ({pnl_percent:+.2f}%)", inline=True)

        # --- 실제 포지션 정보 조회 ---
        position_map = {pos['symbol']: pos for pos in positions if float(pos.get('positionAmt', 0)) != 0}

        for symbol in config.symbols: #.env에 설정된 심볼들을 순회
            pos_data = position_map.get(symbol)
            if pos_data:
                pos_amt = float(pos_data.get('positionAmt', 0))
                entry_price = float(pos_data.get('entryPrice', 0))
                unrealized_pnl = float(pos_data.get('unRealizedProfit', 0)) # 키 이름 변경
                leverage = float(pos_data.get('leverage', 1))
                side = "LONG" if pos_amt > 0 else "SHORT"

                pos_value = f"**{side}** | {abs(pos_amt)} @ ${entry_price:,.2f}\n" \
                            f"> PnL: **${unrealized_pnl:,.2f}** | 레버리지: {leverage:.0f}x"
            else:
                pos_value = "없음"

            embed.add_field(name=f"--- {symbol} 포지션 ---", value=pos_value, inline=False)

    except BinanceAPIException as e:
        embed.add_field(name="⚠️ 데이터 조회 오류", value=f"API 오류가 발생했습니다: {e}", inline=False)
        embed.set_footer(text="API 키의 권한(읽기, 선물) 또는 IP 설정을 확인해주세요.")
    except Exception as e:
        embed.add_field(name="⚠️ 데이터 조회 오류", value=f"알 수 없는 오류가 발생했습니다: {e}", inline=False)

    embed.timestamp = datetime.now(timezone.utc)
    return embed

# --- 백그라운드 작업 ---
@tasks.loop(minutes=5)
async def analysis_loop():
    print(f"\n 계층적 컨플루언스 분석 시작...")
    
    best_signal = None
    best_score = 0

    for symbol in config.symbols:
        print(f"\n--- {symbol} 분석 중 ---")
        final_score, tf_scores, tf_rows = confluence_engine.analyze(symbol)
        
        print(f"분석 완료: {symbol} | 최종 점수: {final_score:.2f}")
        print(f"타임프레임별 점수: {tf_scores}")

        if abs(final_score) > abs(best_score):
            best_score = final_score
            best_signal = {
                'symbol': symbol,
                'score': final_score,
                'tf_scores': tf_scores,
                'tf_rows': tf_rows
            }
    
    print("\n--- 최종 분석 결과 ---")
    if best_signal:
        print(f"가장 강력한 신호: {best_signal['symbol']} (점수: {best_signal['score']:.2f})")
    else:
        print("유의미한 거래 신호 없음.")
        return

    open_threshold = config.open_threshold
    
    side = None
    if best_score > open_threshold:
        side = 'BUY'
    elif best_score < -open_threshold:
        side = 'SELL'

    if side and config.exec_active:
        print(f"🚀 거래 신호 발생: {best_signal['symbol']} {side} (점수: {best_score:.2f})")
        
        atr = confluence_engine.extract_atr(best_signal['tf_rows'])
        quantity = position_sizer.calculate_position_size(best_signal['symbol'], 0, atr)
        
        if quantity is None or quantity <= 0:
            print("계산된 주문 수량이 유효하지 않아 거래를 건너뜁니다.")
            return

        analysis_context = {'final_score': best_score, 'tf_scores': best_signal['tf_scores']}
        await trading_engine.place_order(best_signal['symbol'], side, quantity, analysis_context)
    else:
        print("거래 신호 없음 (임계값 미달 또는 자동매매 비활성).")

#... (event_listener, periodic_analysis_report, dashboard_update_loop 등 나머지 백그라운드 작업은 이전과 동일하게 유지)
@tasks.loop(seconds=10)
async def dashboard_update_loop():
    #... (이전 코드와 동일)
    pass
@tasks.loop(seconds=1)
async def event_listener():
    #... (이전 코드와 동일)
    pass
@tasks.loop(hours=24)
async def periodic_analysis_report():
    #... (이전 코드와 동일)
    pass

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
@app_commands.check(is_owner_check)
async def summon_panel(interaction: discord.Interaction):
    embed = discord.Embed(title="⚙️ 시스템 제어 패널", description="아래 버튼과 메뉴를 사용하여 시스템을 제어하세요.", color=discord.Color.dark_gold())
    await interaction.response.send_message(embed=embed, view=ControlPanelView())

@tree.command(name="test_order", description="테스트 주문을 실행하여 이벤트 흐름을 확인합니다.")
@app_commands.check(is_owner_check)
async def test_order_slash(interaction: discord.Interaction):
    await interaction.response.send_message("테스트 주문 실행을 요청합니다...", ephemeral=True)
    analysis_context = {'final_score': 99.9, 'tf_scores': {'test': 1}}
    await trading_engine.place_order("BTCUSDT", "BUY", 0.01, analysis_context)

# --- 봇 실행 ---
if __name__ == "__main__":
    if not all([config.discord_bot_token, config.api_key, config.api_secret]):
        print("오류:.env 파일에 필수 설정(DISCORD_BOT_TOKEN, BINANCE API 키)이 모두 있는지 확인하세요.")
    else:
        bot.run(config.discord_bot_token)
