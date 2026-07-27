import os
import json
import time
from typing import Any, Dict, Tuple

# tesla_solar_charger.py（充電制御ループ）と control_server.py（スマホ操作用サーバー）の
# 両プロセスがこのファイルを介して「マニュアル・オーバーライド」状態を共有する。
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
OVERRIDE_FILE: str = os.environ.get("TESLA_OVERRIDE_PATH", os.path.join(BASE_DIR, "override_state.json"))


def read_override() -> bool:
    return read_override_state()[0]


def read_override_state() -> Tuple[bool, float]:
    """オーバーライドの有効・無効と、最後に切替えられたUNIX時刻を返す。

    充電制御ループ側は updated_at を使って「フル充電モードが何時間続いているか」を
    毎サイクル可視化する。ファイルが無い・壊れている場合は (False, 0.0) を返す。
    """
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
            enabled = bool(data.get("manual_override", False))
            try:
                updated_at = float(data.get("updated_at", 0.0))
            except (TypeError, ValueError):
                updated_at = 0.0
            return enabled, updated_at
    except Exception:
        return False, 0.0


def write_override(enabled: bool) -> None:
    data: Dict[str, Any] = {"manual_override": enabled, "updated_at": time.time()}
    tmp_file: str = OVERRIDE_FILE + ".tmp"
    fd = os.open(tmp_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, OVERRIDE_FILE)
