from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Signal(Base):
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    final_score = Column(Float, nullable=False)
    score_1d = Column(Float)
    score_4h = Column(Float)
    score_1h = Column(Float)
    score_15m = Column(Float)
    # 1일봉 ATR 값을 저장하여 변동성 분석에 사용
    atr_1d = Column(Float)
    # 4시간봉 ADX 값을 저장하여 추세/횡보 판단에 사용
    adx_4h = Column(Float)
    # 1일봉 200 이평선 위에 있는지 여부 (장기 추세 판단)
    is_above_ema200_1d = Column(Boolean)
    trade = relationship("Trade", back_populates="signal", uselist=False)

class Trade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"))
    binance_order_id = Column(Integer, unique=True)
    symbol = Column(String)
    side = Column(String)
    quantity = Column(Float)
    entry_price = Column(Float)
    
    # --- ▼▼▼ V4 수정 사항 ▼▼▼ ---
    # 진입 시점의 ATR 값을 저장하여 기술적 손절 라인 계산에 사용
    entry_atr = Column(Float)
    # 진입 시점에 계산된 고정 손절/익절 가격 (브라켓 주문)
    stop_loss_price = Column(Float)
    take_profit_price = Column(Float)
    # --- ▲▲▲ V4 수정 사항 ▲▲▲ ---

    highest_price_since_entry = Column(Float)
    exit_price = Column(Float)
    pnl = Column(Float)
    entry_time = Column(DateTime, default=datetime.utcnow)
    exit_time = Column(DateTime)
    status = Column(String, default="OPEN")
    signal = relationship("Signal", back_populates="trade")
