# 파일명: tests/test_position_sizer.py (수정 완료)

import pytest
from unittest.mock import MagicMock

import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from risk_management.position_sizer import PositionSizer
from core.config_manager import config as actual_config # 실제 config를 임포트

# --- 테스트 준비 (가짜 객체 생성) ---

@pytest.fixture
def mock_binance_client():
    """가짜 바이낸스 클라이언트 객체를 생성하고, 필요한 반환값을 설정합니다."""
    client = MagicMock()
    client.futures_account_balance.return_value = [{'asset': 'USDT', 'balance': '10000'}]

    # ▼▼▼ [수정] futures_exchange_info에 대한 응답을 설정합니다. ▼▼▼
    exchange_info = {
        'symbols': [{
            'symbol': 'BTCUSDT',
            'quantityPrecision': 2 # BTCUSDT의 수량 정밀도는 소수점 2자리라고 가정
        }]
    }
    client.futures_exchange_info.return_value = exchange_info
    # ▲▲▲ [수정] ▲▲▲

    return client

@pytest.fixture
def mock_config():
    """테스트에 사용할 가짜 설정 객체를 생성합니다."""
    # 실제 ConfigManager를 사용하되, 테스트에 필요한 값만 덮어씁니다.
    # 이렇게 하면 실제 .env 값과 유사한 환경에서 테스트할 수 있습니다.
    actual_config.max_open_positions = 2
    actual_config.risk_target_pct = 0.01
    actual_config.sl_atr_multiplier = 2.0
    actual_config.risk_scale_high = 1.5
    actual_config.risk_scale_medium = 1.0
    actual_config.risk_scale_low = 0.5
    return actual_config

# --- 실제 테스트 코드 ---

def test_calculate_position_size_normal_case(mock_binance_client, mock_config):
    """가장 기본적인 상황에서의 포지션 크기 계산을 테스트합니다."""
    # PositionSizer가 전역 config를 직접 참조하므로, mock_config를 따로 주입할 필요가 없습니다.
    sizer = PositionSizer(mock_binance_client)

    quantity = sizer.calculate_position_size(
        symbol="BTCUSDT", atr=100, aggr_level=5,
        open_positions_count=0, average_score=15.0
    )
    # 예상 결과: 0.25 -> 정밀도 2에 따라 0.25로 반올림
    assert quantity == pytest.approx(0.25)

def test_high_risk_high_score_case(mock_binance_client, mock_config):
    """고위험, 고신뢰도 신호 상황에서의 포지션 크기 계산을 테스트합니다."""
    sizer = PositionSizer(mock_binance_client)
    quantity = sizer.calculate_position_size(
        symbol="BTCUSDT", atr=100, aggr_level=8,
        open_positions_count=0, average_score=18.0
    )
    # 예상 결과: 0.4875 -> 정밀도 2에 따라 0.49로 반올림
    assert quantity == pytest.approx(0.49)

def test_no_available_slot(mock_binance_client, mock_config):
    """포지션 슬롯이 꽉 찼을 때 None을 반환하는지 테스트합니다."""
    sizer = PositionSizer(mock_binance_client)
    quantity = sizer.calculate_position_size(
        symbol="BTCUSDT", atr=100, aggr_level=5,
        open_positions_count=2, average_score=15.0
    )
    assert quantity is None