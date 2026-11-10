"""操作状態ファイルの同時更新。別キーの書き込みが後勝ちで消えないこと。"""
import threading

import override_state


def test_同時の別キー更新が両方残る(tmp_path, monkeypatch):
    path = tmp_path / "override_state.json"
    monkeypatch.setattr(override_state, "OVERRIDE_FILE", str(path))
    barrier = threading.Barrier(2)
    original = override_state._read_all

    def _read_and_wait():
        data = original()
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return data

    monkeypatch.setattr(override_state, "_read_all", _read_and_wait)

    def _override():
        override_state.write_override(True)

    def _probe():
        override_state.write_away_probe(True)

    first = threading.Thread(target=_override)
    second = threading.Thread(target=_probe)
    first.start()
    second.start()
    first.join()
    second.join()

    monkeypatch.setattr(override_state, "_read_all", original)
    saved = original()
    assert saved.get("manual_override") is True
    assert saved.get("away_probe") is True


def test_1回の読みが3つの値を返す(tmp_path, monkeypatch):
    path = tmp_path / "override_state.json"
    monkeypatch.setattr(override_state, "OVERRIDE_FILE", str(path))
    override_state.write_override(True)
    override_state.write_charge_target(30)
    snap = override_state.read_snapshot()
    assert snap["manual_override"] is True
    assert snap["charge_target_soc"] == 30
    assert snap["away_probe"] is False
    assert snap["charge_target_invalid"] is False
