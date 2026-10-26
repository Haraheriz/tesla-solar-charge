"""設定ファイル・トークンファイルの権限検査（config_loader.warn_if_accessible_by_others）。

検査は知らせるだけで起動を止めない。ここで見るのは「どの権限で知らせ、どの権限で黙るか」である。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import warn_if_accessible_by_others  # noqa: E402

posix_only = pytest.mark.skipif(os.name != "posix", reason="権限ビットは POSIX でのみ意味を持つ")


def _file_with_mode(tmp_path, mode):
    path = tmp_path / "tesla_config.json"
    path.write_text("{}", encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


@posix_only
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o666, 0o610, 0o601])
def test_所有者以外にビットが立っていれば知らせる(tmp_path, mode):
    messages = []
    path = _file_with_mode(tmp_path, mode)
    assert warn_if_accessible_by_others(path, messages.append) is True
    assert len(messages) == 1
    assert path in messages[0]
    assert "chmod 600" in messages[0]


@posix_only
@pytest.mark.parametrize("mode", [0o600, 0o400, 0o200, 0o700])
def test_所有者だけの権限なら黙る(tmp_path, mode):
    messages = []
    assert warn_if_accessible_by_others(_file_with_mode(tmp_path, mode), messages.append) is False
    assert messages == []


def test_ファイルが無ければ黙る(tmp_path):
    messages = []
    assert warn_if_accessible_by_others(str(tmp_path / "missing.json"), messages.append) is False
    assert messages == []
