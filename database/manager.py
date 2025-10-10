# database/manager.py (수정)
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base
# from core.config_manager import config # <- 이 줄을 삭제합니다.

class DatabaseManager:
    """Handle engine creation and session management for the database."""

    # --- ▼▼▼ [핵심 수정] __init__에서 config 객체를 직접 받도록 변경 ▼▼▼ ---
    def __init__(self, config) -> None:
        self.config = config
        db_dir = os.path.dirname(os.path.abspath(self.config.db_path))
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        self.engine = create_engine(f"sqlite:///{self.config.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        print(f"데이터베이스 매니저가 초기화되었습니다. (경로: {self.config.db_path})")
    # --- ▲▲▲ [핵심 수정] ---

    def get_session(self):
        return self.Session()

# --- ▼▼▼ [핵심 수정] 파일 하단의 단일 객체 생성을 삭제합니다. ▼▼▼ ---
# db_manager = DatabaseManager() # <- 이 줄을 삭제하거나 주석 처리합니다.
