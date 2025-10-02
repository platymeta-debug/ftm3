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
import requests

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
latest_analysis_results = {}

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

def get_external_prices(symbol: str) -> str:
    """바이낸스 선물과 업비트 현물 가격을 조회하여 문자열로 반환합니다."""
    # 심볼 변환 (e.g., BTCUSDT -> KRW-BTC)
    upbit_symbol = f"KRW-{symbol.replace('USDT', '')}"
    
    # 바이낸스 가격 조회
    try:
        futures_price_info = binance_client.futures_mark_price(symbol=symbol)
        futures_price = float(futures_price_info['markPrice'])
        price_str = f"📈 **바이낸스**: `${futures_price:,.2f}`"
    except Exception:
        price_str = "📈 **바이낸스**: `N/A`"

    # 업비트 가격 조회
    try:
        response = requests.get(f"https://api.upbit.com/v1/ticker?markets={upbit_symbol}", timeout=2)
        response.raise_for_status()
        upbit_price = response.json()[0]['trade_price']
        price_str += f"\n📉 **업비트**: `₩{upbit_price:,.0f}`"
    except Exception:
        price_str += "\n📉 **업비트**: `N/A`"
        
    return price_str

def get_panel_embed() -> discord.Embed:
    """
    [V5.4 최종] API Key 이름 오류('unrealizedProfit')를 수정한 최종 버전입니다.
    API를 기준으로 포지션을 정확히 탐지하고, DB 정보와 결합하여
    SL/TP, 청산가 등 모든 상세 정보를 표시합니다.
    """
    embed = discord.Embed(title="⚙️ 통합 관제 시스템", description="봇의 모든 상태를 확인하고 제어합니다.", color=0x2E3136)
    
    # --- 1. 상단 정보 (핵심 상태, 현재 전략) ---
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

    # --- 2. 포트폴리오 및 포지션 상세 정보 ---
     try:
        account_info = binance_client.futures_account()
        positions_from_api = [p for p in account_info.get('positions', []) if float(p.get('positionAmt', 0)) != 0]
        
        total_balance = float(account_info.get('totalWalletBalance', 0.0))
        total_pnl = float(account_info.get('totalUnrealizedProfit', 0.0))
        pnl_color = "📈" if total_pnl >= 0 else "📉"
        
        embed.add_field(
            name="[포트폴리오]",
            value=f"💰 **총 자산**: `${total_balance:,.2f}`\n"
                  f"{pnl_color} **총 미실현 PnL**: `${total_pnl:,.2f}`\n"
                  f"📊 **운영 포지션**: **{len(positions_from_api)} / {config.max_open_positions}** 개",
            inline=False
        )

        if not positions_from_api:
            embed.add_field(name="[오픈된 포지션]", value="현재 오픈된 포지션이 없습니다.", inline=False)
        else:
            db_session = db_manager.get_session()
            for pos in positions_from_api:
                symbol = pos.get('symbol')
                if not symbol: continue

                pnl = float(pos.get('unrealizedProfit', 0.0))
                side = "LONG" if float(pos.get('positionAmt', 0.0)) > 0 else "SHORT"
                quantity = abs(float(pos.get('positionAmt', 0.0)))
                entry_price = float(pos.get('entryPrice', 0.0))
                leverage = int(pos.get('leverage', 1))
                liq_price = float(pos.get('liquidationPrice', 0.0))
                margin = float(pos.get('initialMargin', 0.0))
                pnl_percent = (pnl / margin * 100) if margin > 0 else 0.0
                
                trade_db = db_session.query(Trade).filter(Trade.symbol == symbol, Trade.status == "OPEN").first()
                
                pnl_text = f"📈 **PnL**: `${pnl:,.2f}` (`{pnl_percent:+.2f} %`)" if pnl >= 0 else f"📉 **PnL**: `${pnl:,.2f}` (`{pnl_percent:+.2f} %`)"
                
                details_text = f"> **진입가**: `${entry_price:,.2f}` | **수량**: `{quantity}`\n> {pnl_text}\n"
                
                if trade_db and trade_db.stop_loss_price:
                    # DB에 기록된 SL/TP가 있는 경우
                    sl_price = trade_db.stop_loss_price
                    tp_price = trade_db.take_profit_price
                    mark_price = float(binance_client.futures_mark_price(symbol=symbol).get('markPrice', 0.0))
                    
                    if mark_price > 0:
                        sl_dist_pct = (abs(mark_price - sl_price) / mark_price) * 100
                        tp_dist_pct = (abs(tp_price - mark_price) / mark_price) * 100
                        details_text += f"> **SL**: `${sl_price:,.2f}` (`{sl_dist_pct:.2f}%`)\n> **TP**: `${tp_price:,.2f}` (`{tp_dist_pct:.2f}%`)\n"
                    else:
                        details_text += f"> **SL**: `${sl_price:,.2f}`\n> **TP**: `${tp_price:,.2f}`\n"
                else:
                    details_text += "> **SL/TP**: `(봇 관리 아님)`\n"

                details_text += f"> **청산가**: " + (f"`${liq_price:,.2f}`" if liq_price > 0 else "`N/A`")

                embed.add_field(name=f"--- {symbol} ({side} x{leverage}) ---", value=details_text, inline=False)
                
            db_session.close()

    except Exception as e:
        print(f"패널 정보 업데이트 중 오류: {e}")
        embed.add_field(name="[포트폴리오]", value="⚠️ 정보를 가져오는 중 오류가 발생했습니다.", inline=False)
    
    embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
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
    """점수 리스트로 보기 좋은 텍스트 스파크라인 차트를 생성합니다."""
    if not scores or len(scores) < 2: return "데이터 수집 중..."
    bar_chars = [' ', '▂', '▃', '▄', '▅', '▆', '▇', '█']
    min_s, max_s = min(scores), max(scores)
    score_range = max_s - min_s if max_s > min_s else 1
    sparkline = [bar_chars[int((s - min_s) / score_range * (len(bar_chars) - 1))] for s in scores]
    trend_emoji = "📈" if scores[-1] > scores[0] else "📉" if scores[-1] < scores[0] else "➡️"
    return f"`{''.join(sparkline)}` **{scores[-1]:.1f}** {trend_emoji}"


