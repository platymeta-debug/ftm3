import discord
from discord import app_commands
from discord.ext import commands, tasks
from binance.client import Client
import asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
import pandas as pd
from enum import Enum
import statistics

# 1. 모듈 임포트
from core.config_manager import config
from core.event_bus import event_bus
# --- ▼▼▼ 수정된 부분 ▼▼▼ ---
from database.manager import db_manager
from database.models import Signal, Trade # Signal과 Trade를 models.py에서 가져오도록 수정
# --- ▲▲▲ 수정된 부분 ▲▲▲ ---
from execution.trading_engine import TradingEngine
from analysis.confluence_engine import ConfluenceEngine
from risk_management.position_sizer import PositionSizer
from ui.views import ControlPanelView, ConfirmView

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

# --- 시장 체제 정의 ---
class MarketRegime(Enum):
    BULL_TREND = "강세 추세"
    BEAR_TREND = "약세 추세"
    SIDEWAYS = "횡보"

# --- 유틸리티 함수 ---

def _extract_float_from_row(row, keys):
    if row is None:
        return None
    if isinstance(keys, str):
        keys = (keys,)
    for key in keys:
        value = None
        if hasattr(row, "get"):
            try:
                value = row.get(key)
            except Exception:
                value = None
        if value is None:
            try:
                if key in row:
                    value = row[key]
            except Exception:
                value = None
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_bool_from_row(row, key):
    if row is None:
        return None
    value = None
    if hasattr(row, "get"):
        try:
            value = row.get(key)
        except Exception:
            value = None
    if value is None:
        try:
            if key in row:
                value = row[key]
        except Exception:
            value = None
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "t", "yes", "y"}:
            return True
        if lowered in {"false", "0", "f", "no", "n"}:
            return False
    return None

# --- 콜백 및 UI 생성 함수 ---
def on_aggr_level_change(new_level: int):
    global current_aggr_level
    current_aggr_level = new_level

def get_panel_embed() -> discord.Embed:
    """실시간 데이터를 담은 제어 패널 Embed를 생성합니다."""
    embed = discord.Embed(title="⚙️ 통합 관제 시스템", description="봇의 모든 상태를 확인하고 제어합니다.", color=0x2E3136)
    
    trade_mode_text = "🔴 **실시간 매매**" if not config.is_testnet else "🟢 **테스트넷**"
    auto_trade_text = "✅ **자동매매 ON**" if config.exec_active else "❌ **자동매매 OFF**"
    adaptive_text = "🧠 **자동 조절 ON**" if config.adaptive_aggr_enabled else "👤 **수동 설정**"
    embed.add_field(name="[핵심 상태]", value=f"{trade_mode_text}\n{auto_trade_text}\n{adaptive_text}", inline=True)

    symbols_text = f"**{', '.join(config.symbols)}**"
    base_aggr_text = f"**Level {config.aggr_level}**"
    current_aggr_text = f"**Level {current_aggr_level}**"
    if config.adaptive_aggr_enabled and config.aggr_level != current_aggr_level:
        status = " (⚠️위험)" if current_aggr_level < config.aggr_level else " (📈안정)"
        current_aggr_text += status
    embed.add_field(name="[현재 전략]", value=f"분석 대상: {symbols_text}\n기본 공격성: {base_aggr_text}\n현재 공격성: {current_aggr_text}", inline=True)
    
    try:
        with db_manager.get_session() as session:
            open_positions_count = session.query(Trade).filter(Trade.status == "OPEN").count()
        embed.add_field(name="[포트폴리오]", value=f"**{open_positions_count} / {config.max_open_positions}** 포지션 운영 중", inline=False)

        positions = binance_client.futures_position_information()
        open_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]

        if not open_positions:
            embed.add_field(name="[오픈된 포지션]", value="현재 오픈된 포지션이 없습니다.", inline=False)
        else:
            for pos in open_positions:
                symbol = pos['symbol']
                side = "LONG" if float(pos['positionAmt']) > 0 else "SHORT"
                quantity = abs(float(pos['positionAmt']))
                entry_price = float(pos['entryPrice'])
                unrealized_pnl = float(pos['unRealizedProfit'])
                pnl_color = "📈" if unrealized_pnl >= 0 else "📉"
                leverage = int(pos.get('leverage', 1))
                margin = float(pos.get('isolatedWallet', 0))
                pnl_percent = (unrealized_pnl / margin * 100) if margin > 0 else 0.0
                pos_value = (f"**{side}** | `{quantity}` @ `${entry_price:,.2f}` | **{leverage}x**\n"
                             f"> PnL: `${unrealized_pnl:,.2f}` ({pnl_percent:+.2f}%) {pnl_color}")
                embed.add_field(name=f"--- {symbol} ---", value=pos_value, inline=True)
    except Exception as e:
        print(f"패널 포지션 정보 업데이트 중 오류: {e}")
        embed.add_field(name="[오픈된 포지션]", value="⚠️ 정보를 가져오는 중 오류가 발생했습니다.", inline=False)

    embed.set_footer(text=f"최종 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return embed

def diagnose_market_regime(session, symbol: str) -> MarketRegime:
    """[시장 진단] DB 데이터를 기반으로 현재 시장 체제를 진단합니다."""
    latest_signal_tuple = session.execute(
        select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc())
    ).first()

    if not latest_signal_tuple: return MarketRegime.SIDEWAYS 
    
    latest_signal = latest_signal_tuple[0]
    if latest_signal.adx_4h is None or getattr(latest_signal, 'is_above_ema200_1d', None) is None:
        return MarketRegime.SIDEWAYS

    if latest_signal.adx_4h > config.market_regime_adx_th:
        return MarketRegime.BULL_TREND if latest_signal.is_above_ema200_1d else MarketRegime.BEAR_TREND
    else:
        return MarketRegime.SIDEWAYS
    
