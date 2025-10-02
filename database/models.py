from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Signal(Base):
    __tablename__ = "signals"
    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    final_score = Column(Float, nullable=False)
    score_1d = Column(Float)
    score_4h = Column(Float)
    score_1h = Column(Float)
    score_15m = Column(Float)
    atr_1d = Column(Float)
    atr_4h = Column(Float)
    adx_4h = Column(Float)
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
    entry_atr = Column(Float)
    stop_loss_price = Column(Float)
    take_profit_price = Column(Float)
    highest_price_since_entry = Column(Float)
    exit_price = Column(Float)
    pnl = Column(Float)
    entry_time = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    exit_time = Column(DateTime)
    status = Column(String, default="OPEN")
    signal = relationship("Signal", back_populates="trade")
