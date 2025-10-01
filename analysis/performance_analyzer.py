from __future__ import annotations

import pandas as pd
from sqlalchemy import select

from database.manager import db_manager
from database.models import Signal, Trade


class PerformanceAnalyzer:
    """Analyze historical trades to surface actionable insights."""

    def __init__(self) -> None:
        print("성과 분석 엔진이 초기화되었습니다.")

    def generate_report(self) -> dict | None:
        """Analyze stored trades and generate an aggregated performance report."""
        session = db_manager.get_session()
        try:
            query = (
                select(Trade, Signal)
                .join(Signal, Trade.signal_id == Signal.id)
                .where(Trade.status == "CLOSED")
            )
            results = session.execute(query).all()
        finally:
            session.close()

        if len(results) < 10:
            return None

        records: list[dict] = []
        for trade, signal in results:
            records.append(
                {
                    "pnl": trade.pnl,
                    "side": trade.side,
                    "final_score": signal.final_score,
                    "score_4h": signal.score_4h,
                    "score_1d": signal.score_1d,
                }
            )

        df = pd.DataFrame(records)

        total_trades = len(df)
        winning_trades = df[df["pnl"] > 0]
        losing_trades = df[df["pnl"] <= 0]
        win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0

        avg_profit = winning_trades["pnl"].mean()
        avg_loss = losing_trades["pnl"].abs().mean()
        profit_factor = (
            winning_trades["pnl"].sum() / losing_trades["pnl"].abs().sum()
            if losing_trades["pnl"].abs().sum() > 0
            else 0
        )

        insights: list[str] = []

        high_score_trades = df[df["final_score"].abs() > 15]
        if not high_score_trades.empty:
            high_score_win_rate = (high_score_trades["pnl"] > 0).mean() * 100
            insights.append(
                f"- 최종 점수 15점 초과 거래의 승률: **{high_score_win_rate:.2f}%** (전체 승률: {win_rate:.2f}%)"
            )

        aligned_trades = df[
            (
                (df["final_score"] > 0) & (df["score_1d"] > 0)
            )
            | ((df["final_score"] < 0) & (df["score_1d"] < 0))
        ]
        if not aligned_trades.empty:
            aligned_win_rate = (aligned_trades["pnl"] > 0).mean() * 100
            insights.append(
                f"- 일봉 추세에 순응한 거래의 승률: **{aligned_win_rate:.2f}%**"
            )

        avg_profit_loss_ratio = (
            f"{avg_profit / avg_loss:.2f}" if avg_loss and not pd.isna(avg_loss) else "N/A"
        )

        return {
            "total_trades": total_trades,
            "win_rate": f"{win_rate:.2f}%",
            "profit_factor": f"{profit_factor:.2f}",
            "avg_profit_loss_ratio": avg_profit_loss_ratio,
            "insights": insights,
        }