def update_adaptive_aggression_level():
    """[지능형 로직] 시장 변동성을 분석하여 현재 공격성 레벨을 동적으로 조절합니다."""
    global current_aggr_level
    base_aggr_level = config.aggr_level
    with db_manager.get_session() as session:
        try:
            # --- ▼▼▼ [오류 1 해결] .scalar_one_or_none()을 .first()로 변경 ▼▼▼ ---
            latest_signal_tuple = session.execute(select(Signal).where(Signal.symbol == "BTCUSDT").order_by(Signal.id.desc())).first()
            # --- ▲▲▲ [오류 1 해결] ▲▲▲ ---

            if not latest_signal_tuple or not latest_signal_tuple[0].atr_1d:
                if current_aggr_level != base_aggr_level:
                    print(f"[Adaptive] 데이터 부족. 공격성 레벨 복귀: {current_aggr_level} -> {base_aggr_level}")
                    current_aggr_level = base_aggr_level
                return

            latest_signal = latest_signal_tuple[0]
            mark_price_info = binance_client.futures_mark_price(symbol="BTCUSDT")
            current_price = float(mark_price_info['markPrice'])
            volatility = latest_signal.atr_1d / current_price
            if volatility > config.adaptive_volatility_threshold:
                new_level = max(1, base_aggr_level - 2)
                if new_level != current_aggr_level:
                    print(f"[Adaptive] 변동성 증가 감지({volatility:.2%})! 공격성 레벨 하향 조정: {current_aggr_level} -> {new_level}")
                    current_aggr_level = new_level
            else:
                if current_aggr_level != base_aggr_level:
                    print(f"[Adaptive] 시장 안정. 공격성 레벨 복귀: {current_aggr_level} -> {base_aggr_level}")
                    current_aggr_level = base_aggr_level
        except Exception as e:
            print(f"🚨 적응형 레벨 조정 중 오류: {e}")
            current_aggr_level = base_aggr_level

# --- V3 백그라운드 작업 ---

