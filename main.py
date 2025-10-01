import asyncio
from datetime import datetime
from typing import Optional


import discord
from binance.client import Client
from discord import app_commands
from discord.ext import commands, tasks

from core.config_manager import config
from core.event_bus import event_bus
from database.manager import db_manager  # noqa: F401 - ensure initialization side-effects
from execution.trading_engine import TradingEngine
from analysis.confluence_engine import ConfluenceEngine
from analysis.performance_analyzer import PerformanceAnalyzer
from risk_management.position_sizer import PositionSizer
from ui.views import ControlPanelView


intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


def _create_binance_client() -> Client:
    try:
        client = Client(config.api_key, config.api_secret, testnet=config.is_testnet)
        client.ping()
        print(f"바이낸스 연결 성공. (환경: {config.trade_mode})")
        return client
    except Exception as exc:  # pragma: no cover - initialization guard
        print(f"바이낸스 연결 실패: {exc}")
        raise


try:
    binance_client = _create_binance_client()
except Exception:
    exit()

trading_engine = TradingEngine(binance_client)
confluence_engine = ConfluenceEngine(binance_client)
position_sizer = PositionSizer(binance_client)
analyzer = PerformanceAnalyzer()


dashboard_message: Optional[discord.Message] = None


def create_dashboard_embed() -> discord.Embed:
    """실시간 대시보드 임베드를 생성합니다."""
    embed = discord.Embed(title="📈 실시간 트레이딩 대시보드", color=discord.Color.blue())

    system_status = "🟢 활성" if config.exec_active else "🔴 비활성"
    pnl_today = "+$125.34 (+1.25%)"
    total_equity = "$10,125.34"

    embed.add_field(name="시스템 상태", value=system_status, inline=True)
    embed.add_field(name="총 자산", value=total_equity, inline=True)
    embed.add_field(name="금일 손익", value=pnl_today, inline=True)

    btc_position = "LONG | 0.1 BTC @ $65,000\n> PnL: +$50.00 (+0.7%)"
    eth_position = "없음"

    embed.add_field(name="--- BTCUSDT 포지션 ---", value=btc_position, inline=False)
    embed.add_field(name="--- ETHUSDT 포지션 ---", value=eth_position, inline=False)

    embed.set_footer(text=f"마지막 업데이트: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    return embed


@tasks.loop(seconds=10)
async def dashboard_update_loop() -> None:
    """10초마다 대시보드를 업데이트합니다."""
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
async def event_listener() -> None:
    """이벤트 큐를 확인하고, 알림 임베드를 전송합니다."""
    try:
        event = await asyncio.wait_for(event_bus.subscribe(), timeout=1.0)
        channel = bot.get_channel(config.alerts_channel_id)
        if not channel:
            return

        event_type = event.get("type")
        data = event.get("data", {})

        if event_type == "ORDER_SUCCESS":
            response = data.get("response", data)
            source = data.get("source", "SYSTEM")
            embed = discord.Embed(title="✅ 주문 체결 성공", color=discord.Color.green())
            embed.add_field(name="출처", value=f"`{source}`", inline=False)
            embed.add_field(name="심볼", value=response.get("symbol", "-"), inline=True)
            embed.add_field(name="방향", value=response.get("side", "-"), inline=True)
            embed.add_field(name="수량", value=str(response.get("origQty", response.get("quantity", "-"))), inline=True)
            if "orderId" in response:
                embed.set_footer(text=f"바이낸스 ID: {response['orderId']}")
            await channel.send(embed=embed)

        elif event_type == "ORDER_FAILURE":
            params = data.get("params", {})
            source = data.get("source", "SYSTEM")
            embed = discord.Embed(title="❌ 주문 체결 실패", color=discord.Color.red())
            embed.add_field(name="출처", value=f"`{source}`", inline=False)
            embed.add_field(
                name="요청 내용",
                value=f"{params.get('side', '-') } {params.get('symbol', '-') } {params.get('quantity', '-')}",
                inline=False,
            )
            embed.add_field(name="오류 메시지", value=f"```{data.get('error', 'Unknown error')}```", inline=False)
            await channel.send(embed=embed)

        elif event_type == "PANIC_SIGNAL":
            user = data.get("user", "알 수 없음")
            embed = discord.Embed(title="🚨 긴급 청산 요청", color=discord.Color.orange())
            embed.description = f"{user} 님이 긴급 청산을 요청했습니다."
            await channel.send(embed=embed)

        event_bus.task_done()
    except asyncio.TimeoutError:
        pass
    except Exception as exc:
        print(f"이벤트 리스너 오류: {exc}")


@tasks.loop(minutes=5)
async def analysis_loop() -> None:
    """주기적으로 분석 스냅샷을 게시합니다."""
    channel = bot.get_channel(config.analysis_channel_id)
    if not channel:
        if analysis_loop.current_loop == 0:
            print(f"경고: 분석 채널 ID({config.analysis_channel_id})를 찾을 수 없습니다.")
        return

    snapshot = await confluence_engine.build_snapshot()
    embed = discord.Embed(title="🧠 컨플루언스 리포트", color=discord.Color.purple())
    embed.add_field(name="요약", value=snapshot.get("summary", "데이터 없음"), inline=False)

    signals = snapshot.get("signals", [])
    if signals:
        formatted_lines = []
        for item in signals:
            symbol = item.get("symbol", "-")
            confidence = float(item.get("confidence", 0))
            direction = item.get("direction", "-")
            suggested = position_sizer.recommend_size(symbol, confidence)
            formatted_lines.append(
                f"• {symbol}: {direction} (신뢰도 {confidence:.0%}, 추천 수량 {suggested})"
            )
        formatted = "\n".join(formatted_lines)
    else:
        formatted = "신호가 없습니다."
    embed.add_field(name="시그널", value=formatted, inline=False)

    embed.set_footer(text=f"업데이트: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    await channel.send(embed=embed)


@tasks.loop(hours=24)
async def periodic_analysis_report() -> None:
    """누적 거래 데이터를 기반으로 일일 성과 리포트를 전송합니다."""
    print(f"[{datetime.utcnow().isoformat()}] 일일 성과 분석 리포트 생성 시작...")

    channel = bot.get_channel(config.analysis_channel_id)
    if not channel:
        print(f"경고: 분석 채널 ID({config.analysis_channel_id})를 찾을 수 없습니다.")
        return

    report = analyzer.generate_report()
    if report is None:
        await channel.send(
            "📈 **일일 성과 리포트**\n> 아직 분석할 만큼 충분한 데이터가 쌓이지 않았습니다."
        )
        return

    embed = discord.Embed(title="📈 일일 성과 분석 리포트", color=discord.Color.purple())
    embed.add_field(name="총 거래 수", value=report["total_trades"], inline=True)
    embed.add_field(name="승률", value=report["win_rate"], inline=True)
    embed.add_field(name="손익비", value=report["profit_factor"], inline=True)

    insights = report.get("insights", [])
    if insights:
        embed.add_field(name="💡 주요 인사이트", value="\n".join(insights), inline=False)

    embed.set_footer(text="이 리포트는 'CLOSED' 상태의 거래만을 기준으로 합니다.")
    await channel.send(embed=embed)


@bot.event
async def on_ready() -> None:
    bot.add_view(ControlPanelView())
    await tree.sync()
    print(f"{bot.user.name} 봇이 준비되었습니다. 슬래시 명령어가 동기화되었습니다.")
    print("------------------------------------")
    event_listener.start()
    analysis_loop.start()
    dashboard_update_loop.start()
    periodic_analysis_report.start()


@tree.command(name="panel", description="시스템 제어 패널을 소환합니다.")
@app_commands.checks.is_owner()
async def summon_panel(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="⚙️ 시스템 제어 패널",
        description="아래 버튼과 메뉴를 사용하여 시스템을 제어하세요.",
        color=discord.Color.dark_gold(),
    )
    await interaction.response.send_message(embed=embed, view=ControlPanelView())


@tree.command(name="test_order", description="테스트 주문을 실행하여 이벤트 흐름을 확인합니다.")
@app_commands.checks.is_owner()
async def test_order_slash(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("테스트 주문 실행을 요청합니다...", ephemeral=True)
    await trading_engine.place_order(
        "BTCUSDT",
        "BUY",
        0.01,
        {"final_score": 0.0, "tf_scores": {}},
    )



if __name__ == "__main__":
    required = [
        config.discord_bot_token,
        config.api_key,
        config.api_secret,
        config.alerts_channel_id,
    ]
    if not all(required):
        print("오류:.env 파일에 필수 설정(토큰, API키, 채널ID)이 모두 있는지 확인하세요.")
    else:
        bot.run(config.discord_bot_token)
