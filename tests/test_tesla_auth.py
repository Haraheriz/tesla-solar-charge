"""トークンファイルの形式と、更新ロック。本体と観測ツールが同じ関数を使う。"""
import json
import threading
import time

import tesla_auth


def test_保存形式は期限を1800秒早める(tmp_path):
    path = tmp_path / "tesla_tokens.json"
    before = time.time()
    expires_at = tesla_auth.save_token_file(str(path), "access-1", "refresh-1", 28800)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert set(saved) == {"access_token", "refresh_token", "token_expires_at"}
    assert saved["access_token"] == "access-1"
    assert saved["refresh_token"] == "refresh-1"
    assert saved["token_expires_at"] == expires_at
    assert before + 28800 - 1800 - 2 <= expires_at <= time.time() + 28800 - 1800


def test_観測側と同じ読み書きでキーが一致する(tmp_path):
    path = str(tmp_path / "tesla_tokens.json")
    tesla_auth.save_token_file(path, "access-2", "refresh-2", 1000)
    loaded = tesla_auth.load_token_file(path)
    assert loaded["access_token"] == "access-2"
    assert loaded["refresh_token"] == "refresh-2"
    assert abs(loaded["token_expires_at"] - (time.time() + 1000 - 1800)) < 2


def test_ロックを取れないときはNoneを返す(tmp_path):
    path = str(tmp_path / "tesla_tokens.json")
    tesla_auth.save_token_file(path, "access", "refresh", 100)
    held = tesla_auth.try_lock(path, blocking=True)
    assert held is not None
    try:
        assert tesla_auth.try_lock(path, blocking=False) is None
        # 取れなくてもファイルは読める。
        assert tesla_auth.load_token_file(path)["access_token"] == "access"
    finally:
        tesla_auth.unlock(held)


def test_ロックを持っているあいだ別スレッドは待たされる(tmp_path):
    path = str(tmp_path / "tesla_tokens.json")
    tesla_auth.save_token_file(path, "old", "refresh", 10)
    started = threading.Event()
    release = threading.Event()
    saw = {}

    def _hold():
        fd = tesla_auth.try_lock(path, blocking=True)
        started.set()
        release.wait(timeout=2)
        tesla_auth.save_token_file(path, "new", "refresh", 10)
        tesla_auth.unlock(fd)

    def _reader():
        started.wait(timeout=2)
        fd = tesla_auth.try_lock(path, blocking=False)
        saw["locked"] = fd is None
        if fd is None:
            saw["access"] = tesla_auth.load_token_file(path)["access_token"]
        else:
            tesla_auth.unlock(fd)

    holder = threading.Thread(target=_hold)
    reader = threading.Thread(target=_reader)
    holder.start()
    reader.start()
    reader.join()
    release.set()
    holder.join()
    assert saw["locked"] is True
    assert saw["access"] == "old"


def test_瞬時電力のepc231を読む():
    appliances = [{
        "type": "EL_SMART_METER",
        "smart_meter": {"echonetlite_properties": [
            {"epc": 231, "val": "-1200", "updated_at": "2026-08-09T00:00:00Z"},
        ]},
    }]
    value, updated = tesla_auth.epc_value(
        appliances[0]["smart_meter"]["echonetlite_properties"], 231
    )
    assert value == "-1200"
    assert updated == "2026-08-09T00:00:00Z"