# main.py의 get_analysis_embed 함수를 아래 내용으로 전체 교체해주세요.

def get_analysis_embed(session) -> discord.Embed:
    """
    [V5.6 최종] 각 코인별 시장 체제를 개별적으로 표시하고, 전체적인 시장 상황을
    요약하여 보여주는 최종 버전의 상황판입니다.
    """
    embed = discord.Embed(title="📊 라이브. 종합 상황판", color=0x4A90E2)
    
    if not latest_analysis_results:
        embed.description = "분석 데이터를 수집하고 있습니다..."
        return embed

    # --- ▼▼▼ [V5.6] 상단 설명을 더 중립적이고 전체적으로 수정 ▼▼▼ ---
    # 여러 코인의 시장 체제를 종합하여 보여줍니다.
    regime_counts = {}
    for data in latest_analysis_results.values():
        regime = data.get("market_regime")
        if regime:
            regime_counts[regime.value] = regime_counts.get(regime.value, 0) + 1
    
    if regime_counts:
        summary = ", ".join([f"**{k}**({v}개)" for k, v in regime_counts.items()])
        embed.description = f"현재 분석 대상 코인들의 시장은 {summary} 상태입니다."
    else:
        embed.description = "시장 체제를 분석하고 있습니다."
    # --- ▲▲▲ [V5.6] 수정 완료 ▲▲▲ ---
    
    for symbol, data in latest_analysis_results.items():
        # --- 실시간 가격 조회 ---
        price_text = get_external_prices(symbol)
        embed.add_field(name=f"--- {symbol} 실시간 시세 ---", value=price_text, inline=False)
        
        # --- 분석 정보 ---
        final_score = data.get("final_score", 0)
        
        # --- ▼▼▼ [V5.6] 각 코인별 시장 체제 표시 추가 ▼▼▼ ---
        market_regime = data.get("market_regime")
        regime_text = f"`{market_regime.value}`" if market_regime else "`분석 중...`"
        # --- ▲▲▲ [V5.6] 수정 완료 ▲▲▲ ---

        # 다중 타임프레임 점수 요약
        tf_scores = {tf: data.get("tf_breakdowns", {}).get(tf, {}) for tf in config.analysis_timeframes}
        tf_summary = " ".join([f"`{tf}:{sum(scores.values())}`" for tf, scores in tf_scores.items()])

        # 4h 주요 지표
        rows_4h = data.get("tf_rows", {}).get("4h")
        indicators_text = "N/A"
        if rows_4h is not None:
            rsi = rows_4h.get('RSI_14', 0)
            adx = rows_4h.get('ADX_14', 0)
            mfi = rows_4h.get('MFI_14', 0)
            indicators_text = f"**RSI**: `{rsi:.1f}` | **ADX**: `{adx:.1f}` | **MFI**: `{mfi:.1f}`"
        
        score_color = "🟢" if final_score > 0 else "🔴" if final_score < 0 else "⚪"
        
        field_value = (
            f"**현재 시장 체제:** {regime_text}\n" # <-- 여기에 개별 시장 체제 표시
            f"**종합 점수:** {score_color} **{final_score:.2f}**\n"
            f"**TF별 점수:** {tf_summary}\n"
            f"**4h 주요지표:** {indicators_text}"
        )
        embed.add_field(name="--- 분석 요약 ---", value=field_value, inline=False)
        
    embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    return embed

