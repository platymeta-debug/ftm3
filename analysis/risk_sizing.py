# analysis/risk_sizing.py
from math import floor

def _round_down_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return floor(x / step) * step

def calc_order_qty(
    price: float,
    atr: float,
    sl_atr_mult: float,
    equity: float,
    *,
    risk_per_trade: float = 0.01,        # 자본의 1%
    max_exposure_frac: float = 0.30,     # 총노출 = 자본의 30% (레버리지 전 기준)
    margin: float = 1/10,                # backtesting의 margin = 1/레버리지
    min_notional: float = 5.0,           # 거래소 최소 주문가치(USDT)
    qty_step: float = 1e-6,              # 수량 스텝
    min_qty: float = 1e-6,               # 최소 수량
) -> float:
    """
    SL 가격거리와 증거금/거래소 제약을 모두 고려한 '절대 수량'을 돌려준다.
    반환값이 0이면 진입을 스킵해야 한다.
    """
    if price <= 0 or atr <= 0 or sl_atr_mult <= 0 or equity <= 0:
        return 0.0

    # 1) 손절 거리(달러)
    sl_dist = atr * sl_atr_mult

    # 2) 거래당 리스크 한도(달러)
    risk_cap = max(1e-9, equity * float(risk_per_trade))

    # 3) 리스크 기준 수량
    qty_risk = risk_cap / sl_dist

    # 4) 총 노출 상한(레버리지 고려)
    max_notional = (equity / max(margin, 1e-9)) * float(max_exposure_frac)
    qty_expo = max_notional / price

    qty = max(0.0, min(qty_risk, qty_expo))

    # 5) 거래소 제약 적용
    qty = _round_down_to_step(qty, qty_step)
    if qty < min_qty:
        return 0.0
    if qty * price < min_notional:
        need = min_notional / price
        qty = _round_down_to_step(max(qty, need), qty_step)
        if qty * price < min_notional:
            return 0.0
    return qty
