from datetime import datetime
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
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
    highest_price_since_entry = Column(Float)
    exit_price = Column(Float)
    pnl = Column(Float)
    entry_time = Column(DateTime, default=datetime.utcnow)
    exit_time = Column(DateTime)
    status = Column(String, default="OPEN")
    # 진입 시점의 ATR 값을 저장하여 기술적 손절 라인 계산에 사용
    entry_atr = Column(Float)
    signal = relationship("Signal", back_populates="trade")
