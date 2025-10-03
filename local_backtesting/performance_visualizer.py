# 파일명: local_backtesting/performance_visualizer.py (표시 방식 개선 최종본)
import matplotlib
matplotlib.use('Agg')
import pandas as pd
import matplotlib.pyplot as plt
import io
import numpy as np # numpy 임포트

def create_performance_report(stats: pd.Series, start_cash: float) -> tuple[str, io.BytesIO]:
    """
    backtesting.py의 stats 객체와 시작 자본금을 받아
    요약 텍스트와 수익 곡선 차트 이미지를 생성합니다.
    """
    # ▼▼▼ [수정] nan 값 처리를 위한 헬퍼 함수 ▼▼▼
    def format_stat(value, is_percent=False, is_currency=False):
        if pd.isna(value) or np.isnan(value):
            return "N/A"
        if is_currency:
            return f"${value:,.2f}"
        if is_percent:
            return f"{value:.2f}%"
        return value
    # ▲▲▲ [수정] ▲▲▲

    # 1. 핵심 성과 지표(KPI) 추출 및 포맷팅
    kpis = {
        "총 기간": stats.get('Duration'),
        "시작 자본금": format_stat(start_cash, is_currency=True), # 시작 자본금 추가
        "최종 자산": format_stat(stats.get('Equity Final [$]', 0), is_currency=True),
        "최대 자산": format_stat(stats.get('Equity Peak [$]', 0), is_currency=True),
        "수익률 (Return)": format_stat(stats.get('Return [%]', 0), is_percent=True),
        "최대 낙폭 (Max. Drawdown)": format_stat(stats.get('Max. Drawdown [%]', 0), is_percent=True),
        "승률 (Win Rate)": format_stat(stats.get('Win Rate [%]'), is_percent=True), # nan 처리
        "총 거래 횟수": f"{stats.get('No. Trades', 0)}회",
        "평균 거래 수익률": format_stat(stats.get('Avg. Trade [%]'), is_percent=True), # nan 처리
        "샤프 비율 (Sharpe Ratio)": f"{stats.get('Sharpe Ratio', 0):.2f}" if not pd.isna(stats.get('Sharpe Ratio')) else "N/A"
    }

    report_text = "📊 **백테스팅 성과 요약** 📊\n" + "-"*30 + "\n"
    for key, value in kpis.items():
        report_text += f"**{key}**: {value}\n"

    # ... (이하 차트 생성 로직은 동일) ...
    equity_curve = stats.get('_equity_curve')
    if equity_curve is None:
        return report_text, None

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(12, 7))

    equity_curve['Equity'].plot(ax=ax, color='cyan', linewidth=2, label='Equity Curve')
    equity_curve['DrawdownPct'].plot(ax=ax, kind='area', color='red', alpha=0.3, secondary_y=True, label='Drawdown %')

    ax.set_title('Equity Curve & Drawdown', fontsize=16)
    ax.set_ylabel('Equity ($)', color='cyan')
    ax.right_ax.set_ylabel('Drawdown (%)', color='red')
    ax.legend(loc='upper left')
    ax.right_ax.legend(loc='lower left')
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    buf.seek(0)
    plt.close(fig)

    return report_text, buf
