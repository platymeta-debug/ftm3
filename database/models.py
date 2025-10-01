from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Signal(Base):
    """거래를 유발한 분석 컨텍스트를 저장하는 테이블 모델"""

    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    final_score = Column(Float, nullable=False)
    score_1d = Column(Float)
    score_4h = Column(Float)
    score_1h = Column(Float)
    score_15m = Column(Float)
    trade = relationship("Trade", back_populates="signal", uselist=False)


class Trade(Base):
    """실제 거래 내역을 저장하는 테이블 모델"""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id"))
    binance_order_id = Column(String)
    symbol = Column(String)
    side = Column(String)
    quantity = Column(Float)
    entry_price = Column(Float)
    exit_price = Column(Float)
    pnl = Column(Float)
    entry_time = Column(DateTime, default=datetime.utcnow)
    exit_time = Column(DateTime)
    status = Column(String, default="OPEN")
    signal = relationship("Signal", back_populates="trade")
