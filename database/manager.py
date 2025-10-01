import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base
from core.config_manager import config


class DatabaseManager:
    """Handle engine creation and session management for the database."""

    def __init__(self) -> None:
        db_dir = os.path.dirname(os.path.abspath(config.db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        self.engine = create_engine(f"sqlite:///{config.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        print(f"데이터베이스 매니저가 초기화되었습니다. (경로: {config.db_path})")

    def get_session(self):
        return self.Session()


# 단일 데이터베이스 매니저 객체
db_manager = DatabaseManager()
