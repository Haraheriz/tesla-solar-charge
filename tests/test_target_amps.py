"""目標電流の式と、送信前の範囲。式の切り捨ては変えない。"""
from conftest import _load_module


def test_充電中の買電は現在値から減らす(tmp_path):
    module = _load_module(tmp_path)
    # 15 A で充電中、買電 1000 W → 5 A 減らして 10 A（docs/01_architecture.md の例）
    assert module.compute_target_amps(15, 1000, True) == 10


def test_停止中の売電は0から増やす(tmp_path):
    module = _load_module(tmp_path)
    assert module.compute_target_amps(15, -1000, False) == 5


def test_換算の端数は0へ向かって切り捨てる(tmp_path):
    module = _load_module(tmp_path)
    # 買電 399 W は -1.995 A。int は -1。
    assert module.compute_target_amps(0, 399, False) == -1
    # 売電 399 W は 1.995 A。int は 1。
    assert module.compute_target_amps(0, -399, False) == 1


def test_範囲内の値は型も含めて変えない(tmp_path):
    module = _load_module(tmp_path)
    assert module.clamp_amps(16, 4, 48) == 16
    assert module.clamp_amps(16.0, 4, 48) == 16.0


def test_範囲外は上下限に切り詰める(tmp_path):
    module = _load_module(tmp_path)
    assert module.clamp_amps(100, module.MIN_AMPS, module.MAX_AMPS) == module.MAX_AMPS
    assert module.clamp_amps(0, module.MIN_AMPS, module.MAX_AMPS) == module.MIN_AMPS


def test_範囲外の電流設定は切り詰めて送る(tmp_path):
    module = _load_module(tmp_path)
    sent = []

    class _Response:
        status_code = 200

        def json(self):
            return {"response": {"result": True}}

    class _Session:
        def post(self, url, headers=None, json=None, timeout=None):
            sent.append(json["charging_amps"])
            return _Response()

    module.proxy_session = _Session()
    assert module.set_charging_amps("VIN", {}, 100) is True
    assert sent == [module.MAX_AMPS]
    assert module.set_charging_amps("VIN", {}, 0) is True
    # 下限へ切り詰めた値は 5 A 未満なので、1試行につき 2 回送る。
    assert sent[1:] == [module.MIN_AMPS, module.MIN_AMPS]
