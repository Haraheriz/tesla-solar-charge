import os
import json
import time
from typing import Any, Dict, Tuple

# tesla_solar_charger.py（充電制御ループ）と control_server.py（スマホ操作用サーバー）の
# 両プロセスがこのファイルを介して状態を共有する。現在2つ持っている。
#
#   manual_override … フル充電モード（太陽光の発電状況を無視して充電する）
#   away_probe      … 外出先の充電記録（ケーブル未接続でも車両データを読み続ける）
#
# どちらも「利用者が意図して入れ、しばらく続く状態」であり、設定ファイルではなくここに置く。
# tesla_config.json は起動時に1回しか読まないため、変更に再起動が要る。こちらは制御ループが
# 毎サイクル読み直すので、スマホからの切替が次のサイクルで反映される。
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
OVERRIDE_FILE: str = os.environ.get("TESLA_OVERRIDE_PATH", os.path.join(BASE_DIR, "override_state.json"))


def _read_all() -> Dict[str, Any]:
    """状態ファイル全体を辞書で返す。無い・壊れている場合は空の辞書を返す。"""
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: Dict[str, Any]) -> None:
    """状態ファイルを原子的に置き換える。

    一時ファイルへ書いてから os.replace で差し替えるため、読み手が中途半端な内容を
    見ることはない。0o600 は、フル充電モードの操作状態を他ユーザーへ晒さないためである。
    """
    tmp_file: str = OVERRIDE_FILE + ".tmp"
    fd = os.open(tmp_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, OVERRIDE_FILE)


def _read_flag(key: str, updated_key: str) -> Tuple[bool, float]:
    data = _read_all()
    enabled = bool(data.get(key, False))
    try:
        updated_at = float(data.get(updated_key, 0.0))
    except (TypeError, ValueError):
        updated_at = 0.0
    return enabled, updated_at


def _write_flag(key: str, updated_key: str, enabled: bool) -> None:
    """1つのフラグだけを書き換える。他のキーは読み直して保存する。

    全体を上書きすると、同じファイルに同居しているもう一方のフラグが消える。
    書き手は control_server.py（利用者のタップ）と tesla_solar_charger.py
    （手動停止を検知したときのフル充電モード自動解除）の2プロセスある。
    os.replace が原子的なのでファイルが壊れることはないが、同時刻に書けば後勝ちで
    片方の変更が失われる。人間のタップと10分周期の自動解除が同一の瞬間に当たる必要があり、
    失われるのは記録設定であって制御判断ではないため、ロックは設けていない。
    """
    data = _read_all()
    data[key] = enabled
    data[updated_key] = time.time()
    _write_all(data)


def read_override() -> bool:
    return read_override_state()[0]


def read_override_state() -> Tuple[bool, float]:
    """フル充電モードの有効・無効と、最後に切替えられたUNIX時刻を返す。

    充電制御ループ側は updated_at を使って「フル充電モードが何時間続いているか」を
    毎サイクル可視化する。ファイルが無い・壊れている場合は (False, 0.0) を返す。
    """
    return _read_flag("manual_override", "updated_at")


def write_override(enabled: bool) -> None:
    _write_flag("manual_override", "updated_at", enabled)


def read_away_probe() -> bool:
    return read_away_probe_state()[0]


def read_away_probe_state() -> Tuple[bool, float]:
    """外出先の充電記録の有効・無効と、最後に切替えられたUNIX時刻を返す。

    既定は無効である。自宅の充電器にケーブルが繋がっていないとき、車両データを読んでも
    電流調整・停止・開始のどの判断も変わらないため、通常は読まない。有効にすると、
    外出先での充電を記録するために一定間隔で読み直すようになる（課金対象）。
    """
    return _read_flag("away_probe", "away_probe_updated_at")


def write_away_probe(enabled: bool) -> None:
    _write_flag("away_probe", "away_probe_updated_at", enabled)
