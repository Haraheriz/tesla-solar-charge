import os
import json
import time
from typing import Any, Dict

# tesla_solar_charger.py（充電制御ループ）と control_server.py（スマホ操作用サーバー）の
# 両プロセスがこのファイルを介して「マニュアル・オーバーライド」状態を共有する。
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
OVERRIDE_FILE: str = os.environ.get("TESLA_OVERRIDE_PATH", os.path.join(BASE_DIR, "override_state.json"))


def read_override() -> bool:
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as f:
            data: Dict[str, Any] = json.load(f)
            return bool(data.get("manual_override", False))
    except Exception:
        return False


def write_override(enabled: bool) -> None:
    data: Dict[str, Any] = {"manual_override": enabled, "updated_at": time.time()}
    tmp_file: str = OVERRIDE_FILE + ".tmp"
    fd = os.open(tmp_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, OVERRIDE_FILE)
