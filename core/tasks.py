# core/tasks.py (모든 백그라운드 기능 통합본)

import discord
from discord.ext import tasks
from datetime import datetime, timezone
from sqlalchemy import select
import pandas as pd
import requests

# 핵심 모듈 임포트
from database.manager import db_manager
from database.models import Signal, Trade
from analysis.core_strategy import diagnose_market_regime, MarketRegime

class BackgroundTasks:
    def __init__(self, bot):
        self.bot = bot
        # main.py의 bot 객체로부터 핵심 요소들을 가져와 클래스 속성으로 만듭니다.
        self.config = bot.config
        self.binance_client = bot.binance_client
        self.confluence_engine = bot.confluence_engine
        self.position_sizer = bot.position_sizer
        self.trading_engine = bot.trading_engine
        
        # main.py에서 사용하던 전역 변수들을 클래스 속성으로 이전합니다.
        self.panel_message: discord.Message = None
        self.analysis_message: discord.Message = None
        self.latest_analysis_results = {}
        self.decision_log = []
        self.current_aggr_level = self.config.aggr_level

    def start_all_tasks(self):
        """모든 백그라운드 루프를 시작합니다."""
        self.panel_update_loop.start()
        self.data_collector_loop.start()
        self.trading_decision_loop.start()

    def on_aggr_level_change(self, new_level: int):
        """공격성 레벨 변경 콜백 함수입니다."""
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

    def get_panel_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚙️ 통합 관제 시스템", description="봇의 모든 상태를 확인하고 제어합니다.", color=0x2E3136)
        
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

        try:
            account_info = self.binance_client.futures_account()
            positions_from_api = [p for p in account_info.get('positions', []) if float(p.get('positionAmt', 0)) != 0]
            total_balance = float(account_info.get('totalWalletBalance', 0.0))
            total_pnl = float(account_info.get('totalUnrealizedProfit', 0.0))
            pnl_color = "📈" if total_pnl >= 0 else "📉"
            
            embed.add_field(name="[포트폴리오]", value=f"💰 **총 자산**: `${total_balance:,.2f}`\n{pnl_color} **총 미실현 PnL**: `${total_pnl:,.2f}`\n📊 **운영 포지션**: **{len(positions_from_api)} / {self.config.max_open_positions}** 개", inline=False)

            if not positions_from_api:
                embed.add_field(name="[오픈된 포지션]", value="현재 오픈된 포지션이 없습니다.", inline=False)
            else:
                with db_manager.get_session() as db_session:
                    for pos in positions_from_api:
                        symbol = pos.get('symbol')
                        if not symbol: continue

                        pnl = float(pos.get('unrealizedProfit', 0.0))
                        side = "LONG" if float(pos.get('positionAmt', 0.0)) > 0 else "SHORT"
                        quantity = abs(float(pos.get('positionAmt', 0.0)))
                        entry_price = float(pos.get('entryPrice', 0.0))
                        margin = float(pos.get('initialMargin', 0.0))
                        pnl_percent = (pnl / margin * 100) if margin > 0 else 0.0
                        
                        pnl_text = f"📈 **PnL**: `${pnl:,.2f}` (`{pnl_percent:+.2f} %`)" if pnl >= 0 else f"📉 **PnL**: `${pnl:,.2f}` (`{pnl_percent:+.2f} %`)"
                        details_text = f"> **진입가**: `${entry_price:,.2f}` | **수량**: `{quantity}`\n> {pnl_text}"
                        embed.add_field(name=f"--- {symbol} ({side}) ---", value=details_text, inline=False)
        except Exception as e:
            embed.add_field(name="[포트폴리오 및 포지션]", value=f"⚠️ **API 오류:** 실시간 정보를 가져오는 데 실패했습니다.\n`오류 내용: {e}`", inline=False)
        
        embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
        return embed

    def get_analysis_embed(self) -> discord.Embed:
        embed = discord.Embed(title="📊 라이브 종합 상황판", color=0x4A90E2)
        if not self.latest_analysis_results:
            embed.description = "분석 데이터를 수집하고 있습니다..."
            return embed

        btc_data = self.latest_analysis_results.get("BTCUSDT", {})
        fng_index = btc_data.get("fng_index", "N/A")
        summary_text = f"**공포-탐욕 지수**: `{fng_index}`"
        embed.add_field(name="--- 종합 시장 현황 ---", value=summary_text, inline=False)
        
        for symbol, data in self.latest_analysis_results.items():
            price_text = self.get_external_prices(symbol)
            embed.add_field(name=f"--- {symbol} 실시간 시세 ---", value=price_text, inline=False)
            
            final_score = data.get("final_score", 0)
            market_regime = data.get("market_regime")
            regime_text = f"`{market_regime.value}`" if market_regime else "`N/A`"
            score_color = "🟢" if final_score > 0 else "🔴" if final_score < 0 else "⚪"
            
            analysis_summary = f"**시장 체제:** {regime_text}\n**종합 점수:** {score_color} **{final_score:.2f}**"
            embed.add_field(name="--- 분석 요약 ---", value=analysis_summary, inline=False)

        if self.decision_log:
            embed.add_field(name="--- 최근 매매 결정 로그 ---", value="\n".join(self.decision_log), inline=False)
            
        embed.set_footer(text=f"최종 업데이트: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}")
        return embed

    # --- 백그라운드 루프들 ---

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
        try:
            with db_manager.get_session() as session:
                for symbol in self.config.symbols:
                    analysis_result = self.confluence_engine.analyze_symbol(symbol)
                    if not analysis_result:
                        self.latest_analysis_results.pop(symbol, None) # 데이터가 없으면 제거
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

                    self.latest_analysis_results[symbol] = {
                        "final_score": final_score, "tf_rows": tf_rows,
                        "tf_breakdowns": tf_breakdowns, "market_regime": market_regime,
                        "fng_index": fng, "confluence": confluence
                    }
                session.commit()
        except Exception as e:
            print(f"🚨 데이터 수집 루프 중 오류: {e}")

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
            log_message += "자동매매 OFF 상태. 의사결정을 건너뜁니다."
        else:
            log_message += f"[Lvl:{self.current_aggr_level}] 의사결정 사이클 시작. "
            try:
                with db_manager.get_session() as session:
                    open_trades = session.execute(select(Trade).where(Trade.status == "OPEN")).scalars().all()
                    
                    if open_trades:
                        log_message += f"{len(open_trades)}개 포지션 관리 실행. "
                        await self.manage_open_positions(session, open_trades)
                    
                    open_positions_count = session.query(Trade).filter(Trade.status == "OPEN").count()
                    symbols_in_trade = {t.symbol for t in open_trades}
                    
                    decision_reason = await self.find_new_entry_opportunities(session, open_positions_count, symbols_in_trade)
                    log_message += decision_reason
            except Exception as e:
                log_message += f"🚨 루프 중 심각한 오류 발생: {e}"
                print(f"🚨 의사결정 루프 중 심각한 오류 발생: {e}")

        self.decision_log.insert(0, log_message)
        if len(self.decision_log) > 5: # 로그 보관 개수 증가
            self.decision_log.pop()
        print(log_message)

    # --- 트레이딩 로직 헬퍼 함수들 ---

    async def manage_open_positions(self, session, open_trades):
        """현재 오픈된 포지션들을 시나리오에 따라 관리합니다."""
        for trade in list(open_trades):
            try:
                mark_price = float(self.binance_client.futures_mark_price(symbol=trade.symbol).get('markPrice', 0.0))
                if mark_price == 0.0: continue

                # 최종 익절/손절 로직
                if trade.take_profit_price and ((trade.side == "BUY" and mark_price >= trade.take_profit_price) or (trade.side == "SELL" and mark_price <= trade.take_profit_price)):
                    await self.trading_engine.close_position(trade, f"자동 최종 익절 (TP: ${trade.take_profit_price:,.2f})")
                    continue

                if trade.stop_loss_price and ((trade.side == "BUY" and mark_price <= trade.stop_loss_price) or (trade.side == "SELL" and mark_price >= trade.stop_loss_price)):
                    await self.trading_engine.close_position(trade, f"자동 손절 (SL: ${trade.stop_loss_price:,.2f})")
                    continue
            except Exception as e:
                print(f"포지션 관리 중 오류 ({trade.symbol}): {e}")

    async def find_new_entry_opportunities(self, session, open_positions_count, symbols_in_trade):
        """신규 진입 기회를 탐색하고, 조건 충족 시 주문을 실행합니다."""
        if open_positions_count >= self.config.max_open_positions:
            return f"슬롯 부족 ({open_positions_count}/{self.config.max_open_positions}). 관망."
        
        for symbol in self.config.symbols:
            if symbol in symbols_in_trade: continue

            recent_signals = session.execute(select(Signal).where(Signal.symbol == symbol).order_by(Signal.id.desc()).limit(self.config.trend_entry_confirm_count)).scalars().all()
            if len(recent_signals) < self.config.trend_entry_confirm_count:
                continue

            recent_scores = [s.final_score for s in recent_signals]
            side, reason, context = self.confluence_engine.analyze_and_decide(symbol, recent_scores)
            
            if side and context:
                leverage = self.position_sizer.get_leverage_for_symbol(symbol, self.current_aggr_level)
                quantity = self.position_sizer.calculate_position_size(
                    symbol, context['entry_atr'], self.current_aggr_level, open_positions_count, context['avg_score']
                )
                if quantity:
                    context['signal_id'] = recent_signals[0].id
                    await self.trading_engine.place_order_with_bracket(symbol, side, quantity, leverage, context['entry_atr'], context)
                    return reason # 성공 시 루프 종료 및 리턴
        return "탐색 완료, 신규 진입 기회 없음."
