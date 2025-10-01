import asyncio
from datetime import datetime


import discord
from binance.client import Client
from discord.ext import commands, tasks

from core.config_manager import config
from core.event_bus import event_bus
from database.manager import db_manager  # noqa: F401  # Ensures initialization
from execution.trading_engine import TradingEngine
from analysis.confluence_engine import ConfluenceEngine
from risk_management.position_sizer import PositionSizer



intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)


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


@tasks.loop(minutes=5)
async def analysis_loop() -> None:
    """Periodically evaluate confluence scores and trigger trades."""

    print(f"\n[{datetime.utcnow().isoformat()}] 계층적 컨플루언스 분석 시작...")

    for symbol in config.symbols:
        final_score, tf_scores, tf_rows = confluence_engine.analyze(symbol)
        print(f"분석 완료: {symbol} | 최종 점수: {final_score:.2f}")
        print(f"타임프레임별 점수: {tf_scores}")

        side = None
        if final_score > config.open_threshold:
            side = "BUY"
        elif final_score < -config.open_threshold:
            side = "SELL"

        if not side:
            print("거래 신호 없음 (임계값 미달).")
            continue

        print(f"🚀 거래 신호 발생: {symbol} {side} (점수: {final_score:.2f})")
        atr_value = confluence_engine.extract_atr(tf_rows)
        quantity = position_sizer.calculate_position_size(symbol, 0.0, atr_value)
        await trading_engine.place_order(symbol, side, quantity)


@tasks.loop(seconds=1)
async def event_listener() -> None:
    """Listen for events from the event bus and dispatch Discord notifications."""
    try:
        event = await asyncio.wait_for(event_bus.subscribe(), timeout=1.0)
        channel = bot.get_channel(config.alerts_channel_id)
        if not channel:
            print(f"경고: 알림 채널 ID({config.alerts_channel_id})를 찾을 수 없습니다.")
            event_bus.task_done()
            return

        if event["type"] == "ORDER_SUCCESS":
            data = event["data"]
            msg = (
                "✅ **주문 체결 알림**\n"
                f"> {data['side']} {data['symbol']} {data['quantity']} @ ${data['price']}"
            )
            await channel.send(msg)

        event_bus.task_done()
    except asyncio.TimeoutError:
        pass
    except Exception as exc:
        print(f"이벤트 리스너 오류: {exc}")


@bot.event
async def on_ready():
    print(f"{bot.user.name} 봇이 준비되었습니다.")
    print("------------------------------------")
    event_listener.start()
    if not analysis_loop.is_running():
        analysis_loop.start()



@bot.command(name="test_order")
async def test_order(ctx: commands.Context) -> None:
    """Trigger a simulated order to validate the event flow."""
    await ctx.send("테스트 주문 실행을 요청합니다...")
    await trading_engine.place_order("BTCUSDT", "BUY", config.trade_quantity)



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
