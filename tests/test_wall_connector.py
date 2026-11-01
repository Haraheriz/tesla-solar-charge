"""wall_connector の、同じサイクルで読んだ vitals の保持と読み直しを直接確かめる。

制御ループを通したテスト（test_charge_control.py）では、保持が効いていることは
問い合わせ回数で確かめている。読み直し（fresh=True）が保持した値を置き換えることは、
ループを通すと目標到達後の待機（T14）でしか現れないため、ここで単独に確かめる。
"""
import pytest

import wall_connector


class _Response:
    def __init__(self, body):
        self.status_code = 200
        self._body = body

    def json(self):
        return self._body


class _VitalsSession:
    """呼ばれるたびに、用意した vitals を順に返す。"""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    def get(self, url, timeout=None):
        assert url.endswith("/api/1/vitals")
        body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        return _Response(body)


@pytest.fixture
def session(monkeypatch):
    fake = _VitalsSession([
        {"vehicle_connected": True, "contactor_closed": False, "vehicle_current_a": 0.0},
        {"vehicle_connected": True, "contactor_closed": True, "vehicle_current_a": 16.2},
    ])
    monkeypatch.setattr(wall_connector, "wc_session", fake)
    wall_connector.begin_cycle()
    yield fake
    wall_connector.begin_cycle()


def test_同じサイクルでは1回だけ読み結果を使い回す(session):
    assert wall_connector.read_vehicle_connected("192.0.2.1") == wall_connector.WC_CONNECTED
    assert wall_connector.read_delivering("192.0.2.1") == wall_connector.WC_NOT_DELIVERING
    assert wall_connector.read_vehicle_current("192.0.2.1") == 0.0
    assert session.calls == 1


def test_読み直しは保持した値を置き換える(session):
    assert wall_connector.read_delivering("192.0.2.1") == wall_connector.WC_NOT_DELIVERING
    assert wall_connector.read_delivering("192.0.2.1", fresh=True) == wall_connector.WC_DELIVERING
    # 読み直した後は、同じサイクルの他の判定も新しい値を使う
    assert wall_connector.read_vehicle_current("192.0.2.1") == 16.2
    assert session.calls == 2


def test_サイクルの始まりで保持した値を捨てる(session):
    assert wall_connector.read_delivering("192.0.2.1") == wall_connector.WC_NOT_DELIVERING
    wall_connector.begin_cycle()
    assert wall_connector.read_delivering("192.0.2.1") == wall_connector.WC_DELIVERING
    assert session.calls == 2
