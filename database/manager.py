import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

# Remove the top-level import to avoid a circular dependency
# from core.config_manager import config

class DatabaseManager:
    """Handle engine creation and session management for the database."""

    def __init__(self, config) -> None:
        db_dir = os.path.dirname(os.path.abspath(config.db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        self.engine = create_engine(f"sqlite:///{config.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        print(f"데이터베이스 매니저가 초기화되었습니다. (경로: {config.db_path})")

    def get_session(self):
        return self.Session()

# 1. db_manager 객체를 전역 변수로 즉시 생성하지 않습니다.
# 2. 대신, 이 함수를 호출하여 필요한 시점에 인스턴스를 얻습니다.
db_manager = None
def get_db_manager(config_instance):
    global db_manager
    if db_manager is None:
        db_manager = DatabaseManager(config_instance)
    return db_manager