@tasks.loop(minutes=1)
async def data_collector_loop():
    global analysis_message, latest_analysis_results
    print(f"\n--- [Data Collector] 분석 시작 ---")
    session = db_manager.get_session()
    try:
        for symbol in config.symbols:
            final_score, tf_scores, tf_rows, tf_breakdowns = confluence_engine.analyze(symbol)
            latest_analysis_results[symbol] = {
                "final_score": final_score,
                "tf_rows": tf_rows,
                "tf_breakdowns": tf_breakdowns,
                "market_regime": diagnose_market_regime(session, symbol) # 시장 체제 진단 결과도 함께 저장
            }
            atr_1d_val = confluence_engine.extract_atr(tf_rows, primary_tf='1d')
            atr_4h_val = confluence_engine.extract_atr(tf_rows, primary_tf='4h')

            adx_4h_val = _extract_float_from_row(tf_rows.get("4h"), ("adx_value", "ADX_14"))
            daily_row = tf_rows.get("1d")
            is_above_ema200 = _extract_bool_from_row(daily_row, "is_above_ema200")

            new_signal = Signal(
                    symbol=symbol, final_score=final_score,
                    score_1d=tf_scores.get("1d"), score_4h=tf_scores.get("4h"),
                    score_1h=tf_scores.get("1h"), score_15m=tf_scores.get("15m"),
                    atr_1d=atr_1d_val,
                    atr_4h=atr_4h_val, # 4시간봉 ATR도 함께 저장
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


# --- V4: 시나리오 기반 포지션 관리 및 신규 진입 헬퍼 함수 ---

async def manage_open_positions(session, open_trades):
    """[V4] 현재 오픈된 포지션들을 시나리오에 따라 관리합니다 (분할익절, 피라미딩, 손절 등)."""
    print(f"총 {len(open_trades)}개의 포지션 관리 중...")
    for trade in list(open_trades):
        try:
            mark_price_info = binance_client.futures_mark_price(symbol=trade.symbol)
            current_price = float(mark_price_info.get('markPrice', 0.0))
            if current_price == 0.0: continue

            # 1. 스케일 아웃 (분할 익절) 로직
            if not trade.is_scaled_out:
                # 손익비 1:1 지점 계산
                scale_out_target_price = trade.entry_price + (trade.take_profit_price - trade.entry_price) / config.risk_reward_ratio
                
                if (trade.side == "BUY" and current_price >= scale_out_target_price) or \
                   (trade.side == "SELL" and current_price <= scale_out_target_price):
                    
                    quantity_to_close = trade.quantity / 2
                    await trading_engine.close_position(trade, f"자동 분할 익절 (목표: ${scale_out_target_price:,.2f})", quantity_to_close=quantity_to_close)
                    
                    # DB 업데이트: 분할 익절 플래그, 손절가를 본전으로 변경
                    trade.is_scaled_out = True
                    trade.stop_loss_price = trade.entry_price 
                    session.commit()
                    print(f"🛡️ [무위험 포지션 전환] {trade.symbol}의 손절가를 본전(${trade.entry_price:,.2f})으로 변경.")
                    continue

            # 2. 최종 익절 및 손절 로직
            if trade.take_profit_price and ((trade.side == "BUY" and current_price >= trade.take_profit_price) or \
               (trade.side == "SELL" and current_price <= trade.take_profit_price)):
                await trading_engine.close_position(trade, f"자동 최종 익절 (TP: ${trade.take_profit_price:,.2f})")
                continue

            if trade.stop_loss_price and ((trade.side == "BUY" and current_price <= trade.stop_loss_price) or \
               (trade.side == "SELL" and current_price >= trade.stop_loss_price)):
                await trading_engine.close_position(trade, f"자동 손절 (SL: ${trade.stop_loss_price:,.2f})")
                continue

            # 3. 피라미딩 (불타기) 로직 (분할 익절 후에는 실행 안 함)
            if not trade.is_scaled_out and trade.pyramid_count < 1: # 최대 1회로 제한
                latest_signal = session.execute(select(Signal).where(Signal.symbol == trade.symbol).order_by(Signal.id.desc())).scalar_one_or_none()
                if latest_signal and abs(latest_signal.final_score) >= config.quality_min_avg_score: # 여전히 강한 추세
                    
                    pyramid_quantity = trade.quantity # 현재 남은 물량만큼 추가
                    
                    print(f"🔥 [피라미딩] {trade.symbol}에 대한 강력한 추세 지속. {pyramid_quantity}만큼 추가 진입 시도.")
                    side = trade.side
                    order = binance_client.futures_create_order(symbol=trade.symbol, side=side, type='MARKET', quantity=pyramid_quantity)
                    
                    new_entry_price = float(order.get('avgPrice', current_price))
                    total_quantity = trade.quantity + pyramid_quantity
                    avg_price = (trade.entry_price * trade.quantity + new_entry_price * pyramid_quantity) / total_quantity
                    
                    trade.entry_price = avg_price
                    trade.quantity = total_quantity
                    trade.pyramid_count += 1
                    
                    new_atr = latest_signal.atr_4h
                    if new_atr > 0:
                        stop_loss_distance = new_atr * config.sl_atr_multiplier
                        trade.stop_loss_price = avg_price - stop_loss_distance if side == "BUY" else avg_price + stop_loss_distance
                    
                    session.commit()
                    print(f"   ㄴ 추가 진입 성공. 새로운 평균 단가: ${avg_price:,.2f}, 총 수량: {total_quantity}, 새로운 SL: ${trade.stop_loss_price:,.2f}")

        except Exception as e:
            print(f"포지션 관리 중 오류 ({trade.symbol}): {e}")
            session.rollback()

async def find_new_entry_opportunities(session, open_positions_count, symbols_in_trade):
    """[V4] 신규 진입 기회를 탐색하고 조건에 맞으면 주문을 실행합니다."""
    if open_positions_count >= config.max_open_positions:
        return
        
    print(f"신규 진입 기회 탐색 중... (현재 {open_positions_count}/{config.max_open_positions} 슬롯 사용 중)")
    for symbol in config.symbols:
        if symbol in symbols_in_trade: continue

        market_regime = diagnose_market_regime(session, symbol)
        if market_regime in [MarketRegime.BULL_TREND, MarketRegime.BEAR_TREND]:
            recent_signals = session.execute(select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc()).limit(config.trend_entry_confirm_count)).scalars().all()
            if len(recent_signals) < config.trend_entry_confirm_count: continue
            
            scores = [s.final_score for s in recent_signals]
            avg_score = statistics.mean(scores)
            std_dev = statistics.pstdev(scores) if len(scores) > 1 else 0

            print(f"[{symbol}] 추세장 신호 품질 평가: Avg={avg_score:.2f}, StdDev={std_dev:.2f}")

            side = None
            if market_regime == MarketRegime.BULL_TREND and avg_score >= config.quality_min_avg_score and std_dev <= config.quality_max_std_dev:
                side = "BUY"
            elif market_regime == MarketRegime.BEAR_TREND and abs(avg_score) >= config.quality_min_avg_score and std_dev <= config.quality_max_std_dev:
                side = "SELL"
            
            if side:
                print(f"🚀 [A급 타점 포착!] {symbol} {side} (Avg: {avg_score:.2f})")
                
                entry_atr = recent_signals[0].atr_4h 
                if not entry_atr or entry_atr <= 0:
                    print(f"ATR 값이 유효하지 않아({entry_atr}) 진입을 건너뜁니다.")
                    continue
                
                quantity = position_sizer.calculate_position_size(symbol, entry_atr, current_aggr_level, open_positions_count, avg_score)
                if not quantity or quantity <= 0: continue
                
                leverage = position_sizer.get_leverage_for_symbol(symbol, current_aggr_level)
                analysis_context = {"signal_id": recent_signals[0].id}
                await trading_engine.place_order_with_bracket(symbol, side, quantity, leverage, entry_atr, analysis_context)
                return # 한 번에 하나의 신규 진입만 실행
            
