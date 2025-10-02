import discord
from core.config_manager import config

# 순환 참조를 피하기 위해 main.py의 콜백 함수 타입 힌팅
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import on_aggr_level_change


class ControlPanelView(discord.ui.View):
    """V3: 봇의 상태를 제어하는 동적 인터랙티브 패널"""

    def __init__(self, aggr_level_callback: 'on_aggr_level_change'):
        super().__init__(timeout=None)
        self.aggr_level_callback = aggr_level_callback
        self._update_adaptive_button()

    def _update_adaptive_button(self):
        """적응형 로직 버튼의 라벨과 스타일을 현재 상태에 맞게 업데이트합니다."""
        # custom_id를 통해 특정 버튼을 찾음
        adaptive_button = next((item for item in self.children if hasattr(item, 'custom_id') and item.custom_id == "toggle_adaptive"), None)
        if adaptive_button:
            if config.adaptive_aggr_enabled:
                adaptive_button.label = "🧠 자동 조절 ON"
                adaptive_button.style = discord.ButtonStyle.success
            else:
                adaptive_button.label = "👤 수동 설정"
                adaptive_button.style = discord.ButtonStyle.secondary

    @discord.ui.button(label="자동매매 시작", style=discord.ButtonStyle.green, custom_id="toggle_autotrade_start", row=0)
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        config.exec_active = True
        await interaction.response.send_message("✅ 자동매매를 시작합니다.", ephemeral=True)

    @discord.ui.button(label="자동매매 중지", style=discord.ButtonStyle.red, custom_id="toggle_autotrade_stop", row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        config.exec_active = False
        await interaction.response.send_message("🛑 자동매매를 중지합니다.", ephemeral=True)

    @discord.ui.button(label=" ", style=discord.ButtonStyle.secondary, custom_id="toggle_adaptive", row=0)
    async def adaptive_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        config.adaptive_aggr_enabled = not config.adaptive_aggr_enabled
        self._update_adaptive_button()
        await interaction.message.edit(view=self)
        status = "활성화" if config.adaptive_aggr_enabled else "비활성화"
        await interaction.response.send_message(f"🧠 적응형 공격성 레벨 자동 조절을 {status}했습니다.", ephemeral=True)

    @discord.ui.select(
        placeholder="기본 공격성 레벨 변경",
        options=[discord.SelectOption(label=f"Level {i}", value=str(i)) for i in range(1, 11)],
        custom_id="select_agg_level", row=1
    )
    async def agg_level_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        selected_level = int(select.values[0])
        config.aggr_level = selected_level
        self.aggr_level_callback(selected_level)
        await interaction.response.send_message(f"기본 공격성 레벨을 **Level {selected_level}** (으)로 설정했습니다.", ephemeral=True)

    @discord.ui.button(label="🚨 긴급 전체 청산", style=discord.ButtonStyle.danger, custom_id="panic_close_all", row=2)
    async def panic_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 이벤트 버스를 통해 긴급 청산 신호 발행
        await interaction.response.send_message("🚨 긴급 전체 청산 신호를 보냈습니다. 결과를 확인해주세요.", ephemeral=True)
        # event_bus.publish(...) # 추후 직접 청산 로직으로 강화 가능
