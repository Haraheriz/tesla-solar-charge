"""起動時のトークン更新。ポート8000で待たないことと、ファイルを消さないことを見る。"""
import json
import os

import pytest

from conftest import CapturingHandler, FakeTime, StopSim, _load_module


class _Stdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def _no_tty(monkeypatch, module):
    monkeypatch.delenv("TESLA_INTERACTIVE_AUTH", raising=False)
    monkeypatch.setattr(module.sys, "stdin", _Stdin(False))


def _refuse_server(*args, **kwargs):
    raise AssertionError("初回認証の待受を開始した")


def test_トークンが無く端末が無ければ待たずに終了する(tmp_path, monkeypatch):
    module = _load_module(tmp_path)
    _no_tty(monkeypatch, module)
    os.remove(module.TOKEN_FILE)
    capture = CapturingHandler()
    module.logger.addHandler(capture)
    monkeypatch.setattr(module, "HTTPServer", _refuse_server)

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    assert not os.path.exists(module.TOKEN_FILE)
    assert any("起動を中止します" in message for _, message in capture.records)


def test_起動時の通信失敗ではトークンファイルを残して待つ(tmp_path, monkeypatch):
    module = _load_module(tmp_path)
    _no_tty(monkeypatch, module)
    before = json.loads(open(module.TOKEN_FILE, encoding="utf-8").read())
    module.time = FakeTime(1_700_000_000, budget_sec=10)

    def _fail():
        module.token_refresh_failures += 1
        module.token_refresh_rejected = False
        return False

    module.refresh_tesla_token = _fail
    monkeypatch.setattr(module, "HTTPServer", _refuse_server)

    with pytest.raises(StopSim):
        module.main()

    assert json.loads(open(module.TOKEN_FILE, encoding="utf-8").read())["refresh_token"] == before["refresh_token"]
    assert 180 in module.time.slept


def test_起動時の拒否では終了しトークンファイルを残す(tmp_path, monkeypatch):
    module = _load_module(tmp_path)
    _no_tty(monkeypatch, module)
    module.time = FakeTime(1_700_000_000, budget_sec=10_000)

    def _reject():
        module.token_refresh_rejected = True
        return False

    module.refresh_tesla_token = _reject
    monkeypatch.setattr(module, "HTTPServer", _refuse_server)

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    assert os.path.exists(module.TOKEN_FILE)
    assert module.time.slept == []


def test_更新の400は拒否として記録する(tmp_path):
    module = _load_module(tmp_path)
    module.refresh_token = "dummy-refresh"

    class _Response:
        status_code = 400

        def raise_for_status(self):
            error = Exception("HTTP 400")
            error.response = self
            raise error

    module.cloud_session = type("S", (), {"post": staticmethod(lambda *args, **kwargs: _Response())})()
    assert module.refresh_tesla_token() is False
    assert module.token_refresh_rejected is True


def test_更新の通信失敗は拒否にしない(tmp_path):
    module = _load_module(tmp_path)
    module.refresh_token = "dummy-refresh"

    def _post(*args, **kwargs):
        raise TimeoutError("timed out")

    module.cloud_session = type("S", (), {"post": staticmethod(_post)})()
    assert module.refresh_tesla_token() is False
    assert module.token_refresh_rejected is False