# --- ▼▼▼ [V4.1] 이벤트 핸들러 루프 추가 ▼▼▼ ---
async def event_handler_loop():
    """이벤트 버스에서 이벤트를 구독하고, 유형에 따라 적절한 동작을 수행합니다."""
    print("이벤트 핸들러 루프가 시작되었습니다. 알림 대기 중...")
    while True:
        try:
            event = await event_bus.subscribe()
            event_type = event.get("type")
            data = event.get("data", {})
            
            alerts_channel = bot.get_channel(config.alerts_channel_id)
            if not alerts_channel:
                print("⚠️ 알림 채널 ID를 찾을 수 없습니다. .env 파일을 확인하세요.")
                continue

            if event_type == "ORDER_SUCCESS":
                trade = data.get("trade")
                embed = discord.Embed(title="🚀 신규 포지션 진입", color=0x00FF00 if trade.side == "BUY" else 0xFF0000)
                embed.add_field(name="코인", value=trade.symbol, inline=True)
                embed.add_field(name="방향", value=trade.side, inline=True)
                embed.add_field(name="수량", value=f"{trade.quantity}", inline=True)
                embed.add_field(name="진입 가격", value=f"${trade.entry_price:,.4f}", inline=False)
                embed.add_field(name="손절 (SL)", value=f"${trade.stop_loss_price:,.4f}", inline=True)
                embed.add_field(name="익절 (TP)", value=f"${trade.take_profit_price:,.4f}", inline=True)
                embed.set_footer(text=f"주문 ID: {trade.binance_order_id}")
                await alerts_channel.send(embed=embed)

            elif event_type == "ORDER_CLOSE_SUCCESS":
                trade = data.get("trade")
                reason = data.get("reason")
                pnl_percent = (trade.pnl / (trade.entry_price * trade.quantity) * 100)
                embed = discord.Embed(title="✅ 포지션 종료", description=f"사유: {reason}", color=0x3498DB)
                embed.add_field(name="코인", value=trade.symbol, inline=True)
                embed.add_field(name="수익 (PnL)", value=f"${trade.pnl:,.2f} ({pnl_percent:+.2f}%)", inline=True)
                await alerts_channel.send(embed=embed)

            elif event_type == "ORDER_FAILURE":
                embed = discord.Embed(title="🚨 주문 실패", description=data.get("error"), color=0xFF0000)
                embed.add_field(name="코인", value=data.get("symbol"), inline=True)
                await alerts_channel.send(embed=embed)

        except Exception as e:
            print(f"이벤트 핸들러 오류: {e}")

