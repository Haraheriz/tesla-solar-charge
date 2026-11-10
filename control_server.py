import os
import sys
import json
import html
import hmac
import logging
import re
import socket
import threading
import time
from logging.handlers import RotatingFileHandler
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from typing import Any, Dict, List, Optional

from config_loader import DAY_END_HOUR, DAY_START_HOUR, Settings, warn_if_accessible_by_others
from override_state import (
    parse_charge_target,
    read_snapshot,
    write_away_probe,
    write_charge_target,
    write_override,
)
from vehicle_status import load_vehicle_status

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOG_FILE: str = "control_server.log"
MAX_LOG_SIZE: int = 5 * 1024 * 1024
BACKUP_COUNT: int = 3

logger = logging.getLogger("ControlServer")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE: str = os.environ.get("TESLA_CONFIG_PATH", os.path.join(BASE_DIR, "tesla_config.json"))

if not os.path.exists(CONFIG_FILE):
    logger.critical(f"設定ファイル（{CONFIG_FILE}）が見つかりません。")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
    config: Dict[str, Any] = json.load(f)

# CONTROL_TOKEN を含むため、所有者以外から読める権限なら起動時に警告する（充電制御側と同じ検査）。
warn_if_accessible_by_others(CONFIG_FILE, logger.warning)

# 数値の設定は充電制御側と同じ入口を通す。片方だけ素の config.get に戻ると、
# 「設定を読む場所は1か所」という前提がまた崩れる。
settings = Settings(config, logger.warning)

# 0 は「OSに空きポートを割り当てさせる」を意味してしまい、
# スマホのホーム画面から開けなくなる。下限を1にする。
CONTROL_PORT: int = settings.integer("CONTROL_PORT", 8090, minimum=1)
MAX_AMPS: int = settings.integer("MAX_AMPS", 48, minimum=1)
control_token_value: Any = config.get("CONTROL_TOKEN")


def is_token_charset_valid(token: str) -> bool:
    """トークンが表示可能なASCII文字（0x21〜0x7E、空白を含まない）だけから成るかを返す。

    画面の JavaScript はトークンを X-Control-Token ヘッダーで送る。HTTP のヘッダー値に
    使える文字は RFC 9110 §5.5 で表示可能なASCII文字に限られ、ブラウザーの fetch() は
    それ以外の文字を含むヘッダー値を送らずに失敗する。クエリパラメータなら送れる値でも
    ヘッダーで送れなければ画面が動かないため、起動時に揃えて拒否する。
    `openssl rand -hex 32` が生成する値はこの条件を満たす。
    """
    return bool(token) and all(0x21 <= ord(c) <= 0x7E for c in token)


if (not isinstance(control_token_value, str) or len(control_token_value) < 32
        or control_token_value == "YOUR_RANDOM_CONTROL_TOKEN_HERE"
        or not is_token_charset_valid(control_token_value)):
    logger.critical(
        "CONTROL_TOKEN が文字列でない、32文字未満、テンプレート値、"
        "または表示可能なASCII文字以外を含んでいます。起動を中止します。"
    )
    sys.exit(1)
CONTROL_TOKEN: str = control_token_value

MAX_POST_BYTES: int = 4096
CLIENT_TIMEOUT_SEC: int = 5
MAX_CLIENTS: int = 16

# 1接続の処理全体（要求行・ヘッダー・本文の受信から応答まで）の締切秒。
#
# CLIENT_TIMEOUT_SEC は1回の受信ごとに適用される。そのため、5秒未満の間隔でヘッダーを
# 1バイトずつ送り続ける接続は締切に掛からず、同時処理の枠（MAX_CLIENTS）を占有し続けられた。
# 16接続でそれを行うと、正規の要求は枠が空くまで応答なしで閉じられる。
# スマートフォンは要求を数ミリ秒で送り終えるため、15秒を超える要求は正規の利用では起きない。
REQUEST_DEADLINE_SEC: float = 15.0

