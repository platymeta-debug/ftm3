import discord

from core.config_manager import config
from core.event_bus import event_bus


AGGRESSION_OPTIONS = [
    discord.SelectOption(
        label="레벨 1 - 보수적",
        value="1",
        description="최소한의 리스크를 감수하는 전략"
    ),
    discord.SelectOption(
        label="레벨 2 - 균형형",
        value="2",
        description="리스크와 수익의 균형을 추구"
    ),
    discord.SelectOption(
        label="레벨 3 - 적극적",
        value="3",
        description="더 큰 수익을 위해 리스크 허용"
    ),
    discord.SelectOption(
        label="레벨 4 - 공격적",
        value="4",
        description="높은 리스크를 감수하는 전략"
    ),
    discord.SelectOption(
        label="레벨 5 - 최대",
        value="5",
        description="극단적인 리스크를 감수하는 전략"
    ),
]


class ControlPanelView(discord.ui.View):
    """제어 패널의 버튼과 메뉴들을 포함하는 View 클래스입니다."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="자동매매 시작",
        style=discord.ButtonStyle.green,
        custom_id="toggle_autotrade_start",
    )
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        config.exec_active = True
        await interaction.response.send_message("✅ 자동매매를 시작합니다.", ephemeral=True)
        print("사용자가 자동매매 시작 버튼을 눌렀습니다.")

    @discord.ui.button(
        label="자동매매 중지",
        style=discord.ButtonStyle.red,
        custom_id="toggle_autotrade_stop",
    )
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        config.exec_active = False
        await interaction.response.send_message("🛑 자동매매를 중지합니다.", ephemeral=True)
        print("사용자가 자동매매 중지 버튼을 눌렀습니다.")

    @discord.ui.button(
        label="긴급 전체 청산",
        style=discord.ButtonStyle.danger,
        emoji="🚨",
        custom_id="panic_close_all",
    )
    async def panic_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            "🚨 긴급 전체 청산 신호를 보냈습니다. 결과를 확인해주세요.",
            ephemeral=True,
        )
        await event_bus.publish("PANIC_SIGNAL", {"user": interaction.user.name})

    @discord.ui.select(
        placeholder="투자 공격성 레벨 선택",
        options=AGGRESSION_OPTIONS,
        custom_id="select_agg_level",
    )
    async def agg_level_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        selected_level = select.values[0] if select.values else "1"
        try:
            config.aggr_level = int(selected_level)
        except ValueError:
            pass
        await interaction.response.send_message(
            f"투자 공격성 레벨을 **{selected_level}** (으)로 설정했습니다.",
            ephemeral=True,
        )
        print(f"사용자가 공격성 레벨을 {selected_level}(으)로 변경했습니다.")
