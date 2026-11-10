"""日中の vehicle_data 失敗で待つ秒。夜間の再試行回数は変えていない。"""
from conftest import _load_module


def test_日中の失敗ごとの待機秒(tmp_path):
    module = _load_module(tmp_path)
    assert module.day_vehicle_data_wait_sec(401, False) == 180
    assert module.day_vehicle_data_wait_sec(429, True) == 3600
    assert module.day_vehicle_data_wait_sec(408, True) == 180
    assert module.day_vehicle_data_wait_sec(408, False) == 600
    assert module.day_vehicle_data_wait_sec(500, False) == 600