# 同時接続の上限による拒否は、直前の記録からこの秒数の間は数えるだけにし、まとめて記録する。
# まとめの行に載せる相手の IP は最大 REFUSAL_LOG_MAX_ADDRESSES 件とし、メモリと行の長さを抑える。
REFUSAL_LOG_INTERVAL_SEC: float = 60.0
REFUSAL_LOG_MAX_ADDRESSES: int = 16

ICONS_DIR: str = os.path.join(BASE_DIR, "icons")

# トークン無しで配信するアイコンの一覧。パスをそのままファイル名にせず、ここに載せたものだけを返す。
# purpose "any" と "maskable" を1つの画像で兼ねると、any として表示するときは切り抜き用の余白が
# 余分になり、maskable として切り抜くときは余白が足りず図柄の端が欠ける。別ファイルにして
# manifest で使い分ける（tools/gen_icon.py）。
ICON_FILES: frozenset = frozenset({
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-192.png",
    "icon-maskable-512.png",
    "apple-touch-icon-180.png",
})

def _read_template(name: str) -> str:
    path = os.path.join(BASE_DIR, "control_templates", name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()

PAGE_TEMPLATE: str = _read_template("page.html")
MANIFEST_TEMPLATE: str = _read_template("manifest.webmanifest")
SERVICE_WORKER_SCRIPT: str = _read_template("sw.js")


# 画面に返す車両の状態。vehicle_status.json のうち、表示に使うキーだけを渡す。
VEHICLE_STATUS_KEYS = (
    "battery_level",
    "charging_state",
    "charge_limit_soc",
    "charge_limit_soc_min",
    "charge_limit_applied_soc",
    "charge_limit_restore_soc",
    "observed_at",
    "target_reached_at",
)


def status_payload() -> Dict[str, Any]:
    """/api/status と各POSTの応答。画面は1回の応答で全体を描き直す。"""
    vehicle = load_vehicle_status()
    snap = read_snapshot()
    return {
        "manual_override": snap["manual_override"],
        "away_probe": snap["away_probe"],
        "charge_target_soc": snap["charge_target_soc"],
        "max_amps": MAX_AMPS,
        "day_start_hour": DAY_START_HOUR,
        "day_end_hour": DAY_END_HOUR,
        "vehicle": {key: vehicle.get(key) for key in VEHICLE_STATUS_KEYS},
    }


def _token_for_url(token: str) -> str:
    """URLのクエリ値としてのトークン。英数字と "_.-~" 以外はすべてパーセント符号化する。"""
    return quote(token, safe="")


def _token_for_js(token: str) -> str:
    """JavaScript の文字列リテラル（引用符を含む）としてのトークン。

    JSON の文字列リテラルは JavaScript の文字列リテラルとして読める。json.dumps は
    引用符・バックスラッシュ・制御文字を符号化し、非ASCIIは \\uXXXX にする。
    "</" を "<\\/" にするのは、値に "</script>" が含まれていても script 要素が
    そこで閉じないようにするためである。
    """
    return json.dumps(token).replace("</", "<\\/")


# 要求の記録に載せる文字列の上限。相手が送った任意の文字列を記録するため、長さを抑える。
LOG_TEXT_MAX_CHARS: int = 200

# token= の値は & か空白までを伏せる。引用符では止めない。repr() を通した要求行では、
# 値の途中の引用符がエスケープされたり、値の直後に閉じ引用符が付いたりするため。
_TOKEN_QUERY_PATTERN = re.compile(r"(token=)[^&\s]*", re.IGNORECASE)

# 表示する道具によって改行として扱われる文字。ASCII の制御文字、DEL、C1 制御文字（U+0085 を含む）、
# 行区切り（U+2028）、段落区切り（U+2029）。
_LINE_BREAKING_PATTERN = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def _token_forms(token: str) -> tuple:
    """記録の中にトークンが現れうる形。長いものから順に置き換える。

    - 生の形（X-Control-Token や、符号化しないクライアントのクエリ）
    - URL 符号化した形（画面と manifest はこの形でクエリに載せる。_token_for_url）
    - repr() でエスケープされた形（http.server は要求行を %r で記録に渡す。
      バックスラッシュは常に2つになり、引用符は文字列全体の内容によってエスケープされる）
    """
    escaped = token.replace("\\", "\\\\")
    forms = {
        token,
        quote(token, safe=""),
        escaped,
        escaped.replace("'", "\\'"),
        escaped.replace('"', '\\"'),
    }
    return tuple(sorted((form for form in forms if form), key=len, reverse=True))


def _escape_line_breaking(match: "re.Match") -> str:
    code = ord(match.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def text_for_log(text: str) -> str:
    """相手が送った文字列（パス・要求行）を記録用に整える。

    1. クエリの token= の値と、トークンそのもの（_token_forms の各形）を *** に置き換える。
       要求行には ?token= が付くため、そのまま記録するとログファイルと journal にトークンが残る。
       キーを %74oken= のように符号化した形は対象外である（アプリはこの形で送らない）。
       その場合も、値がトークンの生の形か URL 符号化した形であれば、2番目の置換で伏せる。
    2. 改行として扱われうる文字を \\xNN・\\uNNNN で表す。偽の記録行を作らせないため。
    3. LOG_TEXT_MAX_CHARS 文字で切る。切るのは伏せた後なので、トークンの一部が残ることはない。
    """
    masked = _TOKEN_QUERY_PATTERN.sub(r"\1***", text)
    for form in _TOKEN_FORMS:
        masked = masked.replace(form, "***")
    escaped = _LINE_BREAKING_PATTERN.sub(_escape_line_breaking, masked)
    if len(escaped) > LOG_TEXT_MAX_CHARS:
        return escaped[:LOG_TEXT_MAX_CHARS] + "…"
    return escaped


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return "<表示できない値>"


def format_for_log(format_: Any, args: tuple) -> str:
    """log_error()・log_message() の書式と引数から、記録する1行を作る。どう呼ばれても例外を出さない。

    http.server は書式を固定の文字列にし、相手が送った文字列は引数で渡している。ただしこれは
    標準ライブラリの書き方による性質で、保証ではない。相手の文字列が書式に入ると、% を含む
    要求行1つで TypeError・ValueError・KeyError を起こせる。記録の処理で例外が出ると、
    その接続は応答なしで切断され、トレースバックが journal に出る。

    - 引数が無ければ % で展開せず、書式をそのまま使う
    - 展開に失敗したら、書式と引数を repr() で並べる（repr() の失敗も受ける）
    - BaseException（KeyboardInterrupt など）は受けない
    最後に text_for_log() を通す。トークンは引数の側から入るため、組み立てた後に伏せる。
    """
    try:
        text = str(format_) % args if args else str(format_)
    except Exception:
        text = f"書式を展開できませんでした：{_safe_repr(format_)} " \
               f"({', '.join(_safe_repr(arg) for arg in args)})"
    return text_for_log(text)


_TOKEN_FORMS: tuple = _token_forms(CONTROL_TOKEN)


def render_page(token: str) -> str:
    """トークンは2つの文脈に埋め込む。文脈ごとに符号化を分ける。

    1. manifest への link 要素の href（HTML属性値の中のURLクエリ）
       … パーセント符号化してから HTML エスケープ
    2. script 要素の中の const TOKEN（JavaScript の文字列リテラル）
       … JSON の文字列リテラルとして符号化。HTML エスケープは script の中では復号されず、
         "&quot;" がそのまま値になるので使わない。

    トークンは tesla_config.json の設定値であり、ここで防いでいるのは利用者の設定値が
    画面を壊すことであって、第三者の入力ではない。
    """
    url_token = html.escape(_token_for_url(token), quote=True)
    return PAGE_TEMPLATE.replace("__TOKEN_URL__", url_token).replace('"__TOKEN_JS__"', _token_for_js(token))


def render_manifest(token: str) -> str:
    """start_url はJSON文字列の中のURLなので、パーセント符号化したうえでJSON文字列として符号化する。"""
    url_token = json.dumps(_token_for_url(token))[1:-1]
    return MANIFEST_TEMPLATE.replace("__TOKEN_URL__", url_token)


class ControlHandler(BaseHTTPRequestHandler):
    """太陽光追従ロジックのマニュアル・オーバーライドをスマホから切替えるためのHTTPハンドラー"""

    def _check_token(self, query: Dict[str, list]) -> bool:
        supplied = self.headers.get("X-Control-Token") or query.get("token", [None])[0]
        if not supplied:
            return False
        # str 同士の compare_digest は ASCII 以外の文字で TypeError を投げる。
        # 非ASCIIのトークンを送られると応答を返さずに切断し、トレースバックが journal に残っていた。
        # bytes に揃えれば文字種を問わず「一致しない」として 403 を返せる。
        return hmac.compare_digest(supplied.encode("utf-8"), CONTROL_TOKEN.encode("utf-8"))

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # 他サイトの iframe に埋め込ませない。トークン付きURLを知る相手にしか使えないため影響は
        # 小さいが、埋め込みを許す理由もない。frame-ancestors はヘッダーでのみ有効（meta では無効）。
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query: Dict[str, list] = parse_qs(parsed.query)

        if parsed.path == "/api/status":
            if not self._check_token(query):
                self._send_json(403, {"error": "invalid token"})
                return
            self._send_json(200, status_payload())
            return

        if parsed.path == "/":
            if not self._check_token(query):
                self._send_bytes(403, "Forbidden: invalid or missing token".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send_bytes(200, render_page(CONTROL_TOKEN).encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/manifest.webmanifest":
            if not self._check_token(query):
                self._send_json(403, {"error": "invalid token"})
                return
            self._send_bytes(200, render_manifest(CONTROL_TOKEN).encode("utf-8"), "application/manifest+json; charset=utf-8")
            return

        if parsed.path == "/sw.js":
            # PWAインストール判定に必要なService Workerはトークン不要の公開アセットとして配信する
            self._send_bytes(200, SERVICE_WORKER_SCRIPT.encode("utf-8"), "application/javascript; charset=utf-8")
            return

        if parsed.path.startswith("/icons/") and parsed.path[len("/icons/"):] in ICON_FILES:
            # アイコン画像自体は機密情報を含まないため、トークン無しで配信する
            icon_path = os.path.join(ICONS_DIR, os.path.basename(parsed.path))
            try:
                with open(icon_path, "rb") as f:
                    icon_bytes = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self._send_bytes(200, icon_bytes, "image/png")
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query: Dict[str, list] = parse_qs(parsed.query)

        if parsed.path not in ("/api/override", "/api/away_probe", "/api/charge_target"):
            self.send_response(404)
            self.end_headers()
            return

        if not self._check_token(query):
            self._send_json(403, {"error": "invalid token"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length < 0:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length > MAX_POST_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return
        raw_body = self.rfile.read(length) if length else b""
        try:
            payload: Dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return

        if parsed.path == "/api/charge_target":
            # 値の検査はここで1回だけ行い、通った値だけを保存する（parse_charge_target）。
            # 制御ループは保存された値を信用するが、手で書き換えられた場合に備えて読む側でも同じ関数を通す。
            if not isinstance(payload, dict) or "soc" not in payload:
                self._send_json(400, {"error": "soc is required"})
                return
            try:
                target = parse_charge_target(payload["soc"])
            except ValueError:
                self._send_json(400, {"error": "soc must be an integer from 1 to 100, or null"})
                return
            write_charge_target(target)
            logger.info(f"目標充電率を {'未設定' if target is None else f'{target}%'} に変更しました。")
            self._send_json(200, status_payload())
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            self._send_json(400, {"error": "enabled must be a boolean"})
            return
        enabled = payload["enabled"]

        if parsed.path == "/api/override":
            write_override(enabled)
            logger.info(f"マニュアル・オーバーライドを {'有効（フル充電）' if enabled else '無効（太陽光追従に復帰）'} に切替えました。")
        else:
            write_away_probe(enabled)
            logger.info(f"外出先の充電記録を {'有効（課金対象の問い合わせを再開）' if enabled else '無効'} に切替えました。")

        # 画面は1回の応答で全体を描き直す。一部だけ返すと、残りの表示が
        # 次のポーリング（5秒後）まで古いままになる。
        self._send_json(200, status_payload())

    def _path_for_log(self) -> str:
        """クエリ文字列を除いたパス。要求行を解釈できなかった要求には path が無いので "-" とする。"""
        path = getattr(self, "path", None)
        if not path:
            return "-"
        return text_for_log(urlparse(path).path or "-")

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """応答1件を1行で記録する。

        4xx・5xx と画面の読み込み（GET /）は INFO に出す。2026-10-26 19:06〜19:14 に
        スマホの操作が効かなかった時間帯は、要求が届いていたか、403 だったかを記録から
        確認できなかった（docs/DEPLOY_LOG.md の v0.8.0）。
        それ以外の成功（5秒ごとの /api/status、アイコン、Service Worker）は DEBUG のままにする。
        画面を1つ開いているだけで1日に約17,000行になり、ログファイル（5MB×4本）の
        保存期間が短くなるため。"""
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        method = text_for_log(getattr(self, "command", None) or "-")
        path = self._path_for_log()
        line = f"{self.client_address[0]} からの {method} {path} に {status} を返しました。"
        if status >= 400 or (method == "GET" and path == "/"):
            logger.info(line)
        else:
            logger.debug(line)

    def log_error(self, format: str, *args: Any) -> None:
        """http.server が検出した異常（受信の時間切れ、解釈できない要求行など）を INFO で記録する。

        既定の実装は log_message() を通り DEBUG になっていた。メッセージには要求行が
        含まれることがあるため、format_for_log() の中の text_for_log() でトークンを伏せる。"""
        logger.info(f"{self.client_address[0]} との通信で異常を検出しました：{format_for_log(format, args)}")

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format_for_log(format, args))


class _RequestState:
    """1接続の処理が終わったかどうか。締切のタイマーと処理のスレッドが lock の中で読み書きする。"""

    __slots__ = ("lock", "finished")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.finished = False


class ControlServer(ThreadingHTTPServer):
    """同時接続を16件に制限し、各接続の読み取りを5秒で、処理全体を15秒で打ち切る。"""

    request_queue_size = MAX_CLIENTS

    def __init__(self, server_address: tuple, handler_class: type) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        # 上限による拒否の記録を間引くための状態。_refusal_lock の中でだけ読み書きする。
        self._refusal_lock = threading.Lock()
        self._refusal_logged_at: Optional[float] = None
        self._refused_count = 0
        self._refused_addresses: List[str] = []
        super().__init__(server_address, handler_class)

    def get_request(self) -> tuple:
        request, client_address = super().get_request()
        request.settimeout(CLIENT_TIMEOUT_SEC)
        return request, client_address

    def _record_refusal(self, address: str) -> None:
        """上限による拒否を記録する。直前の記録から REFUSAL_LOG_INTERVAL_SEC 秒以内の拒否は数えるだけにする。

        枠が埋まっている間、スマホの要求は応答なしで閉じられる。記録が無いと、「操作が効かない」
        原因がこの拒否かどうかを後から判別できない。一方で、拒否1件ごとに1行を書くと、枠を
        埋め続ける接続があるとき1秒に数十行になり、control_server.log（5MB×4本）から調べたい
        時間帯の記録を押し出す。数えた拒否は、次に記録するとき（間隔を過ぎた拒否か、枠が空いて
        受け付けた接続）に1行にまとめる。"""
        with self._refusal_lock:
            now = time.monotonic()
            if self._refusal_logged_at is not None and now - self._refusal_logged_at < REFUSAL_LOG_INTERVAL_SEC:
                self._refused_count += 1
                if address not in self._refused_addresses and len(self._refused_addresses) < REFUSAL_LOG_MAX_ADDRESSES:
                    self._refused_addresses.append(address)
                return
            summary = self._take_refusal_summary()
            self._refusal_logged_at = now
        logger.warning(
            f"同時接続が上限（{MAX_CLIENTS}件）に達しているため、{address} からの接続を応答せずに閉じました。{summary}"
        )

    def _take_refusal_summary(self) -> str:
        """数えた拒否の件数と相手を1文にし、数え直す。_refusal_lock の中で呼ぶ。"""
        if not self._refused_count:
            return ""
        addresses = "、".join(self._refused_addresses)
        if self._refused_count > len(self._refused_addresses) and len(self._refused_addresses) >= REFUSAL_LOG_MAX_ADDRESSES:
            addresses += " ほか"
        summary = f"前回の記録の後に、同じ理由で {self._refused_count} 件を閉じていました（相手：{addresses}）。"
        self._refused_count = 0
        self._refused_addresses = []
        return summary

    def _flush_refusals(self) -> None:
        """枠が空いて接続を受け付けたとき、数えたまま記録していない拒否を1行で記録する。"""
        with self._refusal_lock:
            summary = self._take_refusal_summary()
            if summary:
                self._refusal_logged_at = None
        if summary:
            logger.warning(f"同時接続の上限による拒否が終わりました。{summary}")

    def process_request(self, request: Any, client_address: tuple) -> None:
        if not self._slots.acquire(blocking=False):
            self._record_refusal(client_address[0])
            self.shutdown_request(request)
            return
        self._flush_refusals()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    @staticmethod
    def _abort_request(request: Any, client_address: tuple, state: "_RequestState") -> None:
        """締切を過ぎた接続を閉じる。受信待ちのスレッドは空の読み取りを受けて要求処理を終える。

        Timer.cancel() は発火済みのタイマーを止められない。処理が終わる直前に発火すると、
        終わった要求を「締切で閉じた」と記録してしまう。そのため、処理の完了と締切による切断は
        state.lock の中で判定し、先に来た一方だけを実行する。処理が先に終わっていれば、
        記録も切断もしない。

        ロックの中では判定と切断だけを行い、記録はロックを放してから書く。shutdown() は待たずに
        返るが、ログの書き込み（ファイルと journal）は待つことがある。ロックの中で書くと、その間
        同じ接続の後始末（process_request_thread の finally）が待たされる。切断を済ませてから
        書くので、記録の内容は判定の結果と一致する。"""
        with state.lock:
            if state.finished:
                return
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        logger.info(
            f"{client_address[0]} との接続を、処理の締切（{REQUEST_DEADLINE_SEC}秒）を過ぎたため閉じました。"
        )

    def handle_error(self, request: Any, client_address: tuple) -> None:
        """相手か締切が閉じた接続での読み書きの失敗は1行で記録し、それ以外は既定どおりトレースバックを出す。

        締切で閉じた接続（_abort_request）への書き込みや、処理中に切断したスマホとの読み書きは
        BrokenPipeError か ConnectionResetError になる。既定の handle_error() は例外の種類に
        かかわらずトレースバックを標準エラーへ出すため、journal で不具合と区別できなくなる。"""
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            logger.info(f"{client_address[0]} との接続は、要求の処理中に閉じられていました（{type(error).__name__}）。")
            return
        super().handle_error(request, client_address)

    def process_request_thread(self, request: Any, client_address: tuple) -> None:
        state = _RequestState()
        deadline = threading.Timer(REQUEST_DEADLINE_SEC, self._abort_request, args=(request, client_address, state))
        deadline.daemon = True
        deadline.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            with state.lock:
                state.finished = True
            deadline.cancel()
            self._slots.release()


def main() -> None:
    server = ControlServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    logger.info("=========================================================================")
    logger.info(f"スマホ操作用コントロールサーバーをポート {CONTROL_PORT} で起動しました。")
    logger.info("=========================================================================")
    server.serve_forever()


if __name__ == "__main__":
    main()
