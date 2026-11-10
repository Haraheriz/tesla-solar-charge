import os
import json
import time
from typing import Any, Callable, Dict, Optional, Tuple

try:
    import fcntl
except ImportError:  # Windows の開発機には無い。ロックは POSIX だけで取る。
    fcntl = None  # type: ignore

# tesla_solar_charger.py（充電制御ループ）と control_server.py（スマホ操作用サーバー）の
# 両プロセスがこのファイルを介して状態を共有する。現在3つ持っている。
#
#   manual_override … フル充電モード（太陽光の発電状況を無視して充電する）
#   away_probe      … 外出先の充電記録（ケーブル未接続でも車両データを読み続ける）
#   charge_target_soc … 目標充電率（docs/05_charge_target_design.md）
#
# いずれも「利用者が意図して入れ、しばらく続く状態」であり、設定ファイルではなくここに置く。
# tesla_config.json は起動時に1回しか読まないため、変更に再起動が要る。こちらは制御ループが
# 毎サイクル読み直すので、スマホからの切替が次のサイクルで反映される。
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
OVERRIDE_FILE: str = os.environ.get("TESLA_OVERRIDE_PATH", os.path.join(BASE_DIR, "override_state.json"))


def _read_all() -> Dict[str, Any]:
    """状態ファイル全体を辞書で返す。無い・壊れている場合は空の辞書を返す。"""
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8-sig") as f:
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
    return _flag_from(_read_all(), key, updated_key)


def _update(mutator: Callable[[Dict[str, Any]], None]) -> None:
    """読み込んでから置換するまでを排他する。

    os.replace は原子的だが、2プロセスが同じ内容を読んでから書くと、後の書き込みが
    先の変更を消す。消えるのは外出先の充電記録だけではなく、フル充電モードの解除も
    ありうる。ロックはファイルの読み書きだけに取り、Tesla API を待っているあいだは持たない。
    POSIX 以外ではロックしない（権限検査と同じ）。プロセスが終了すると flock は解放される。
    """
    if fcntl is None or os.name != "posix":
        data = _read_all()
        mutator(data)
        _write_all(data)
        return
    lock_fd = os.open(OVERRIDE_FILE + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _read_all()
        mutator(data)
        _write_all(data)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _write_flag(key: str, updated_key: str, enabled: bool) -> None:
    """1つのフラグだけを書き換える。他のキーは読み直して保存する。"""

    def mutator(data: Dict[str, Any]) -> None:
        data[key] = enabled
        data[updated_key] = time.time()

    _update(mutator)


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


# 目標充電率の値域。1〜100 の整数だけを受け付ける（docs/05_charge_target_design.md 第5.1節）。
CHARGE_TARGET_MIN: int = 1
CHARGE_TARGET_MAX: int = 100


def parse_charge_target(raw: Any) -> Optional[int]:
    """目標充電率として受け付ける値なら int を、未設定（None）なら None を返す。

    それ以外は ValueError を投げる。bool は int の部分型だが、true を 1% と
    解釈させないために拒否する。コントロールサーバーの入口と、状態ファイルを
    読む側の両方がこの関数を通す（検査の基準を1か所にするため）。
    """
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"目標充電率は整数で指定する: {raw!r}")
    if not CHARGE_TARGET_MIN <= raw <= CHARGE_TARGET_MAX:
        raise ValueError(f"目標充電率は {CHARGE_TARGET_MIN}〜{CHARGE_TARGET_MAX} の範囲で指定する: {raw}")
    return raw


def read_charge_target() -> Tuple[Optional[int], bool]:
    """目標充電率と、ファイル上の値が不正だったかを返す。

    不正な値（手で書き換えた場合など）は未設定として扱う。2つめの戻り値が True のとき、
    呼び出し側はそのことをログへ残す。ここで握りつぶすと、利用者は目標が効いていない理由を
    知る手段がない。
    """
    raw = _read_all().get("charge_target_soc")
    try:
        return parse_charge_target(raw), False
    except ValueError:
        return None, True


def read_snapshot() -> Dict[str, Any]:
    """3つの操作状態を、1回の読みから返す。

    キーは manual_override、updated_at、away_probe、away_probe_updated_at、
    charge_target_soc、charge_target_invalid。ファイルが無い・壊れているときは
    フル充電モードと外出先の充電記録は無効、目標充電率は未設定である。
    """
    data = _read_all()
    manual_override, updated_at = _flag_from(data, "manual_override", "updated_at")
    away_probe, away_probe_updated_at = _flag_from(data, "away_probe", "away_probe_updated_at")
    raw = data.get("charge_target_soc")
    try:
        charge_target_soc = parse_charge_target(raw)
        charge_target_invalid = False
    except ValueError:
        charge_target_soc = None
        charge_target_invalid = True
    return {
        "manual_override": manual_override,
        "updated_at": updated_at,
        "away_probe": away_probe,
        "away_probe_updated_at": away_probe_updated_at,
        "charge_target_soc": charge_target_soc,
        "charge_target_invalid": charge_target_invalid,
    }


def _flag_from(data: Dict[str, Any], key: str, updated_key: str) -> Tuple[bool, float]:
    enabled = bool(data.get(key, False))
    try:
        updated_at = float(data.get(updated_key, 0.0))
    except (TypeError, ValueError):
        updated_at = 0.0
    return enabled, updated_at


def write_charge_target(value: Optional[int]) -> None:
    """目標充電率を書く。value は parse_charge_target を通した後の値であること。"""

    def mutator(data: Dict[str, Any]) -> None:
        data["charge_target_soc"] = value
        data["charge_target_updated_at"] = time.time()

    _update(mutator)
