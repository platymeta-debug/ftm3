# cogs/commands.py

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from database.manager import db_manager
from database.models import Trade
from execution.trading_engine import TradingEngine
from ui.views import ConfirmView

class CommandCog(commands.Cog):
    def __init__(self, bot: commands.Bot, trading_engine: TradingEngine):
        self.bot = bot
        self.trading_engine = trading_engine

    @app_commands.command(name="패널", description="인터랙티브 제어실을 소환합니다.")
    async def summon_panel_kr(self, interaction: discord.Interaction):
        # 이 명령어는 이제 main.py의 on_ready에서 자동으로 패널을 생성하므로
        # 수동 호출 시에는 안내 메시지만 보내는 것이 더 안정적입니다.
        await interaction.response.send_message(
            f"✅ 제어 패널은 봇 시작 시 자동으로 생성됩니다. <#{self.bot.config.panel_channel_id}> 채널을 확인해주세요.",
            ephemeral=True
        )

    @app_commands.command(name="상태", description="봇의 현재 핵심 상태를 비공개로 요약합니다.")
    async def status_kr(self, interaction: discord.Interaction):
        # main.py에 있는 get_panel_embed 함수를 호출합니다.
        # on_ready에서 bot 객체에 함수를 할당해두었기 때문에 접근 가능합니다.
        if hasattr(self.bot, 'get_panel_embed'):
            embed = self.bot.get_panel_embed()
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 패널 정보를 가져올 수 없습니다. 봇이 아직 준비 중일 수 있습니다.", ephemeral=True)


    @app_commands.command(name="매수", description="지정한 코인을 즉시 시장가로 매수(LONG)합니다.")
    @app_commands.describe(코인="매수할 코인 심볼 (예: BTCUSDT)", 수량="주문할 수량 (예: 0.01)")
    async def manual_buy_kr(self, interaction: discord.Interaction, 코인: str, 수량: float):
        symbol = 코인.upper()
        view = ConfirmView()
        await interaction.response.send_message(f"**⚠️ 경고: 수동 주문**\n`{symbol}`을(를) `{수량}` 만큼 시장가 매수(LONG) 하시겠습니까?", view=view, ephemeral=True)
        await view.wait()
        if view.value:
            try:
                order = self.bot.binance_client.futures_create_order(symbol=symbol, side='BUY', type='MARKET', quantity=수량)
                await interaction.followup.send(f"✅ **수동 매수 주문 성공**\n`{symbol}` {수량} @ `${float(order.get('avgPrice', 0)):.2f}`", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ **수동 매수 주문 실패**\n`{e}`", ephemeral=True)


    @app_commands.command(name="매도", description="지정한 코인을 즉시 시장가로 매도(SHORT)합니다.")
    @app_commands.describe(코인="매도할 코인 심볼 (예: BTCUSDT)", 수량="주문할 수량 (예: 0.01)")
    async def manual_sell_kr(self, interaction: discord.Interaction, 코인: str, 수량: float):
        symbol = 코인.upper()
        view = ConfirmView()
        await interaction.response.send_message(f"**⚠️ 경고: 수동 주문**\n`{symbol}`을(를) `{수량}` 만큼 시장가 매도(SHORT) 하시겠습니까?", view=view, ephemeral=True)
        await view.wait()
        if view.value:
            try:
                order = self.bot.binance_client.futures_create_order(symbol=symbol, side='SELL', type='MARKET', quantity=수량)
                await interaction.followup.send(f"✅ **수동 매도 주문 성공**\n`{symbol}` {수량} @ `${float(order.get('avgPrice', 0)):.2f}`", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"❌ **수동 매도 주문 실패**\n`{e}`", ephemeral=True)


    @app_commands.command(name="청산", description="보유 중인 특정 코인의 포지션을 즉시 청산합니다.")
    @app_commands.describe(코인="청산할 코인 심볼 (예: BTCUSDT)")
    async def close_position_kr(self, interaction: discord.Interaction, 코인: str):
        symbol = 코인.upper()
        view = ConfirmView()
        await interaction.response.send_message(f"**⚠️ 경고: 수동 청산**\n`{symbol}` 포지션을 즉시 시장가로 종료하시겠습니까?", view=view, ephemeral=True)
        await view.wait()
        if view.value is True:
            try:
                # trading_engine의 close_all_positions는 모든 포지션을 닫으므로, 특정 심볼만 닫는 로직이 필요
                # 여기서는 간단하게 trading_engine을 통해 직접 구현합니다.
                with db_manager.get_session() as session:
                    trade_to_close = session.execute(select(Trade).where(Trade.symbol == symbol, Trade.status == "OPEN")).scalar_one_or_none()
                
                if trade_to_close:
                    await self.trading_engine.close_position(trade_to_close, "사용자 수동 청산")
                    await interaction.followup.send(f"✅ **수동 청산 주문 성공**\n`{symbol}` 포지션이 종료되었습니다.", ephemeral=True)
                else:
                    # DB에 없는 포지션 강제 청산
                    positions = self.bot.binance_client.futures_position_information()
                    target_pos = next((p for p in positions if p.get('symbol') == symbol and float(p.get('positionAmt', 0)) != 0), None)
                    if target_pos:
                        quantity = abs(float(target_pos['positionAmt']))
                        side = "BUY" if float(target_pos['positionAmt']) < 0 else "SELL"
                        self.bot.binance_client.futures_create_order(symbol=symbol, side=side, type='MARKET', quantity=quantity)
                        await interaction.followup.send(f"✅ **수동 강제 청산 주문 성공**\n`{symbol}` 포지션이 종료되었습니다.", ephemeral=True)
                    else:
                        await interaction.followup.send(f"❌ **수동 청산 실패**\n`{symbol}`에 대한 오픈된 포지션을 찾을 수 없습니다.", ephemeral=True)

            except Exception as e:
                await interaction.followup.send(f"❌ **수동 청산 주문 실패**\n`{e}`", ephemeral=True)

async def setup(bot: commands.Bot):
    trading_engine = bot.trading_engine
    await bot.add_cog(CommandCog(bot, trading_engine))
