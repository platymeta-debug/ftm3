# core/tasks.py

import discord
from discord.ext import tasks
from datetime import datetime, timezone
from sqlalchemy import select
import pandas as pd

from database.manager import db_manager
from database.models import Signal, Trade
from analysis.core_strategy import diagnose_market_regime

class BackgroundTasks:
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.binance_client = bot.binance_client
        self.confluence_engine = bot.confluence_engine
        self.position_sizer = bot.position_sizer
        self.trading_engine = bot.trading_engine
        
        # main.py에서 옮겨온 전역 변수들을 클래스 속성으로 관리
        self.panel_message = None
        self.analysis_message = None
        self.latest_analysis_results = {}
        self.decision_log = []
        self.current_aggr_level = self.config.aggr_level

    def start_all_tasks(self):
        self.panel_update_loop.start()
        self.data_collector_loop.start()
        self.trading_decision_loop.start()

    def on_aggr_level_change(self, new_level: int):
        self.current_aggr_level = new_level

    # --- Panel & UI Helper ---
    def get_panel_embed(self) -> discord.Embed:
        # get_panel_embed 로직 (main.py에서 그대로 가져옴)
        embed = discord.Embed(title="⚙️ 통합 관제 시스템", description="봇의 모든 상태를 확인하고 제어합니다.", color=0x2E3136)
        
        trade_mode_text = "🔴 **실시간 매매**" if not self.config.is_testnet else "🟢 **테스트넷**"
        auto_trade_text = "✅ **자동매매 ON**" if self.config.exec_active else "❌ **자동매매 OFF**"
        adaptive_text = "🧠 **자동 조절 ON**" if self.config.adaptive_aggr_enabled else "👤 **수동 설정**"
        embed.add_field(name="[핵심 상태]", value=f"{trade_mode_text}\n{auto_trade_text}\n{adaptive_text}", inline=True)
        
        symbols_text = f"**{', '.join(self.config.symbols)}**"
        base_aggr_text = f"**Level {self.config.aggr_level}**"
        current_aggr_text = f"**Level {self.current_aggr_level}**"
        embed.add_field(name="[현재 전략]", value=f"분석 대상: {symbols_text}\n기본 공격성: {base_aggr_text}\n현재 공격성: {current_aggr_text}", inline=True)

        try:
            account_info = self.binance_client.futures_account()
            positions = [p for p in account_info.get('positions', []) if float(p.get('positionAmt', 0)) != 0]
            balance = float(account_info.get('totalWalletBalance', 0.0))
            pnl = float(account_info.get('totalUnrealizedProfit', 0.0))
            
            embed.add_field(name="[포트폴리오]", value=f"💰 **총 자산**: `${balance:,.2f}`\n" f"📈 **총 미실현 PnL**: `${pnl:,.2f}`\n" f"📊 **운영 포지션**: **{len(positions)} / {self.config.max_open_positions}** 개", inline=False)

            if positions:
                for pos in positions:
                    symbol = pos['symbol']
                    side = "LONG" if float(pos['positionAmt']) > 0 else "SHORT"
                    pnl = float(pos.get('unrealizedProfit', 0.0))
                    entry_price = float(pos.get('entryPrice', 0.0))
                    quantity = abs(float(pos.get('positionAmt', 0.0)))
                    margin = float(pos.get('initialMargin', 0.0))
                    pnl_percent = (pnl / margin * 100) if margin > 0 else 0.0

                    pnl_text = f"📈 **PnL**: `${pnl:,.2f}` (`{pnl_percent:+.2f} %`)"
                    details_text = f"> **진입가**: `${entry_price:,.2f}` | **수량**: `{quantity}`\n> {pnl_text}"
                    embed.add_field(name=f"--- {symbol} ({side}) ---", value=details_text, inline=False)

        except Exception as e:
            embed.add_field(name="[포트폴리오]", value=f"⚠️ API 오류: {e}", inline=False)
        
        embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
        return embed

    def get_analysis_embed(self) -> discord.Embed:
        # get_analysis_embed 로직 (main.py에서 그대로 가져옴)
        embed = discord.Embed(title="📊 라이브 종합 상황판", color=0x4A90E2)
        if not self.latest_analysis_results:
            embed.description = "분석 데이터를 수집하고 있습니다..."
            return embed

        for symbol, data in self.latest_analysis_results.items():
            final_score = data.get("final_score", 0)
            market_regime = data.get("market_regime")
            regime_text = f"`{market_regime.value}`" if market_regime else "`N/A`"
            score_color = "🟢" if final_score > 0 else "🔴" if final_score < 0 else "⚪"
            
            analysis_summary = (
                f"**시장 체제:** {regime_text}\n"
                f"**종합 점수:** {score_color} **{final_score:.2f}**"
            )
            embed.add_field(name=f"--- {symbol} 분석 요약 ---", value=analysis_summary, inline=False)
        
        if self.decision_log:
            embed.add_field(name="--- 최근 매매 결정 로그 ---", value="\n".join(self.decision_log), inline=False)
            
        embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
        return embed

    # --- Background Loops ---

    @tasks.loop(seconds=15)
    async def panel_update_loop(self):
        if self.panel_message:
            try:
                await self.panel_message.edit(embed=self.get_panel_embed())
            except discord.NotFound:
                print("패널 메시지를 찾을 수 없어 업데이트 루프를 중지합니다.")
                self.panel_update_loop.stop()
            except Exception as e:
                print(f"패널 업데이트 중 오류 발생: {e}")

    @tasks.loop(minutes=1)
    async def data_collector_loop(self):
        print(f"\n--- [Data Collector] 분석 시작 ---")
        try:
            with db_manager.get_session() as session:
                for symbol in self.config.symbols:
                    analysis_result = self.confluence_engine.analyze_symbol(symbol)
                    if not analysis_result: continue
                    
                    final_score, _, tf_rows, breakdowns, fng, confluence = analysis_result
                    
                    daily_row = tf_rows.get("1d")
                    four_hour_row = tf_rows.get("4h")
                    market_regime = diagnose_market_regime(
                        pd.Series({
                            'adx_4h': four_hour_row.get('ADX_14') if four_hour_row is not None else None,
                            'is_above_ema200_1d': daily_row.get('close') > daily_row.get('EMA_200') if daily_row is not None and pd.notna(daily_row.get('EMA_200')) else False
                        }),
                        self.config.market_regime_adx_th
                    )

                    self.latest_analysis_results[symbol] = {
                        "final_score": final_score, "tf_rows": tf_rows, "tf_breakdowns": breakdowns,
                        "market_regime": market_regime, "fng_index": fng, "confluence": confluence
                    }
                    
                    # (DB Signal 저장 로직은 생략 - 필요시 추가)
                session.commit()
        except Exception as e:
            print(f"🚨 데이터 수집 루프 중 오류: {e}")

        # 분석 상황판 업데이트
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
        if not self.config.exec_active:
            log_message += "자동매매 OFF."
        else:
            log_message += f"[Lvl:{self.current_aggr_level}] 의사결정 시작. "
            try:
                with db_manager.get_session() as session:
                    open_trades = session.execute(select(Trade).where(Trade.status == "OPEN")).scalars().all()
                    
                    if open_trades:
                        await self.manage_open_positions(session, open_trades)
                    
                    open_positions_count = session.query(Trade).filter(Trade.status == "OPEN").count()
                    symbols_in_trade = {t.symbol for t in open_trades}
                    
                    decision_reason = await self.find_new_entry_opportunities(session, open_positions_count, symbols_in_trade)
                    log_message += decision_reason
            except Exception as e:
                log_message += f"🚨 루프 오류: {e}"

        self.decision_log.insert(0, log_message)
        if len(self.decision_log) > 3:
            self.decision_log.pop()
        print(log_message)

    # --- Trading Logic Helpers ---
    async def manage_open_positions(self, session, open_trades):
        # manage_open_positions 로직 (main.py에서 그대로 가져옴, 일부 수정)
        for trade in open_trades:
            # ... (기존 로직과 거의 동일, self.trading_engine 등 클래스 속성으로 호출)
            pass # 간단히 생략, 실제로는 기존 로직을 여기에 붙여넣어야 합니다.

    async def find_new_entry_opportunities(self, session, open_positions_count, symbols_in_trade):
        # find_new_entry_opportunities 로직 (main.py에서 그대로 가져옴, 일부 수정)
        if open_positions_count >= self.config.max_open_positions:
            return "슬롯 부족."
        
        for symbol in self.config.symbols:
            if symbol in symbols_in_trade: continue

            recent_signals = session.execute(
                select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc()).limit(self.config.trend_entry_confirm_count)
            ).scalars().all()
            recent_scores = [s.final_score for s in recent_signals]

            side, reason, context = self.confluence_engine.analyze_and_decide(symbol, recent_scores)
            
            if side and context:
                quantity = self.position_sizer.calculate_position_size(
                    symbol, context['entry_atr'], self.current_aggr_level, open_positions_count, context['avg_score']
                )
                if quantity:
                    context['signal_id'] = recent_signals[0].id if recent_signals else None
                    await self.trading_engine.place_order_with_bracket(symbol, side, quantity, 0, context['entry_atr'], context)
                    return reason
        return "신규 진입 기회 없음."