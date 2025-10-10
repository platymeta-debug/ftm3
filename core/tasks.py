# core/tasks.py (모든 백그라운드 기능 통합본)

import discord
from discord.ext import tasks
from datetime import datetime, timezone, time, timedelta # timedelta 추가
from sqlalchemy import select
from core.event_bus import event_bus
import pandas as pd
import requests
import asyncio

import pandas as pd
import requests

# 핵심 모듈 임포트
#from database.manager import db_manager
from database.models import Signal, Trade, AccountSnapshot
from analysis.core_strategy import diagnose_market_regime, MarketRegime
from analysis.macro_analyzer import MacroRegime

class BackgroundTasks:
    def __init__(self, bot, db_manager):
        self.bot = bot
        self.config = bot.config
        self.db_manager = db_manager
        self.binance_client = bot.binance_client
        self.confluence_engine = bot.confluence_engine
        self.position_sizer = bot.position_sizer
        self.trading_engine = bot.trading_engine
        self.panel_message: discord.Message | None = None
        self.analysis_message: discord.Message | None = None
        self.latest_analysis_results = {}
        self.decision_log = []
        self.current_aggr_level = self.config.aggr_level
        utc_midnight = time(hour=0, minute=0, tzinfo=timezone.utc)
        try:
            self.daily_snapshot_loop.change_interval(time=[utc_midnight])
        except TypeError:
            self.daily_snapshot_loop.change_interval(time=utc_midnight)

    def start_all_tasks(self):
        self.panel_update_loop.start()
        self.data_collector_loop.start()
        self.trading_decision_loop.start()
        self.daily_snapshot_loop.start()

    def on_aggr_level_change(self, new_level: int):
        self.current_aggr_level = new_level

    # --- UI 및 헬퍼 함수들 (기존 main.py에서 완전 이전) ---

    def get_external_prices(self, symbol: str) -> str:
        upbit_symbol = f"KRW-{symbol.replace('USDT', '')}"
        price_str = ""
        try: # 바이낸스
            ticker = self.binance_client.futures_ticker(symbol=symbol)
            price = float(ticker['lastPrice'])
            change_pct = float(ticker['priceChangePercent'])
            price_str += f"📈 **바이낸스**: `${price:,.2f}` (`{change_pct:+.2f}%`)\n"
        except Exception:
            price_str += "📈 **바이낸스**: `N/A`\n"
        try: # 업비트
            response = requests.get(f"https://api.upbit.com/v1/ticker?markets={upbit_symbol}", timeout=2)
            data = response.json()[0]
            price = data['trade_price']
            change_pct = data['signed_change_rate'] * 100
            price_str += f"📉 **업비트**: `₩{price:,.0f}` (`{change_pct:+.2f}%`)"
        except Exception:
            price_str += "📉 **업비트**: `N/A`"
        return price_str
    
    def update_adaptive_aggression_level(self):
        """[지능형 로직] 시장 변동성을 분석하여 현재 공격성 레벨을 동적으로 조절합니다."""
        base_aggr_level = self.config.aggr_level
        try:
            # (main - 복사본.py의 로직을 그대로 가져오되, self를 사용하도록 수정)
            with self.db_manager.get_session() as session:
                latest_signal_tuple = session.execute(
                    select(Signal).where(Signal.symbol == "BTCUSDT").order_by(Signal.id.desc())
                ).first()

                if not latest_signal_tuple or not latest_signal_tuple[0].atr_1d:
                    if self.current_aggr_level != base_aggr_level:
                        print(f"[Adaptive] 데이터 부족. 공격성 레벨 복귀: {self.current_aggr_level} -> {base_aggr_level}")
                        self.current_aggr_level = base_aggr_level
                    return

                btc_signal = latest_signal_tuple[0] 
                mark_price_info = self.binance_client.futures_mark_price(symbol="BTCUSDT")
                current_price = float(mark_price_info['markPrice'])
                volatility = btc_signal.atr_1d / current_price
                if volatility > self.config.adaptive_volatility_threshold:
                    new_level = max(1, base_aggr_level - 2)
                    if new_level != self.current_aggr_level:
                        print(f"[Adaptive] 변동성 증가 감지({volatility:.2%})! 공격성 레벨 하향 조정: {self.current_aggr_level} -> {new_level}")
                        self.current_aggr_level = new_level
                else:
                    if self.current_aggr_level != base_aggr_level:
                        print(f"[Adaptive] 시장 안정. 공격성 레벨 복귀: {self.current_aggr_level} -> {base_aggr_level}")
                        self.current_aggr_level = base_aggr_level
        except Exception as e:
            print(f"🚨 적응형 레벨 조정 중 오류: {e}")
            self.current_aggr_level = base_aggr_level

    def get_panel_embed(self) -> discord.Embed:
        """[복원] SL/TP, 청산가 등 모든 상세 정보를 포함한 제어 패널을 생성합니다."""
        embed = discord.Embed(title="⚙️ 통합 관제 시스템", description="봇의 모든 상태를 확인하고 제어합니다.", color=0x2E3136)

        # --- 1. 핵심 상태 (기존과 동일) ---
        trade_mode_text = "🔴 **실시간 매매**" if not self.config.is_testnet else "🟢 **테스트넷**"
        auto_trade_text = "✅ **자동매매 ON**" if self.config.exec_active else "❌ **자동매매 OFF**"
        adaptive_text = "🧠 **자동 조절 ON**" if self.config.adaptive_aggr_enabled else "👤 **수동 설정**"
        embed.add_field(name="[핵심 상태]", value=f"{trade_mode_text}\n{auto_trade_text}\n{adaptive_text}", inline=True)

        symbols_text = f"**{', '.join(self.config.symbols)}**"
        base_aggr_text = f"**Level {self.config.aggr_level}**"
        current_aggr_text = f"**Level {self.current_aggr_level}**"
        if self.config.adaptive_aggr_enabled and self.config.aggr_level != self.current_aggr_level:
            status = " (⚠️위험)" if self.current_aggr_level < self.config.aggr_level else " (📈안정)"
            current_aggr_text += status
        embed.add_field(name="[현재 전략]", value=f"분석 대상: {symbols_text}\n기본 공격성: {base_aggr_text}\n현재 공격성: {current_aggr_text}", inline=True)

        # --- 2. API 기반 동적 정보 (상세 정보 포함하여 복원) ---
        try:
            account_info = self.binance_client.futures_account()
            positions_from_api = [p for p in account_info.get('positions', []) if float(p.get('positionAmt', 0)) != 0]

            total_balance = float(account_info.get('totalWalletBalance', 0.0))
            total_pnl = float(account_info.get('totalUnrealizedProfit', 0.0))
            pnl_color = "📈" if total_pnl >= 0 else "📉"

            embed.add_field(
                name="[포트폴리오]",
                value=f"💰 **총 자산**: `${total_balance:,.2f}`\n"
                    f"{pnl_color} **총 미실현 PnL**: `${total_pnl:,.2f}`\n"
                    f"📊 **운영 포지션**: **{len(positions_from_api)} / {self.config.max_open_positions}** 개",
                inline=False
            )

            if not positions_from_api:
                embed.add_field(name="[오픈된 포지션]", value="현재 오픈된 포지션이 없습니다.", inline=False)
            else:
                with self.db_manager.get_session() as db_session:
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

                        # ▼▼▼ [복원] SL/TP 및 청산가 정보 표시 로직 ▼▼▼
                        if trade_db and trade_db.stop_loss_price:
                            sl_price, tp_price = trade_db.stop_loss_price, trade_db.take_profit_price
                            mark_price = float(self.binance_client.futures_mark_price(symbol=symbol).get('markPrice', 0.0))

                            if mark_price > 0:
                                sl_dist_pct = (abs(mark_price - sl_price) / mark_price) * 100
                                tp_dist_pct = (abs(tp_price - mark_price) / mark_price) * 100
                                details_text += f"> **SL**: `${sl_price:,.2f}` (`{sl_dist_pct:.2f}%`)\n> **TP**: `${tp_price:,.2f}` (`{tp_dist_pct:.2f}%`)\n"
                            else:
                                details_text += f"> **SL**: `${sl_price:,.2f}`\n> **TP**: `${tp_price:,.2f}`\n"
                        else:
                            details_text += "> **SL/TP**: `(봇 관리 아님)`\n"

                        details_text += f"> **청산가**: " + (f"`${liq_price:,.2f}`" if liq_price > 0 else "`N/A`")
                        # ▲▲▲ [복원] ▲▲▲

                        embed.add_field(name=f"--- {symbol} ({side} x{leverage}) ---", value=details_text, inline=False)

        except Exception as e:
            embed.add_field(
                name="[포트폴리오 및 포지션]",
                value=f"⚠️ **API 오류:** 실시간 정보를 가져오는 데 실패했습니다.\n"
                    f"`오류 내용: {e}`",
                inline=False
            )

        embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
        return embed

    def get_analysis_embed(self) -> discord.Embed:
        """[복원] 모든 TF별 지표, 핵심 신호 등 상세 정보를 포함한 분석 상황판을 생성합니다."""
        embed = discord.Embed(title="📊 라이브 종합 상황판", color=0x4A90E2)
        if not self.latest_analysis_results:
            embed.description = "분석 데이터를 수집하고 있습니다..."
            return embed

        # --- 1. 종합 정보 섹션 (공포-탐욕, 핵심 신호) ---
        btc_data = self.latest_analysis_results.get("BTCUSDT", {})
        fng_index = btc_data.get("fng_index", "N/A")
        confluence = btc_data.get("confluence", "") # [복원] 핵심 신호 데이터 가져오기

        summary_text = f"**공포-탐욕 지수**: `{fng_index}`\n"
        if confluence: # [복원] 핵심 신호가 있을 경우에만 표시
            summary_text += f"**핵심 신호**: `{confluence}`"

        embed.add_field(name="--- 종합 시장 현황 ---", value=summary_text, inline=False)

        # --- 2. 코인별 상세 분석 ---
        for symbol, data in self.latest_analysis_results.items():
            # 실시간 시세
            price_text = self.get_external_prices(symbol)
            embed.add_field(name=f"--- {symbol} 실시간 시세 ---", value=price_text, inline=False)

            # 분석 정보 추출
            final_score = data.get("final_score", 0)
            market_regime = data.get("market_regime")
            regime_text = f"`{market_regime.value}`" if market_regime else "`N/A`"
            score_color = "🟢" if final_score > 0 else "🔴" if final_score < 0 else "⚪"

            # ▼▼▼ [복원] TF별 세부 점수 표시 로직 ▼▼▼
            tf_scores_data = {tf: sum(data.get("tf_breakdowns", {}).get(tf, {}).values()) for tf in self.config.analysis_timeframes}
            tf_summary = " ".join([f"`{tf}:{score}`" for tf, score in tf_scores_data.items()])
            total_tf_score = sum(tf_scores_data.values())
            # ▲▲▲ [복원] ▲▲▲

            # 분석 요약 필드 생성
            analysis_summary_field = (
                f"**시장 체제:** {regime_text}\n"
                f"**종합 점수:** {score_color} **{final_score:.2f}**\n"
                f"**TF별 점수:** {tf_summary} (총점: `{total_tf_score}`)" # [복원] TF별 점수 필드 추가
            )
            embed.add_field(name="--- 분석 요약 ---", value=analysis_summary_field, inline=False)

            # ▼▼▼ [복원] 모든 타임프레임의 주요 지표 표시 로직 ▼▼▼
            all_tf_indicators = ""
            for tf in self.config.analysis_timeframes:
                rows = data.get("tf_rows", {}).get(tf)
                if rows is not None and not rows.empty:
                    rsi = rows.get('RSI_14', 0)
                    adx = rows.get('ADX_14', 0)
                    mfi = rows.get('MFI_14', 0)
                    all_tf_indicators += f"**{tf.upper()}**: `RSI {rsi:.1f}` `ADX {adx:.1f}` `MFI {mfi:.1f}`\n"

            if not all_tf_indicators:
                all_tf_indicators = "주요 지표 데이터 수집 중..."

            embed.add_field(name="--- 모든 시간대 주요 지표 ---", value=all_tf_indicators.strip(), inline=False)
            # ▲▲▲ [복원] ▲▲▲

        # --- 3. 매매 결정 로그 ---
        if self.decision_log:
            log_text = "\n".join(self.decision_log)
            embed.add_field(name="--- 최근 매매 결정 로그 ---", value=log_text, inline=False)

        embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
        return embed

    # --- 백그라운드 루프들 ---
    @tasks.loop()
    async def daily_snapshot_loop(self):
        """매일 자정(UTC)에 현재 계좌 총자산을 DB에 기록합니다."""
        print("📸 일일 계좌 스냅샷 기록 시간입니다...")
        try:
            account_info = self.binance_client.futures_account()
            total_balance = float(account_info.get('totalWalletBalance', 0.0))
            if total_balance > 0:
                with self.db_manager.get_session() as session:
                    snapshot = AccountSnapshot(total_balance=total_balance)
                    session.add(snapshot)
                    session.commit()
                    print(f"✅ 계좌 스냅샷 저장 완료: ${total_balance:,.2f}")
        except Exception as e:
            print(f"🚨 일일 스냅샷 기록 중 오류 발생: {e}")

    async def check_circuit_breaker(self) -> bool:
        """
        DB에 저장된 계좌 스냅샷을 기반으로 서킷 브레이커 발동 여부를 확인합니다.
        :return: True이면 서킷 브레이커 발동, False이면 정상.
        """
        if not self.config.circuit_breaker_enabled:
            return False

        with self.db_manager.get_session() as session:
            check_period = datetime.now(timezone.utc) - timedelta(days=self.config.drawdown_check_days)
            snapshots = session.execute(
                select(AccountSnapshot)
                .where(AccountSnapshot.timestamp >= check_period)
                .order_by(AccountSnapshot.timestamp.desc())
            ).scalars().all()

            if len(snapshots) < 2: # 비교할 데이터가 부족
                return False

            peak_balance = max(s.total_balance for s in snapshots)
            current_balance = snapshots[0].total_balance
            drawdown = (peak_balance - current_balance) / peak_balance * 100

            if drawdown >= self.config.drawdown_threshold_pct:
                print(f"🚨 서킷 브레이커 발동! 최대 손실 허용치 도달 ({drawdown:.2f}% >= {self.config.drawdown_threshold_pct}%)")
                self.config.exec_active = False # 자동매매 강제 중지

                alerts_channel = self.bot.get_channel(self.config.alerts_channel_id)
                if alerts_channel:
                    embed = discord.Embed(
                        title="🛡️ 서킷 브레이커 발동 🛡️",
                        description="계좌 보호를 위해 모든 신규 자동매매를 중단합니다.",
                        color=0xFF0000
                    )
                    embed.add_field(name="감지된 손실률", value=f"`{drawdown:.2f}%`", inline=True)
                    embed.add_field(name="설정된 임계값", value=f"`{self.config.drawdown_threshold_pct}%`", inline=True)
                    await alerts_channel.send(embed=embed)
                return True
        return False
    
    @tasks.loop(seconds=15)
    async def panel_update_loop(self):
        if self.panel_message:
            try:
                await self.panel_message.edit(embed=self.get_panel_embed())
            except discord.errors.NotFound:
                print("패널 메시지를 찾을 수 없어 루프를 중지합니다.")
                self.panel_update_loop.stop()
            except Exception as e:
                print(f"🚨 패널 업데이트 중 오류: {e}")

    @tasks.loop(minutes=1)
    async def data_collector_loop(self):
        print(f"\n--- [Data Collector] 분석 시작: {datetime.now().strftime('%H:%M:%S')} ---")

        # ▼▼▼ [최종 수정] 봇을 멈추게 하는 분석 로직을 별도 스레드에서 실행하도록 변경 ▼▼▼
        def blocking_analysis(db_manager_instance, config_instance, confluence_engine_instance):
            results = {}
            try:
                with db_manager_instance.get_session() as session:
                    for symbol in config_instance.symbols:
                        analysis_result = confluence_engine_instance.analyze_symbol(symbol)
                        if not analysis_result:
                            results.pop(symbol, None)
                            continue
                        
                        final_score, tf_scores, tf_rows, tf_breakdowns, fng, confluence = analysis_result
                        
                        daily_row = tf_rows.get("1d")
                        four_hour_row = tf_rows.get("4h")
                        market_regime = MarketRegime.SIDEWAYS
                        if daily_row is not None and not daily_row.empty and four_hour_row is not None and not four_hour_row.empty:
                            market_data = pd.Series({
                                'adx_4h': four_hour_row.get('ADX_14'),
                                'is_above_ema200_1d': daily_row.get('close') > daily_row.get('EMA_200') if pd.notna(daily_row.get('EMA_200')) else False
                            })
                            market_regime = diagnose_market_regime(market_data, self.config.market_regime_adx_th)

                        results[symbol] = {
                            "final_score": final_score, "tf_rows": tf_rows,
                            "tf_breakdowns": tf_breakdowns, "market_regime": market_regime,
                            "fng_index": fng, "confluence": confluence
                        }
                    session.commit()
                return results
            except Exception as e:
                print(f"🚨 데이터 수집 스레드 내부 오류: {e}")
                return {}

        loop = asyncio.get_event_loop()
        analysis_results = await loop.run_in_executor(None, blocking_analysis, self.db_manager, self.config, self.confluence_engine)
        self.latest_analysis_results.update(analysis_results)
        # ▲▲▲ [최종 수정] ▲▲▲

        try:
            channel = self.bot.get_channel(self.config.analysis_channel_id)
            if channel:
                embed = self.get_analysis_embed()
                if self.analysis_message:
                    await self.analysis_message.edit(embed=embed)
                else:
                    self.analysis_message = await channel.send(embed=embed)
        except Exception as e:
            print(f"🚨 분석 상황판 업데이트 중 오류: {e}")

    @tasks.loop(minutes=5)
    async def trading_decision_loop(self):
        log_message = f"`{datetime.now().strftime('%H:%M:%S')}`: "
        if await self.check_circuit_breaker():
            log_message += "🚨 서킷 브레이커 발동 상태. 매매 중단."
            self.decision_log.insert(0, log_message)
            if len(self.decision_log) > 5: self.decision_log.pop()
            print(log_message)
            return

        if not self.config.exec_active:
            log_message += "자동매매 OFF 상태. 관망."
        else:
            current_macro_regime = MacroRegime.SIDEWAYS
            try:
                regime, macro_score, _ = self.confluence_engine.macro_analyzer.diagnose_macro_regime()
                current_macro_regime = regime
                log_message += f"시장 진단: **{current_macro_regime.value}** (점수: {macro_score}) | "
                if current_macro_regime == MacroRegime.BEAR:
                    log_message += "🚨 약세장 감지, 신규 진입 중단."
                    self.decision_log.insert(0, log_message)
                    if len(self.decision_log) > 5: self.decision_log.pop()
                    print(log_message)
                    # 약세장에서는 포지션 관리만 하고 신규 진입은 시도하지 않음
                    with self.db_manager.get_session() as session:
                        open_trades = session.execute(select(Trade).where(Trade.status == "OPEN")).scalars().all()
                        if open_trades:
                            await self.manage_open_positions(session, open_trades, current_macro_regime.name)
                    return

            except Exception as e:
                log_message += f"⚠️ 거시 분석 실패: {e}. 중립({current_macro_regime.value})으로 진행 | "

            if self.config.adaptive_aggr_enabled:
                self.update_adaptive_aggression_level()

            log_message += f"[Lvl:{self.current_aggr_level}] 의사결정 시작. "
            try:
                with self.db_manager.get_session() as session:
                    open_trades = session.execute(select(Trade).where(Trade.status == "OPEN")).scalars().all()
                    if open_trades:
                        log_message += f"{len(open_trades)}개 포지션 관리. "
                        await self.manage_open_positions(session, open_trades, current_macro_regime.name)

                    open_positions_count = len(open_trades)
                    symbols_in_trade = {t.symbol for t in open_trades}

                    decision_reason = await self.find_new_entry_opportunities(session, open_positions_count, symbols_in_trade, current_macro_regime.name)
                    log_message += decision_reason
            except Exception as e:
                log_message += f"🚨 루프 중 심각한 오류: {e}"
                print(f"🚨 의사결정 루프 중 심각한 오류 발생: {e}", exc_info=True)

        self.decision_log.insert(0, log_message)
        if len(self.decision_log) > 5: self.decision_log.pop()
        print(log_message)

    # --- 트레이딩 로직 헬퍼 함수들 ---
    async def event_handler_loop(self):
        """이벤트 버스에서 이벤트를 구독하고, 디스코드로 실시간 알림을 보냅니다."""
        print("이벤트 핸들러 루프가 시작되었습니다. 알림 대기 중...")
        while True:
            try:
                event = await event_bus.subscribe()
                event_type = event.get("type")
                data = event.get("data", {})

                alerts_channel = self.bot.get_channel(self.config.alerts_channel_id)
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
                    # PnL 계산을 위한 안전장치 추가
                    initial_investment = trade.entry_price * trade.quantity
                    pnl_percent = (trade.pnl / initial_investment * 100) if initial_investment > 0 else 0

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
                
    async def manage_open_positions(self, session, open_trades, market_regime: str):
        """[Phase 1 최종] 분할 익절 및 추적 손절매 기능이 통합된 포지션 관리 로직입니다."""
        for trade in list(open_trades): # 안전한 순회를 위해 list로 복사
            try:
                mark_price = float(self.binance_client.futures_mark_price(symbol=trade.symbol).get('markPrice', 0.0))
                if mark_price == 0.0:
                    print(f"[{trade.symbol}] 현재가를 가져올 수 없어 건너뜁니다.")
                    continue

                # 1. 포지션의 최고(최저)가 갱신
                if trade.side == "BUY" and (trade.highest_price_since_entry is None or mark_price > trade.highest_price_since_entry):
                    trade.highest_price_since_entry = mark_price
                elif trade.side == "SELL" and (trade.highest_price_since_entry is None or mark_price < trade.highest_price_since_entry):
                    trade.highest_price_since_entry = mark_price

                # 2. 분할 익절 (Scale-Out) 로직
                if not trade.is_scaled_out:
                    # R/R Ratio는 코인별로 다르므로 .env에서 가져오도록 수정
                    risk_reward_ratio = config.get_strategy_params(trade.symbol).get('risk_reward_ratio', 2.0)
                    scale_out_target_price = trade.entry_price + (trade.take_profit_price - trade.entry_price) / risk_reward_ratio
                    
                    if (trade.side == "BUY" and mark_price >= scale_out_target_price) or \
                       (trade.side == "SELL" and mark_price <= scale_out_target_price):
                        
                        quantity_to_close = trade.quantity / 2
                        print(f"💰 [{trade.symbol}] 1차 목표 도달! 50% 분할 익절 실행.")
                        await self.trading_engine.close_position(trade, "자동 분할 익절", quantity_to_close=quantity_to_close)
                        
                        trade.is_scaled_out = True
                        trade.stop_loss_price = trade.entry_price
                        session.commit()
                        print(f"🛡️ [{trade.symbol}] 무위험 포지션 전환 완료. SL을 본전(${trade.entry_price:,.2f})으로 변경.")
                        continue

                # 3. 추적 손절매 (Trailing Stop Loss) 로직 (분할 익절 완료 포지션에만 적용)
                if trade.is_scaled_out:
                    latest_signal = session.execute(select(Signal).where(Signal.symbol == trade.symbol).order_by(Signal.id.desc())).scalar_one_or_none()
                    if latest_signal and latest_signal.atr_4h and latest_signal.atr_4h > 0:
                        atr = latest_signal.atr_4h
                        new_stop_loss = 0

                        if trade.side == "BUY":
                            new_stop_loss = trade.highest_price_since_entry - (atr * self.config.trailing_stop_atr_multiplier)
                            if new_stop_loss > trade.stop_loss_price:
                                trade.stop_loss_price = new_stop_loss
                                print(f"📈 [{trade.symbol}] 추적 손절(Long): SL 상향 조정 -> ${new_stop_loss:,.2f}")

                        elif trade.side == "SELL":
                            new_stop_loss = trade.highest_price_since_entry + (atr * self.config.trailing_stop_atr_multiplier)
                            if new_stop_loss < trade.stop_loss_price:
                                trade.stop_loss_price = new_stop_loss
                                print(f"📉 [{trade.symbol}] 추적 손절(Short): SL 하향 조정 -> ${new_stop_loss:,.2f}")
                
                # 4. 최종 익절(TP) / 손절(SL) 로직
                if trade.take_profit_price and ((trade.side == "BUY" and mark_price >= trade.take_profit_price) or \
                                               (trade.side == "SELL" and mark_price <= trade.take_profit_price)):
                    await self.trading_engine.close_position(trade, f"자동 최종 익절 (TP: ${trade.take_profit_price:,.2f})")
                    continue

                if trade.stop_loss_price and ((trade.side == "BUY" and mark_price <= trade.stop_loss_price) or \
                                             (trade.side == "SELL" and mark_price >= trade.stop_loss_price)):
                    await self.trading_engine.close_position(trade, f"자동 손절 (SL: ${trade.stop_loss_price:,.2f})")
                    continue
                
                session.commit()

            except Exception as e:
                print(f"🚨 포지션 관리 중 오류 ({trade.symbol}): {e}")
                session.rollback()

    async def find_new_entry_opportunities(self, session, open_positions_count, symbols_in_trade, market_regime: str):
        if open_positions_count >= self.config.max_open_positions:
            return f"슬롯 부족 ({open_positions_count}/{self.config.max_open_positions}). 관망."

        valid_opportunities = []
        for symbol in self.config.symbols:
            if symbol in symbols_in_trade:
                continue
            
            # ... (최근 신호 조회 로직은 동일) ...
            recent_signals = session.execute(select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc()).limit(self.config.trend_entry_confirm_count)).scalars().all()
            if len(recent_signals) < self.config.trend_entry_confirm_count: continue
            recent_scores = [s.final_score for s in recent_signals]
            
            # ConfluenceEngine의 analyze_and_decide 호출 시 market_regime 전달
            side, reason, context = self.confluence_engine.analyze_and_decide(symbol, recent_scores, market_regime)
            
            if side and context:
                opportunity = {
                    "symbol": symbol, "side": side, "reason": reason, "context": context,
                    "avg_score": context.get("avg_score", 0), "signal_id": recent_signals[0].id
                }
                valid_opportunities.append(opportunity)

        if not valid_opportunities:
            return "탐색 완료, 신규 진입 기회 없음."
        
        best_opportunity = sorted(valid_opportunities, key=lambda x: abs(x["avg_score"]), reverse=True)[0]
        
        # Position Sizer 호출 시에도 market_regime 전달
        leverage = self.position_sizer.get_leverage_for_symbol(best_opportunity["symbol"], self.current_aggr_level)
        quantity = self.position_sizer.calculate_position_size(
            symbol=best_opportunity["symbol"], 
            atr=best_opportunity["context"]['entry_atr'], 
            aggr_level=self.current_aggr_level,
            open_positions_count=open_positions_count,
            average_score=best_opportunity["avg_score"],
            market_regime=market_regime # market_regime 전달
        )
        
        if quantity:
            context = best_opportunity["context"]
            context['signal_id'] = best_opportunity["signal_id"]
            # trading_engine place_order_with_bracket 함수가 별도로 있다면 그 함수를 호출
            # 여기서는 trading_engine의 다른 함수를 호출한다고 가정
            await self.trading_engine.open_with_bracket(
                symbol=best_opportunity["symbol"],
                side=best_opportunity["side"],
                quantity=quantity,
                entry_atr=context['entry_atr'],
                extra=context
            )
            return f"🏆 최고 점수 신호 선택: {best_opportunity['reason']}"
        else:
            return f"[{best_opportunity['symbol']}]: 포지션 규모 계산 실패로 진입 보류."
