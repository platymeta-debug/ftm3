# cogs/commands.py (최종 수정본)

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from binance.client import Client
import asyncio

# ▼▼▼ [수정] 프로젝트 경로 설정 및 FractionalBacktest 임포트 ▼▼▼
import sys
import os
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from backtesting.lib import FractionalBacktest # FractionalBacktest 임포트
# ▲▲▲ [수정] ▲▲▲

from local_backtesting.backtest_runner import StrategyRunner
from local_backtesting.performance_visualizer import create_performance_report
from analysis.data_fetcher import fetch_klines
from database.models import Trade
from execution.trading_engine import TradingEngine
from ui.views import ConfirmView

class CommandCog(commands.Cog):
    def __init__(self, bot: commands.Bot, trading_engine: TradingEngine):
        self.bot = bot
        self.trading_engine = trading_engine

    @app_commands.command(name="성과", description="지정한 코인에 대한 전략 백테스팅을 실행하고 결과를 시각화합니다.")
    @app_commands.describe(코인="백테스팅을 실행할 코인 심볼 (예: BTCUSDT)")
    async def run_backtest_kr(self, interaction: discord.Interaction, 코인: str):
        symbol = 코인.upper()
        
        try:
            await interaction.response.defer(ephemeral=False, thinking=True)
            
            print(f"[/성과] 1. '{symbol}' 백테스팅 시작... 현재 계좌 잔고 조회 중...")
            try:
                account_info = self.bot.binance_client.futures_account()
                initial_cash = float(account_info.get('totalWalletBalance', 10000))
                print(f"[/성과] 현재 총 자산: ${initial_cash:,.2f}")
            except Exception as e:
                print(f"⚠️ 계좌 정보 조회 실패: {e}. 기본 자본금($10,000)으로 시작합니다.")
                initial_cash = 10_000

            loop = asyncio.get_event_loop()

            backtest_client = Client(self.bot.config.api_key, self.bot.config.api_secret, testnet=self.bot.config.is_testnet)
            if self.bot.config.is_testnet:
                backtest_client.FUTURES_URL = 'https://testnet.binancefuture.com'
            
            klines_data = await loop.run_in_executor(
                None, fetch_klines, backtest_client, symbol, "1d", 500
            )

            if klines_data is None or klines_data.empty:
                await interaction.followup.send(f"❌ `{symbol}`의 과거 데이터를 가져오는 데 실패했습니다.")
                return

            print(f"[/성과] 2. 데이터 로드 성공. (총 {len(klines_data)}개 캔들)")
            klines_data.columns = [col.capitalize() for col in klines_data.columns]

            def run_bt():
                print("[/성과] 3. 백테스팅 라이브러리 실행 직전...")
                strategy_params = self.bot.config.get_strategy_params(symbol)
                StrategyRunner.open_threshold = strategy_params.get("open_th", 12.0)
                StrategyRunner.risk_reward_ratio = strategy_params.get("risk_reward_ratio")
                StrategyRunner.symbol = symbol # 심볼 전달
                print(f"[/성과] '{symbol}' 테스트 파라미터: Threshold={StrategyRunner.open_threshold}, R/R Ratio={StrategyRunner.risk_reward_ratio}")
                
                # ▼▼▼ [수정] FractionalBacktest 사용 및 레버리지 적용 ▼▼▼
                bt = FractionalBacktest(klines_data, StrategyRunner, cash=initial_cash, commission=.002, margin=1/10)
                # ▲▲▲ [수정] ▲▲▲
                
                stats = bt.run()
                print("[/성과] 4. 백테스팅 실행 완료.")
                return stats

            stats = await loop.run_in_executor(None, run_bt)
            
            print("[/성과] 5. 리포트 생성 시작...")
            # ▼▼▼ [수정] create_performance_report에 initial_cash 전달 ▼▼▼
            report_text, chart_buffer = create_performance_report(stats, initial_cash)
            # ▲▲▲ [수정] ▲▲▲
            print("[/성과] 6. 리포트 생성 완료.")

            if chart_buffer:
                file = discord.File(chart_buffer, filename=f"{symbol}_performance.png")
                await interaction.followup.send(content=report_text, file=file)
            else:
                await interaction.followup.send(content=report_text)
            
            print(f"[/성과] 7. '{symbol}' 결과 전송 완료.")

        except Exception as e:
            import traceback
            print(f"🚨 [/성과] 명령어 처리 중 심각한 예외 발생:")
            traceback.print_exc()
            if interaction.response.is_done():
                await interaction.followup.send(f"🚨 백테스팅 실행 중 오류가 발생했습니다: `{e}`")
        # ▲▲▲ [최종 수정] ▲▲▲

    # ... (이하 다른 명령어들은 그대로 유지)
    @app_commands.command(name="패널", description="인터랙티브 제어실을 소환합니다.")
    async def summon_panel_kr(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"✅ 제어 패널은 봇 시작 시 자동으로 생성됩니다. <#{self.bot.config.panel_channel_id}> 채널을 확인해주세요.",
            ephemeral=True
        )

    @app_commands.command(name="상태", description="봇의 현재 핵심 상태를 비공개로 요약합니다.")
    async def status_kr(self, interaction: discord.Interaction):
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
                with self.bot.db_manager.get_session() as session:
                    trade_to_close = session.execute(select(Trade).where(Trade.symbol == symbol, Trade.status == "OPEN")).scalar_one_or_none()
                if trade_to_close:
                    await self.trading_engine.close_position(trade_to_close, "사용자 수동 청산")
                    await interaction.followup.send(f"✅ **수동 청산 주문 성공**\n`{symbol}` 포지션이 종료되었습니다.", ephemeral=True)
                else:
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
