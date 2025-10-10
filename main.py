# main.py (최종 수정)

import discord
from discord.ext import commands
from binance.client import Client
import asyncio
import os
from dotenv import load_dotenv

# 1. 핵심 모듈 임포트 (클래스만 가져오기)
from core.config_manager import ConfigManager
from execution.trading_engine import TradingEngine
from analysis.confluence_engine import ConfluenceEngine
from risk_management.position_sizer import PositionSizer
from core.tasks import BackgroundTasks
from ui.views import ControlPanelView
from database.manager import DatabaseManager

# 2. .env 로드 및 객체 생성 순서 명확화
intents = discord.Intents.default()
intents.message_content = True

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__)))
dotenv_path = os.path.join(project_root, '.env')
load_dotenv(dotenv_path)

config = ConfigManager()
db_manager = DatabaseManager(config)

class FTM3Bot(commands.Bot):
    """모든 핵심 엔진과 백그라운드 작업을 관리하는 커스텀 봇 클래스"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config
        self.db_manager = db_manager

        try:
            self.binance_client = Client(self.config.api_key, self.config.api_secret, testnet=self.config.is_testnet)
            if self.config.is_testnet:
                self.binance_client.FUTURES_URL = 'https://testnet.binancefuture.com'
            self.binance_client.ping()
            print(f"✅ 바이낸스 연결 성공. (환경: {self.config.trade_mode})")
        except Exception as e:
            print(f"🚨 바이낸스 연결 실패: {e}")
            raise RuntimeError("Binance connection failed") from e

        self.position_sizer = PositionSizer(self.binance_client, self.config)
        self.trading_engine = TradingEngine(self.binance_client, self.config, self.db_manager)
        self.confluence_engine = ConfluenceEngine(self.binance_client, self.config)
        self.background_tasks = BackgroundTasks(self, self.db_manager)
        self.get_panel_embed = self.background_tasks.get_panel_embed

bot = FTM3Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    """봇이 디스코드에 성공적으로 로그인하고 모든 준비를 마쳤을 때 호출됩니다."""
    await bot.load_extension("cogs.commands")
    await bot.tree.sync()
    print(f"✅ {bot.user.name} 준비 완료. 슬래시 명령어가 동기화되었습니다.")
    print('------------------------------------')

    panel_channel = bot.get_channel(bot.config.panel_channel_id)
    if panel_channel:
        async for msg in panel_channel.history(limit=5):
            if msg.author == bot.user and msg.embeds and "통합 관제 시스템" in msg.embeds[0].title:
                try:
                    await msg.delete()
                except discord.errors.NotFound:
                    pass
                break
        
        # --- ▼▼▼ [핵심 수정] ControlPanelView 생성 시 config 객체 전달 ▼▼▼ ---
        view = ControlPanelView(
            config=bot.config, # config 객체 전달
            aggr_level_callback=bot.background_tasks.on_aggr_level_change,
            trading_engine=bot.trading_engine
        )
        # --- ▲▲▲ [핵심 수정] ---

        panel_embed = bot.background_tasks.get_panel_embed()
        panel_message = await panel_channel.send(embed=panel_embed, view=view)
        
        bot.background_tasks.panel_message = panel_message
        print(f"✅ '{panel_channel.name}' 채널에 제어 패널을 생성했습니다.")
    else:
        print("⚠️ .env 파일에서 DISCORD_PANEL_CHANNEL_ID를 찾을 수 없거나 채널이 존재하지 않습니다.")

    analysis_channel = bot.get_channel(bot.config.analysis_channel_id)
    if analysis_channel:
        async for msg in analysis_channel.history(limit=10):
            if msg.author == bot.user and msg.embeds and "라이브 종합 상황판" in msg.embeds[0].title:
                bot.background_tasks.analysis_message = msg
                print("✅ 기존 분석 상황판 메시지를 찾았습니다.")
                break
    else:
        print("⚠️ .env 파일에서 DISCORD_ANALYSIS_CHANNEL_ID를 찾을 수 없거나 채널이 존재하지 않습니다.")

    bot.background_tasks.start_all_tasks()
    print("✅ 모든 백그라운드 작업이 시작되었습니다.")
    asyncio.create_task(bot.background_tasks.event_handler_loop())
    print("--- 모든 준비 완료 ---")


if __name__ == "__main__":
    if not os.getenv("DISCORD_BOT_TOKEN"):
        print("🚨 .env 파일에 DISCORD_BOT_TOKEN이 설정되지 않았습니다. 봇을 실행할 수 없습니다.")
    else:
        try:
            bot.run(os.getenv("DISCORD_BOT_TOKEN"))
        except RuntimeError as e:
            print(f"봇 실행 중 치명적인 오류 발생: {e}")
        except Exception as e:
            print(f"예상치 못한 오류로 봇이 종료되었습니다: {e}")
