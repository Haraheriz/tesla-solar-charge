import os
import json
from typing import Any, Dict

# 制御ループが最後に読んだ車両の状態と、車両側の充電上限を元に戻すための値を保存する。
# 書き手は tesla_solar_charger.py だけで、control_server.py は読むだけである。
#
# override_state.json と分けているのは、書き手を1プロセスに限るためである。
# override_state.json には2プロセスが書き込み、同時刻に書けば後から書いた方の内容が残る。
# ここに置く charge_limit_restore_soc（車両側の充電上限の元の値）が失われると、
# 車両の設定を戻せなくなる（docs/05_charge_target_design.md 第5.2節）。
#
# キー：
#   battery_level / charging_state / charge_limit_soc / charge_limit_soc_min … 最後に読んだ値
#   observed_at                … 上記を読んだUNIX時刻
#   charge_limit_applied_soc   … 本システムが最後に設定した車両側の充電上限
#   charge_limit_restore_soc   … 本システムが変更する前の車両側の充電上限
#   target_reached_at          … 目標充電率に達して停止したUNIX時刻（画面表示用）
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
VEHICLE_STATUS_FILE: str = os.environ.get(
    "TESLA_VEHICLE_STATUS_PATH", os.path.join(BASE_DIR, "vehicle_status.json")
)


def load_vehicle_status() -> Dict[str, Any]:
    """保存した状態を返す。無い・壊れている場合は空の辞書を返す。"""
    try:
        with open(VEHICLE_STATUS_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_vehicle_status(data: Dict[str, Any]) -> None:
    """状態を原子的に置き換える（一時ファイルへ書いてから os.replace で差し替える）。"""
    tmp_file: str = VEHICLE_STATUS_FILE + ".tmp"
    fd = os.open(tmp_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_file, VEHICLE_STATUS_FILE)
