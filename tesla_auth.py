"""Tesla トークンファイルの読み書きと、更新の排他。

充電制御と観測ツールが同じ tesla_tokens.json を更新する。リフレッシュトークンは
使用のたびに置き換わるため、同時に更新すると片方のトークンが無効になる。
保存形式は access_token、refresh_token、token_expires_at で、期限は expires_in の 1800 秒前。
"""
import json
import os
from typing import Any, Dict, Optional, Tuple

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

TOKEN_SKEW_SEC = 1800


def load_token_file(path: str) -> Optional[Dict[str, Any]]:
    """トークンファイルを返す。無い・壊れている場合は None。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def save_token_file(path: str, access: str, refresh: Optional[str], expires_in: int) -> float:
    """原子的に書き、token_expires_at（現在時刻 + expires_in - 1800 秒）を返す。"""
    expires_at = time_now() + int(expires_in) - TOKEN_SKEW_SEC
    data = {
        "access_token": access,
        "refresh_token": refresh,
        "token_expires_at": expires_at,
    }
    tmp_file = path + ".tmp"
    fd = os.open(tmp_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=4)
    os.replace(tmp_file, path)
    return expires_at


def time_now() -> float:
    import time
    return time.time()


def try_lock(path: str, blocking: bool = True) -> Optional[int]:
    """トークン更新のロックを取る。POSIX 以外、または取れない非待ちでは None。

    戻り値は解放するまで閉じないファイル記述子。unlock に渡す。
    """
    if fcntl is None or os.name != "posix":
        return None
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, flags)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def unlock(fd: Optional[int]) -> None:
    if fd is None or fcntl is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def epc_value(properties: Any, epc: int) -> Tuple[Optional[str], Optional[str]]:
    """ECHONET プロパティから epc の値と updated_at を返す。無ければ (None, None)。"""
    if not isinstance(properties, list):
        return None, None
    for prop in properties:
        if isinstance(prop, dict) and prop.get("epc") == epc:
            value = prop.get("val")
            updated = prop.get("updated_at")
            return value, updated if isinstance(updated, str) else None
    return None, None