@tasks.loop(seconds=15)
async def panel_update_loop():
    if panel_message:
        try:
            await panel_message.edit(embed=get_panel_embed())
        except discord.NotFound:
            print("패널 메시지를 찾을 수 없어 업데이트 루프를 중지합니다.")
            panel_update_loop.stop()
        except Exception as e:
            print(f"패널 업데이트 중 오류 발생: {e}")


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
        lookback_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        recent_signals = session.execute(
            select(Signal.final_score)
            .where(Signal.symbol == symbol, Signal.timestamp >= lookback_time)
            .order_by(Signal.timestamp.asc())
        ).scalars().all()
        
        sparkline = generate_sparkline(recent_signals)
        
        # 현재 분석 스냅샷
        latest_signal_tuple = session.execute(select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc())).first()
        latest_signal = latest_signal_tuple[0] if latest_signal_tuple else None
        score_text = f"**{latest_signal.final_score:.2f}**" if latest_signal else "N/A"
        embed.add_field(name=f"{symbol} | {market_regime.value}", value=f"스코어 흐름: {sparkline}\n현재 점수: {score_text}", inline=False)
    embed.set_footer(text=f"최종 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return embed


@tasks.loop(minutes=1)
async def data_collector_loop():
    global analysis_message
    print(f"\n--- [Data Collector] 분석 시작 ---")
    session = db_manager.get_session()
    try:
        for symbol in config.symbols:
            final_score, tf_scores, tf_rows = confluence_engine.analyze(symbol)
            
            adx_4h_val = _extract_float_from_row(tf_rows.get("4h"), ("adx_value", "ADX_14"))
            daily_row = tf_rows.get("1d")
            is_above_ema200 = _extract_bool_from_row(daily_row, "is_above_ema200")

            new_signal = Signal(
                symbol=symbol, final_score=final_score,
                score_1d=tf_scores.get("1d"), score_4h=tf_scores.get("4h"),
                score_1h=tf_scores.get("1h"), score_15m=tf_scores.get("15m"),
                atr_1d=_extract_float_from_row(daily_row, "ATR_14"),
                adx_4h=adx_4h_val, is_above_ema200_1d=is_above_ema200
            )
            session.add(new_signal)
        session.commit()
    except Exception as e:
        print(f"🚨 데이터 수집 중 오류: {e}")
        session.rollback()


    try:
        analysis_channel = bot.get_channel(config.analysis_channel_id)
        if not analysis_channel: return
        with db_manager.get_session() as session:
            analysis_embed = get_analysis_embed(session)
        if analysis_message:
            await analysis_message.edit(embed=analysis_embed)
        else:
            async for msg in analysis_channel.history(limit=5):
                if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "📊 라이브 종합 상황판":
                    analysis_message = msg
                    await analysis_message.edit(embed=analysis_embed)
                    return
            analysis_message = await analysis_channel.send(embed=analysis_embed)
    except Exception as e:
        print(f"🚨 분석 상황판 업데이트 중 오류: {e}")
    # --- ▲▲▲ [Discord V3] 분석 상황판 업데이트 로직 ▲▲▲ ---


@tasks.loop(minutes=5)
async def trading_decision_loop():
    """[V4 최종] 시장 체제, 신호 품질, 포트폴리오를 종합하여 매매를 결정합니다."""
    global current_aggr_level

    if not config.exec_active:
        return

    if config.adaptive_aggr_enabled:
        update_adaptive_aggression_level()

    print(f"\n--- [Trading Decision (Lvl:{current_aggr_level})] 매매 결정 시작 ---")
    with db_manager.get_session() as session:
        try:
            open_trades = session.execute(select(Trade).where(Trade.status == "OPEN")).scalars().all()
            open_positions_count = len(open_trades)

            if open_positions_count > 0:
                print(f"총 {open_positions_count}개의 포지션 관리 중...")
                for trade in list(open_trades):
                    try:
                        mark_price_info = binance_client.futures_mark_price(symbol=trade.symbol)
                        current_price = float(mark_price_info.get('markPrice', 0.0))
                        if current_price == 0.0: continue

                        if trade.take_profit_price and ((trade.side == "BUY" and current_price >= trade.take_profit_price) or \
                           (trade.side == "SELL" and current_price <= trade.take_profit_price)):
                            await trading_engine.close_position(trade, f"자동 익절 (TP: ${trade.take_profit_price:,.2f})")
                            continue

                        if trade.stop_loss_price and ((trade.side == "BUY" and current_price <= trade.stop_loss_price) or \
                           (trade.side == "SELL" and current_price >= trade.stop_loss_price)):
                            await trading_engine.close_position(trade, f"자동 손절 (SL: ${trade.stop_loss_price:,.2f})")
                            continue
                    except Exception as e:
                        print(f"포지션 관리 중 오류 ({trade.symbol}): {e}")

            open_positions_count = session.query(Trade).filter(Trade.status == "OPEN").count()
            
            if open_positions_count < config.max_open_positions:
                print(f"신규 진입 기회 탐색 중... (현재 {open_positions_count}/{config.max_open_positions} 슬롯 사용 중)")
                symbols_in_trade = {t.symbol for t in open_trades}
                
                for symbol in config.symbols:
                    if symbol in symbols_in_trade: continue
                    market_regime = diagnose_market_regime(session, symbol)
                    
                    if market_regime in [MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND]:
                        recent_signals = session.execute(select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc()).limit(config.trend_entry_confirm_count)).scalars().all()
                        if len(recent_signals) < config.trend_entry_confirm_count: continue
                        
                        scores = [s.final_score for s in recent_signals]
                        avg_score = statistics.mean(scores)
                        std_dev = statistics.pstdev(scores) if len(scores) > 1 else 0

                        # --- ▼▼▼ [오류 2 해결] 'Momentum' 조건 완화 ▼▼▼ ---
                        print(f"[{symbol}] 추세장 신호 품질 평가: Avg={avg_score:.2f}, StdDev={std_dev:.2f}")

                        side = None
                        if market_regime == MarketRegime.BULL_TREND and avg_score >= config.quality_min_avg_score and std_dev <= config.quality_max_std_dev:
                            side = "BUY"
                        elif market_regime == MarketRegime.BEAR_TREND and abs(avg_score) >= config.quality_min_avg_score and std_dev <= config.quality_max_std_dev:
                            side = "SELL"
                        # --- ▲▲▲ [오류 2 해결] ▲▲▲ ---
                        
                        if side:
                            print(f"🚀 고품질 추세 신호 포착!: {symbol} {side} (Avg: {avg_score:.2f})")
                            
                            # --- ▼▼▼ [오류 2 해결] ATR 조회 로직 강화 ▼▼▼ ---
                            entry_atr = recent_signals[0].atr_1d
                            if not entry_atr or entry_atr <= 0:
                                print(f"경고: 1일봉 ATR({entry_atr})이 유효하지 않습니다. 4시간봉 ATR로 대체합니다.")
                                # 4시간봉 ATR을 조회하기 위해 Confluence Engine 재활용
                                _, _, tf_rows = confluence_engine.analyze(symbol)
                                entry_atr = _extract_float_from_row(tf_rows.get("4h"), "ATR_14")
                                if not entry_atr or entry_atr <= 0:
                                    print(f"오류: 4시간봉 ATR도 유효하지 않아({entry_atr}) 진입을 건너뜁니다.")
                                    continue
                            # --- ▲▲▲ [오류 2 해결] ▲▲▲ ---

                            quantity = position_sizer.calculate_position_size(symbol, entry_atr, current_aggr_level, open_positions_count, avg_score)
                            if not quantity or quantity <= 0: continue
                            
                            leverage = position_sizer.get_leverage_for_symbol(symbol, current_aggr_level)
                            analysis_context = {"signal_id": recent_signals[0].id}
                            await trading_engine.place_order_with_bracket(symbol, side, quantity, leverage, entry_atr, analysis_context)
                            return
        except Exception as e:
            print(f"🚨 매매 결정 루프 중 심각한 오류 발생: {e}")