@tasks.loop(minutes=5)
async def trading_decision_loop():
    """[V4 최종] '사령관'의 두뇌: 포지션 관리와 신규 진입을 총괄합니다."""
    if not config.exec_active:
        return

    if config.adaptive_aggr_enabled:
        update_adaptive_aggression_level()

    print(f"\n--- [Executive's Brain (Lvl:{current_aggr_level})] 의사결정 사이클 시작 ---")
    with db_manager.get_session() as session:
        try:
            # 데이터베이스에서 현재 열려있는 모든 거래를 가져옵니다.
            open_trades = session.execute(select(Trade).where(Trade.status == "OPEN")).scalars().all()
            
            # 1. 기존에 열려있는 포지션들을 관리하는 함수를 호출합니다.
            if open_trades:
                await manage_open_positions(session, open_trades)

            # 2. 새로운 진입 기회를 탐색하는 함수를 호출합니다.
            #    (세션을 다시 조회하여 최신 포지션 상태를 반영합니다.)
            open_positions_count = session.query(Trade).filter(Trade.status == "OPEN").count()
            symbols_in_trade = {t.symbol for t in open_trades}
            await find_new_entry_opportunities(session, open_positions_count, symbols_in_trade)

        except Exception as e:
            print(f"🚨 의사결정 루프 중 심각한 오류 발생: {e}")
            session.rollback()


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
    """봇이 준비되었을 때 모든 작업을 시작합니다."""
    global panel_message
    await tree.sync()
    print(f'{bot.user.name} 봇이 준비되었습니다. 슬래시 명령어가 동기화되었습니다.')
    print('------------------------------------')

    # 1. 패널 자동 소환
    panel_channel = bot.get_channel(config.panel_channel_id)
    if panel_channel:
        # 기존 패널 메시지가 있다면 삭제
        async for msg in panel_channel.history(limit=5):
            if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "⚙️ 통합 관제 시스템":
                try: await msg.delete()
                except: pass
        
        print(f"'{panel_channel.name}' 채널에 제어 패널을 자동으로 생성합니다...")
        view = ControlPanelView(aggr_level_callback=on_aggr_level_change)
        panel_message = await panel_channel.send(embed=get_panel_embed(), view=view)
        
        if not panel_update_loop.is_running():
            panel_update_loop.start()
    else:
        print("경고: .env에 설정된 DISCORD_PANEL_CHANNEL_ID를 찾을 수 없어 패널을 자동으로 시작할 수 없습니다.")

    # 2. 백그라운드 루프 시작
    if not data_collector_loop.is_running():
        data_collector_loop.start()
    
    await asyncio.sleep(5) 
    
    if not trading_decision_loop.is_running():
        trading_decision_loop.start()

    print("모든 준비 완료. 디스코드 채널을 확인하세요.")

    # 3. 이벤트 핸들러 루프 시작
    asyncio.create_task(event_handler_loop())

    print("모든 준비 완료. 디스코드 채널을 확인하세요.")

# --- 봇 실행 ---
if __name__ == "__main__":
    if not config.discord_bot_token:
        print("오류: .env 파일에 DISCORD_BOT_TOKEN이 설정되지 않았습니다.")
    else:
        bot.run(config.discord_bot_token)
