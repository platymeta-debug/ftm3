# 파일명: backtesting/performance_visualizer.py
import matplotlib
matplotlib.use('Agg')
import pandas as pd
import matplotlib.pyplot as plt
import io

def create_performance_report(stats: pd.Series) -> tuple[str, io.BytesIO]:
    """
    backtesting.py의 stats 객체를 받아 요약 텍스트와
    수익 곡선 차트 이미지를 생성합니다.

    :return: (요약 텍스트, 차트 이미지 BytesIO 객체)
    """
    # 1. 핵심 성과 지표(KPI) 추출 및 포맷팅
    kpis = {
        "총 기간": stats.get('Duration'),
        "최종 자산 (Equity Final)": f"${stats.get('Equity Final [$]', 0):,.2f}",
        "최대 자산 (Equity Peak)": f"${stats.get('Equity Peak [$]', 0):,.2f}",
        "수익률 (Return)": f"{stats.get('Return [%]', 0):.2f}%",
        "최대 낙폭 (Max. Drawdown)": f"{stats.get('Max. Drawdown [%]', 0):.2f}%",
        "승률 (Win Rate)": f"{stats.get('Win Rate [%]', 0):.2f}%",
        "총 거래 횟수": f"{stats.get('No. Trades', 0)}회",
        "평균 거래 수익률": f"{stats.get('Avg. Trade [%]', 0):.2f}%",
        "샤프 비율 (Sharpe Ratio)": f"{stats.get('Sharpe Ratio', 0):.2f}"
    }

    report_text = "📊 **백테스팅 성과 요약** 📊\n" + "-"*30 + "\n"
    for key, value in kpis.items():
        report_text += f"**{key}**: {value}\n"

    # 2. 수익 곡선(Equity Curve) 차트 생성
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

    # 3. 차트를 이미지 파일(BytesIO)로 변환
    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    buf.seek(0)
    plt.close(fig)

    return report_text, buf