# --- 한글 슬래시 명령어 (V3) ---

@tree.command(name="패널", description="인터랙티브 제어실을 소환합니다.")
async def summon_panel_kr(interaction: discord.Interaction):
    global panel_message
    panel_channel = bot.get_channel(config.panel_channel_id)
    if not panel_channel:
        return await interaction.response.send_message("⚠️ `.env`에 `DISCORD_PANEL_CHANNEL_ID`를 설정해주세요.", ephemeral=True)
    if panel_message and panel_message.channel.id == panel_channel.id:
        try: await panel_message.delete()
        except: pass
    await interaction.response.send_message(f"✅ 제어 패널을 {panel_channel.mention} 채널에 소환했습니다.", ephemeral=True)
    view = ControlPanelView(aggr_level_callback=on_aggr_level_change)
    panel_message = await panel_channel.send(embed=get_panel_embed(), view=view)
    if not panel_update_loop.is_running():
        panel_update_loop.start()


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
    view = ConfirmView()
    await interaction.response.send_message(f"**⚠️ 경고: 수동 청산**\n`{symbol}` 포지션을 즉시 시장가로 종료하시겠습니까?", view=view, ephemeral=True)
    await view.wait()
    if view.value is True:
        try:
            with db_manager.get_session() as session:
                trade_to_close = session.execute(select(Trade).where(Trade.symbol == symbol, Trade.status == "OPEN")).scalar_one_or_none()
            if trade_to_close:
                await trading_engine.close_position(trade_to_close, "사용자 수동 청산")
                await interaction.followup.send(f"✅ **수동 청산 주문 성공**\n`{symbol}` 포지션이 종료되었습니다.", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ **수동 청산 주문 실패**\n`{symbol}`에 대한 오픈된 포지션이 없습니다.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ **수동 청산 주문 실패**\n`{e}`", ephemeral=True)

# --- 봇 준비 이벤트 ---
@bot.event
async def on_ready():
    """봇이 준비되었을 때 한 번 실행되는 함수"""
    await tree.sync()
    print(f'{bot.user.name} 봇이 준비되었습니다. 슬래시 명령어가 동기화되었습니다.')
    print('------------------------------------')

    # 백그라운드 루프를 시작합니다.
    if not data_collector_loop.is_running():
        data_collector_loop.start()
    
    # data_collector가 데이터를 먼저 쌓을 수 있도록 잠시 기다립니다.
    await asyncio.sleep(5) 
    
    if not trading_decision_loop.is_running():
        trading_decision_loop.start()

    print("모든 준비 완료. `/패널` 명령어를 사용하여 제어실을 소환하세요.")

# --- 봇 실행 ---
if __name__ == "__main__":
    if not config.discord_bot_token:
        print("오류: .env 파일에 DISCORD_BOT_TOKEN이 설정되지 않았습니다.")
    else:
        bot.run(config.discord_bot_token)
