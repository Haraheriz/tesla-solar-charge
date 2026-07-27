import os
import sys
import json
import time
import secrets
import requests
import logging
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any, Tuple

from override_state import read_override_state, write_override

# Windows環境での標準出力のエンコーディング問題を解決
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ==========================================
# 構造化ロギングの設定
# ==========================================
LOG_FILE: str = "tesla_solar_charger.log"
MAX_LOG_SIZE: int = 5 * 1024 * 1024  # 5MB
BACKUP_COUNT: int = 3

# INFO と WARNING の間に「ATTENTION」レベルを設ける。
# フル充電モード（マニュアル・オーバーライド）はユーザーが意図して入れる正常な状態であり
# WARNING では意味が強すぎる。一方でINFOに埋もれると切り忘れに気づけないため、
# 「異常ではないが人間の注意を向けたい事実」を専用レベルとして分離する。
ATTENTION: int = 25
logging.addLevelName(ATTENTION, "ATTENTION")

logger = logging.getLogger("SolarCharger")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")


def log_attention(message: str) -> None:
    logger.log(ATTENTION, message)

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# ==========================================
# 設定ファイルの自動読み込み ＆ 型ガード
# ==========================================
# 実行時のカレントディレクトリに依存しないよう、スクリプト自身の場所を基準に解決する。
# 環境ごとに置き場所を変えたい場合は環境変数 TESLA_CONFIG_PATH / TESLA_TOKEN_PATH で上書き可能。
BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE: str = os.environ.get("TESLA_CONFIG_PATH", os.path.join(BASE_DIR, "tesla_config.json"))
TOKEN_FILE: str = os.environ.get("TESLA_TOKEN_PATH", os.path.join(BASE_DIR, "tesla_tokens.json"))

if not os.path.exists(CONFIG_FILE):
    logger.critical(f"設定ファイル（{CONFIG_FILE}）が見つかりません。")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config: Dict[str, Any] = json.load(f)

REMO_ACCESS_TOKEN: str = str(config.get("REMO_ACCESS_TOKEN", ""))
CLIENT_ID: str = str(config.get("CLIENT_ID", ""))
CLIENT_SECRET: str = str(config.get("CLIENT_SECRET", ""))
DOMAIN: str = str(config.get("DOMAIN", "localhost:8000"))

MIN_AMPS: int = int(config.get("MIN_AMPS", 4))
MAX_AMPS: int = int(config.get("MAX_AMPS", 48))

# ヒステリシス用の開始閾値。充電開始・車両起動はSTART_AMPS以上の余剰を要求し、
# 停止判定はMIN_AMPS未満で行う。開始直後は車両自身の消費で余剰が減るため、
# 閾値が1本だと開始→即停止の発振が起きる。
START_AMPS: int = int(config.get("START_AMPS", MIN_AMPS + 2))

# 停止判定のデバウンス回数。余剰不足がこの回数連続したときだけ充電を停止する。
STOP_DEBOUNCE_CYCLES: int = int(config.get("STOP_DEBOUNCE_CYCLES", 2))

# Remo E瞬時電力の平滑化（1サイクル内のサンプル数と間隔秒）。
# 冷蔵庫・雲などによる±数百Wの揺れをならして誤判定を減らす。
REMO_SAMPLES: int = 3
REMO_SAMPLE_INTERVAL_SEC: int = 10

# 充電コマンド（charge_start / charge_stop / set_charging_amps）のリトライ回数。
# Tesla APIは車両が通信圏外・スリープ移行中などで HTTP 408 や result=false を頻繁に返すため、
# 「投げっぱなしで成功したことにする」と停止漏れがそのまま夜通しの系統充電になる。
COMMAND_RETRIES: int = int(config.get("COMMAND_RETRIES", 3))

# 満充電・ケーブル未接続などの「終端ステータス」を観測したあと、次に車両へ問い合わせるまでの待機秒。
# この間は就寝中の車両をWake Upしない（無意味な叩き起こしとAPIレートリミットを防ぐ）。
TERMINAL_BACKOFF_SEC: int = int(config.get("TERMINAL_BACKOFF_SEC", 600))

# 夜間休止に入る際の充電停止確認を、成功するまで再試行する上限回数（1回=10分間隔）。
# 無制限に再試行するとAPIレートリミット(429)を誘発するため上限を設ける。
NIGHT_STOP_MAX_ATTEMPTS: int = int(config.get("NIGHT_STOP_MAX_ATTEMPTS", 6))

# 充電中を意味するステータス（このときだけ電流調整・停止判定を行う）
STATUS_CHARGING: str = "Charging"
# 充電可能だが止まっているステータス（余剰があれば開始を検討する）
STATUS_STOPPED: str = "Stopped"
# 充電開始処理の途中。コマンドを重ねず次サイクルまで待つ。
STATUS_STARTING: str = "Starting"
# 終端ステータス：これ以上こちらから充電を促しても意味がない状態。
# 満充電(Complete)にcharge_startを送り続けると、車両を無駄に起こしAPIを浪費するだけになる。
TERMINAL_STATUSES: frozenset = frozenset({"Disconnected", "Complete", "NoPower"})
TERMINAL_STATUS_LABELS: Dict[str, str] = {
    "Disconnected": "充電ケーブルが未接続",
    "Complete": "満充電に到達済み",
    "NoPower": "充電設備から給電されていない",
}

# 動作確認用：コマンドライン引数 --force-run または環境変数 FORCE_RUN=1 で
# 夜間休止モード（7:00-18:00以外は停止）を無視して常時稼働させ、
# サイクルごとに画面入力で仮想の家庭消費電力（W）を指定できるようにする
FORCE_RUN: bool = "--force-run" in sys.argv or os.environ.get("FORCE_RUN") == "1"

AUTH_URL: str = "https://auth.tesla.com/oauth2/v3/token"
PROXY_HOST: str = "https://localhost:4443"

# ローカルTesla HTTPプロキシ用の自己署名証明書のパス。
# 環境ごとに置き場所を変えたい場合は環境変数 TESLA_CERT_PATH で上書き可能。
PROXY_CERT_PATH: str = os.environ.get("TESLA_CERT_PATH", os.path.join(BASE_DIR, "cert.pem"))

# ローカルプロキシ宛と外部クラウドAPI宛でTLS検証方法を明確に分離する。
# proxy_session: 自己署名証明書(cert.pem)をピン留めして検証
# cloud_session: 標準CAバンドルで通常のチェーン検証
proxy_session = requests.Session()
proxy_session.verify = PROXY_CERT_PATH

cloud_session = requests.Session()
cloud_session.verify = True

received_code: Optional[str] = None
expected_oauth_state: Optional[str] = None
refresh_token: Optional[str] = None
access_token: Optional[str] = None
token_expires_at: float = 0.0

# トークンリフレッシュの連続失敗回数。指数バックオフとCRITICAL通知の判断に使う。
token_refresh_failures: int = 0
TOKEN_RETRY_BASE_SEC: int = 180
TOKEN_RETRY_MAX_SEC: int = 3600
TOKEN_FAILURE_ALERT_THRESHOLD: int = 5


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """手元PCでの完全自動着地用ローカルWEBサーバー"""
    def do_GET(self) -> None:
        global received_code
        if self.path.startswith("/callback"):
            query: Dict[str, list] = parse_qs(urlparse(self.path).query)
            if "code" in query and query.get("state", [None])[0] == expected_oauth_state:
                received_code = query["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h2>ノンコピペ認証に完全成功しました！</h2><p>この画面を閉じて、PowerShell側を確認してください。</p>".encode("utf-8"))
                return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format % args)


def get_remo_power() -> Optional[int]:
    url: str = "https://api.nature.global/1/appliances"
    headers: Dict[str, str] = {"Authorization": f"Bearer {REMO_ACCESS_TOKEN}"}
    try:
        response = cloud_session.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        for appliance in response.json():
            if appliance.get("type") == "EL_SMART_METER":
                properties: list = appliance.get("smart_meter", {}).get("echonetlite_properties", [])
                if not properties:
                    logger.warning("Nature Remo E がスマートメーターの一時的な空応答を検知しました。")
                    return None
                for prop in properties:
                    if prop.get("epc") == 231:
                        return int(prop.get("val"))
    except Exception as e:
        logger.error(f"Nature Remo E 通信エラー: {e}")
    return None

def get_remo_power_smoothed() -> Optional[int]:
    """瞬時値を複数回サンプリングして平均する。取得できた分だけで平均し、全滅ならNone。"""
    readings: list = []
    for i in range(REMO_SAMPLES):
        if i > 0:
            time.sleep(REMO_SAMPLE_INTERVAL_SEC)
        value = get_remo_power()
        if value is not None:
            readings.append(value)
    if not readings:
        return None
    return round(sum(readings) / len(readings))

def post_vehicle_command(
    vin: str,
    headers: Dict[str, str],
    command: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """車両コマンドを1回だけ送信し、(成功したか, 失敗理由) を返す。

    Tesla APIは HTTP 200 を返しつつ本文で result=false（reason付き）を返すことがあるため、
    ステータスコードと result の両方を検証しないと「成功したつもり」になる。
    """
    url: str = f"{PROXY_HOST}/api/1/vehicles/{vin}/command/{command}"
    try:
        res = proxy_session.post(url, headers=headers, json=payload or {}, timeout=15)
    except Exception as e:
        return False, f"通信例外: {e}"

    if res.status_code != 200:
        return False, f"HTTP {res.status_code}"

    try:
        body: Dict[str, Any] = res.json().get("response") or {}
    except ValueError:
        return False, "レスポンスがJSONとして解釈できません"

    if body.get("result") is True:
        return True, ""
    return False, str(body.get("reason") or "result=false（理由なし）")


def send_charge_command(vin: str, headers: Dict[str, str], command: str) -> bool:
    """charge_start / charge_stop を、成功が確認できるまでリトライしながら送信する。"""
    label: str = "充電開始" if command == "charge_start" else "充電停止"
    for attempt in range(1, COMMAND_RETRIES + 1):
        ok, reason = post_vehicle_command(vin, headers, command)
        if ok:
            if attempt > 1:
                logger.info(f"{label}コマンドが{attempt}回目で成功しました。")
            return True

        # 停止したい相手が既に充電していない／開始したい相手が既に充電中なら、目的は達成されている。
        if command == "charge_stop" and reason in ("not_charging", "is_charging_stopped"):
            logger.info("充電は既に停止済みでした。")
            return True
        if command == "charge_start" and reason in ("is_charging", "already_started"):
            logger.info("充電は既に開始済みでした。")
            return True

        logger.warning(f"{label}コマンドに失敗しました（{attempt}/{COMMAND_RETRIES}回目）: {reason}")
        if attempt < COMMAND_RETRIES:
            time.sleep(2 * attempt)

    logger.error(f"{label}コマンドが{COMMAND_RETRIES}回連続で失敗しました。")
    return False


def set_charging_amps(vin: str, headers: Dict[str, str], amps: int) -> bool:
    """充電電流を設定する。5A未満はTesla APIの既知の癖で1回では反映されないことがあるため2回送信する。"""
    # 5A未満は「2回送らないと反映されない」という車両側の癖への対処であり、失敗時のリトライとは別物。
    # そのため必ず規定回数を送り切ったうえで、最後の結果が成功していなければリトライへ回す。
    required_sends: int = 2 if amps < 5 else 1
    for attempt in range(1, COMMAND_RETRIES + 1):
        ok: bool = False
        reason: str = ""
        for i in range(required_sends):
            if i > 0:
                time.sleep(2)
            ok, reason = post_vehicle_command(vin, headers, "set_charging_amps", {"charging_amps": amps})
        if ok:
            return True
        logger.warning(f"電流設定（{amps}A）に失敗しました（{attempt}/{COMMAND_RETRIES}回目）: {reason}")
        if attempt < COMMAND_RETRIES:
            time.sleep(2 * attempt)

    logger.error(f"電流設定（{amps}A）が{COMMAND_RETRIES}回連続で失敗しました。")
    return False


def format_duration(seconds: float) -> str:
    """経過秒を「N時間N分」形式にする（1時間未満は「N分」）。"""
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}時間{minutes}分" if hours else f"{minutes}分"


def save_tokens(acc: str, ref: Optional[str], exp_in: int) -> None:
    global token_expires_at
    token_expires_at = time.time() + int(exp_in) - 1800
    token_data: Dict[str, Any] = {
        "access_token": acc, "refresh_token": ref, "token_expires_at": token_expires_at
    }
    tmp_file: str = TOKEN_FILE + ".tmp"
    try:
        fd = os.open(tmp_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(token_data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_file, TOKEN_FILE)
    except Exception as e:
        logger.error(f"トークンファイルの安全な保存に失敗しました: {e}")

def load_tokens() -> bool:
    global access_token, refresh_token, token_expires_at
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
                access_token = data.get("access_token")
                refresh_token = data.get("refresh_token")
                if refresh_token == "None":
                    refresh_token = None
                token_expires_at = float(data.get("token_expires_at", 0.0))
                return True
        except Exception:
            return False
    return False

def refresh_tesla_token() -> bool:
    global access_token, refresh_token, token_refresh_failures
    if not refresh_token:
        logger.error("リフレッシュトークンが存在しないため、自動リフレッシュをスキップします。")
        token_refresh_failures += 1
        return False
    logger.info("アクセストークンを自動リフレッシュ中...")
    payload: Dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token
    }
    try:
        res = cloud_session.post(AUTH_URL, data=payload, timeout=10)
        res.raise_for_status()
        data: Dict[str, Any] = res.json()
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        save_tokens(
            str(access_token) if access_token else "",
            refresh_token,
            int(data.get("expires_in", 28800))
        )
        logger.info("トークンの自動保存・更新に成功しました。")
        token_refresh_failures = 0
        return True
    except Exception as e:
        token_refresh_failures += 1
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        if status_code in (400, 401):
            # invalid_grant 等はリトライしても回復しない。過去に401で29日間・6357回リトライし続け、
            # その間システムが完全に無機能だった実績があるため、必ずCRITICALで顕在化させる。
            logger.critical(
                f"リフレッシュトークンが失効しています（HTTP {status_code}）。"
                "手動での再認証（docs/03_operation.md 第4章）が必要です。"
            )
        else:
            logger.error(f"トークンのリフレッシュに失敗しました: {e}")
        if token_refresh_failures == TOKEN_FAILURE_ALERT_THRESHOLD:
            logger.critical(
                f"トークンのリフレッシュが{token_refresh_failures}回連続で失敗しています。"
                "充電制御は停止状態です。再認証手順を確認してください。"
            )
        return False


def token_retry_wait_sec() -> int:
    """リフレッシュ失敗が続くほど待機を伸ばす（180秒 → 最大3600秒）。"""
    wait = TOKEN_RETRY_BASE_SEC * (2 ** max(0, token_refresh_failures - 1))
    return int(min(wait, TOKEN_RETRY_MAX_SEC))


def wake_up_vehicle(vin: str, headers: Dict[str, str]) -> bool:
    logger.info("車両へ起動命令（Wake Up）を送信します...")
    url: str = f"{PROXY_HOST}/api/1/vehicles/{vin}/wake_up"
    for i in range(5):
        try:
            res = proxy_session.post(url, headers=headers, timeout=15)
            if res.status_code == 200:
                state: str = res.json().get("response", {}).get("state", "")
                if state == "online":
                    logger.info("車両がオンラインになりました！データ同期のため60秒間待機します...")
                    time.sleep(60)
                    return True
        except Exception:
            pass
        logger.info(f"起動待機中 ({i+1}/5回)...")
        time.sleep(10)
    return False

def main() -> None:
    global received_code, expected_oauth_state, access_token, refresh_token, token_expires_at

    logger.info("=========================================================================")
    logger.info("太陽光自動充電制御システム（トークン手元調達・初回手動認証方式）起動")
    logger.info("=========================================================================")

    if load_tokens():
        logger.info("有効なトークンファイルを発見。保存済みトークンで再ログインします。")
        if time.time() > token_expires_at:
            if not refresh_tesla_token():
                logger.error("トークンリフレッシュに失敗。再認証が必要です。")
                if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)

    # 初回生成（手元のWindows PC実行時のみここを通る）
    if not os.path.exists(TOKEN_FILE) or refresh_token is None:
        redirect_uri = f"http://{DOMAIN}/callback"
        expected_oauth_state = secrets.token_urlsafe(32)
        login_url: str = f"https://auth.tesla.com/oauth2/v3/authorize?client_id={CLIENT_ID}&redirect_uri={redirect_uri}&response_type=code&scope=openid%20offline_access%20vehicle_device_data%20vehicle_charging_cmds&state={expected_oauth_state}"

        logger.warning("初回認証手続きを開始します。ブラウザが自動起動しない場合は以下を開いてください：")
        logger.warning(f"\n{login_url}\n")

        # Windows環境ならブラウザを自動で起動する
        try:
            import webbrowser
            webbrowser.open(login_url)
        except Exception:
            pass

        # 同じPC内のブラウザからの自動着地を待ち伏せる
        httpd = HTTPServer(('127.0.0.1', 8000), OAuthCallbackHandler)
        while received_code is None:
            httpd.handle_request()

        logger.info("着地を検知！ 初回アクセストークンを取得中...")
        token_payload: Dict[str, str] = {
            "grant_type": "authorization_code", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "code": received_code, "redirect_uri": redirect_uri
        }
        res = cloud_session.post(AUTH_URL, data=token_payload, timeout=10)
        res.raise_for_status()

        token_data: Dict[str, Any] = res.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        save_tokens(
            str(access_token) if access_token else "",
            refresh_token,
            int(token_data.get("expires_in", 28800))
        )
        logger.info("手元でのトークン生成に完全成功！『tesla_tokens.json』が生成されました。")

    headers: Dict[str, str] = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Accept": "application/json"}

    v_res = proxy_session.get(f"{PROXY_HOST}/api/1/vehicles", headers=headers, timeout=10)
    vehicles = v_res.json().get("response") or []
    if not vehicles:
        logger.critical("車両リストを取得できませんでした。終了します。")
        sys.exit(1)

    vin: str = (vehicles[0] or {}).get("vin", "")
    logger.info(f"対象車両 (VIN: {vin}) を捕捉。常駐ループ稼働を開始します。")
    print("-------------------------------------------------------------------------")

    if FORCE_RUN:
        logger.warning("FORCE_RUNモード：夜間休止モードを無視して常時稼働します（動作確認専用）。")
        logger.warning("サイクルごとに画面で仮想の家庭消費電力（W）を入力できます（空Enterで実測値を使用）。")

    below_min_count: int = 0  # 停止デバウンス用：余剰不足が連続したサイクル数
    night_charge_checked: bool = False  # 夜間休止入り時の充電停止確認が完了したかどうか
    night_stop_attempts: int = 0  # 夜間休止入り時の停止確認をこの休止帯で何回試したか
    # 満充電・ケーブル未接続などの終端状態を観測したら、この時刻までは車両を起こさない
    skip_wake_until: float = 0.0
    # 直前サイクルで観測した充電ステータス（外部からの手動停止を検知するために保持する）
    prev_charging_status: str = ""

    while True:
        now = time.localtime()
        manual_override, override_updated_at = read_override_state()

        if not manual_override and not FORCE_RUN and not (7 <= now.tm_hour < 18):
            logger.info("--- 定期チェック開始 ---")
            logger.info(f"夜間休止モード中（現在時刻 {time.strftime('%H:%M:%S')}）")

            # 夜間休止に入る際、充電中なら停止してから休止する。充電中のまま休止に入ると
            # 余剰の有無に関わらず朝まで系統充電が続いてしまうため。
            # 停止が「確認できた」ときだけ完了フラグを立てる。以前はtryに入る前にフラグを立てており、
            # 通信エラーや停止失敗があってもその夜は二度と確認しなかった。
            if not night_charge_checked and night_stop_attempts < NIGHT_STOP_MAX_ATTEMPTS:
                night_stop_attempts += 1
                try:
                    if time.time() > token_expires_at and refresh_tesla_token():
                        headers["Authorization"] = f"Bearer {access_token}"
                    v_res = proxy_session.get(f"{PROXY_HOST}/api/1/vehicles", headers=headers, timeout=10)
                    vehicles = v_res.json().get("response") or [] if v_res.status_code == 200 else []
                    vehicle = vehicles[0] if vehicles else {}
                    vehicle_state = str(vehicle.get("state", ""))

                    if not vehicle:
                        # 車両リストが取れないうちは「充電していない」と断定できないため、次の巡回で再試行する。
                        logger.warning(
                            f"夜間休止前の車両リスト取得に失敗しました（{night_stop_attempts}/{NIGHT_STOP_MAX_ATTEMPTS}回目）。次の巡回で再確認します。"
                        )
                    elif vehicle_state in ("asleep", "offline"):
                        # 就寝中の車両は充電していない。ここで起こすと設計思想（Insomnia Defense）に反する。
                        logger.info(f"車両は『{vehicle_state}』のため充電していないと判断し、そのまま休止します。")
                        night_charge_checked = True
                    else:
                        vin = vehicle.get("vin", "") or vin
                        s_res = proxy_session.get(f"{PROXY_HOST}/api/1/vehicles/{vin}/vehicle_data?endpoints=charge_state", headers=headers, timeout=10)
                        if s_res.status_code != 200:
                            logger.warning(
                                f"夜間休止前の充電状態取得に失敗しました (HTTP {s_res.status_code})（{night_stop_attempts}/{NIGHT_STOP_MAX_ATTEMPTS}回目）。次の巡回で再確認します。"
                            )
                        else:
                            night_charge_state = (s_res.json().get("response") or {}).get("charge_state") or {}
                            if str(night_charge_state.get("charging_state", "")) == STATUS_CHARGING:
                                logger.info("夜間休止に入るため、進行中の充電を『一時停止』します。")
                                if send_charge_command(vin, headers, "charge_stop"):
                                    logger.info("充電の停止を確認しました。")
                                    night_charge_checked = True
                                else:
                                    logger.error(
                                        f"夜間休止前の充電停止に失敗しました（{night_stop_attempts}/{NIGHT_STOP_MAX_ATTEMPTS}回目）。次の巡回で再試行します。"
                                    )
                            else:
                                night_charge_checked = True
                except Exception as e:
                    logger.error(
                        f"夜間休止前の充電状態確認でエラー（{night_stop_attempts}/{NIGHT_STOP_MAX_ATTEMPTS}回目）: {e}"
                    )

                if not night_charge_checked and night_stop_attempts >= NIGHT_STOP_MAX_ATTEMPTS:
                    logger.critical(
                        f"夜間休止前の充電停止を{NIGHT_STOP_MAX_ATTEMPTS}回試みましたが確認できませんでした。"
                        "系統からの充電が継続している可能性があります。手動で確認してください。"
                    )

            logger.info("次の稼働チェックまで10分間スリープします...")
            time.sleep(600)
            continue

        night_charge_checked = False
        night_stop_attempts = 0

        if time.time() > token_expires_at:
            if not refresh_tesla_token():
                wait_sec = token_retry_wait_sec()
                logger.info(f"トークン再取得まで{wait_sec}秒待機します。")
                time.sleep(wait_sec)
                continue
            headers["Authorization"] = f"Bearer {access_token}"

        logger.info("--- 定期チェック開始 ---")
        if manual_override:
            # 自動解除はしない仕様。切り忘れに気づけるよう、経過時間つきでATTENTIONに出す。
            elapsed_label: str = (
                f"ON から{format_duration(time.time() - override_updated_at)}経過"
                if override_updated_at > 0 else "ON からの経過時間は不明"
            )
            night_note: str = "／夜間休止を迂回中" if not (7 <= now.tm_hour < 18) else ""
            log_attention(
                f"フル充電モード継続中（{elapsed_label}{night_note}）："
                f"太陽光の発電状況に関わらず最大{MAX_AMPS}Aで充電します。"
            )

        house_power: Optional[int] = None
        if not manual_override:
            if FORCE_RUN:
                user_input = input(
                    "[FORCE_RUNモード] 仮想の家庭消費電力(W)を入力（負の値＝売電中/余剰あり、空Enterで実測値を使用）: "
                ).strip()
                if user_input:
                    try:
                        house_power = int(user_input)
                    except ValueError:
                        logger.warning("数値として認識できなかったため、実測値を使用します。")

            if house_power is None:
                house_power = get_remo_power_smoothed()
                if house_power is None:
                    time.sleep(180)
                    continue

        try:
            v_res = proxy_session.get(f"{PROXY_HOST}/api/1/vehicles", headers=headers, timeout=10)
            if v_res.status_code != 200:
                logger.warning(f"車両リスト取得エラー (HTTP {v_res.status_code})。10分待機します。")
                time.sleep(600)
                continue

            vehicles = v_res.json().get("response") or []
            if not vehicles:
                time.sleep(180)
                continue

            vin = (vehicles[0] or {}).get("vin", "") or vin
            vehicle_state: str = str((vehicles[0] or {}).get("state", ""))
            power_label: str = "オーバーライド中（太陽光は無視）" if manual_override else f"{house_power} W"
            logger.info(f"車両状態: 『{vehicle_state}』 (RemoE平均電力: {power_label})")

            if vehicle_state in ["asleep", "offline"]:
                if time.time() < skip_wake_until:
                    # 直近で満充電・ケーブル未接続などを観測している。状況が変わるまで車両を起こさない。
                    logger.info(
                        f"直近の充電状態（{prev_charging_status or '終端状態'}）から充電不要と判断し、車両を起こさずに待機します。"
                    )
                    time.sleep(TERMINAL_BACKOFF_SEC)
                    continue
                if not manual_override and house_power >= -(START_AMPS * 200):
                    logger.info(f"車両は就寝中、かつ余剰が開始閾値（{START_AMPS * 200}W）未満のため、このまま寝かせます。")
                    time.sleep(180)
                    continue
                else:
                    if manual_override:
                        logger.info("マニュアル・オーバーライドのため、車両を起動します。")
                    else:
                        logger.info(f"開始閾値以上の余剰電力（{START_AMPS * 200}W以上）を検知したため、車両を起動します。")
                    if not wake_up_vehicle(vin, headers):
                        time.sleep(180)
                        continue

            state_url: str = f"{PROXY_HOST}/api/1/vehicles/{vin}/vehicle_data?endpoints=charge_state"
            s_res = proxy_session.get(state_url, headers=headers, timeout=10)

            if s_res.status_code == 401:
                token_expires_at = 0.0
                continue
            elif s_res.status_code == 429:
                logger.error("テスラAPIレートリミット(429)。1時間処理を退避します。")
                time.sleep(3600)
                continue
            elif s_res.status_code != 200:
                logger.warning(f"車両データ取得エラー (HTTP {s_res.status_code})。10分待機します。")
                time.sleep(600)
                continue

            response_json = s_res.json().get("response")
            if response_json is None:
                continue

            # charge_state はキーが存在しつつ値が null のことがあるため、get のデフォルトでは防げない。
            charge_state = response_json.get("charge_state") or {}
            raw_amps = charge_state.get("charge_current_request")
            charging_status = str(charge_state.get("charging_state") or "")

            if raw_amps is None:
                raw_amps = MIN_AMPS

            # ユーザーがTeslaアプリや車内から充電を止めた場合、こちらがcharge_stopを送っていないのに
            # Charging → Stopped の遷移が観測される。これを「意図的な停止」として扱う。
            # （Complete への遷移は満充電なので手動停止ではない）
            externally_stopped: bool = (
                prev_charging_status == STATUS_CHARGING and charging_status == STATUS_STOPPED
            )
            prev_charging_status = charging_status

            if externally_stopped and manual_override:
                write_override(False)
                log_attention(
                    "手動での充電停止を検知したため、フル充電モードを自動解除しました。"
                    "太陽光追従モードに復帰します。"
                )
                below_min_count = 0
                time.sleep(180)
                continue

            if charging_status in TERMINAL_STATUSES:
                # 満充電・ケーブル未接続・給電なし。ここで charge_start を送っても状態は変わらない。
                # 以前は Disconnected 以外がこの分岐に入らず、満充電の車へ3分おきに開始命令を
                # 送り続けていた（2026-07-15 の 03:28〜07:39 で60回）。
                below_min_count = 0
                skip_wake_until = time.time() + TERMINAL_BACKOFF_SEC
                reason_label = TERMINAL_STATUS_LABELS.get(charging_status, charging_status)
                logger.info(
                    f"{reason_label}のため、充電コマンドの送信をスキップします。{TERMINAL_BACKOFF_SEC // 60}分待機します。"
                )
                time.sleep(TERMINAL_BACKOFF_SEC)
                continue

            if charging_status == STATUS_STARTING:
                # 充電開始処理の最中。コマンドを重ねると二重送信になるため次サイクルまで待つ。
                logger.info("充電の開始処理中です。次のサイクルまで待機します。")
                time.sleep(180)
                continue

            if charging_status not in (STATUS_CHARGING, STATUS_STOPPED):
                # 未知のステータスに対して開始命令を投げるのは危険なので、安全側に倒して待機する。
                below_min_count = 0
                skip_wake_until = time.time() + TERMINAL_BACKOFF_SEC
                logger.warning(
                    f"未知の充電ステータス『{charging_status or '(空)'}』を受信しました。"
                    f"安全のためコマンドを送信せず{TERMINAL_BACKOFF_SEC // 60}分待機します。"
                )
                time.sleep(TERMINAL_BACKOFF_SEC)
                continue

            skip_wake_until = 0.0

            if manual_override:
                if charging_status != STATUS_CHARGING:
                    target_amps = MAX_AMPS
                else:
                    # 車両側で手動調整された電流は尊重しつつ、停止閾値未満には落とさない
                    target_amps = max(raw_amps, MIN_AMPS)
                logger.info(f"演算状況（オーバーライド） → 目標: {target_amps}A (車両現在値: {raw_amps}A / ステータス: {charging_status})")
            else:
                calc_base_amps = raw_amps if charging_status == STATUS_CHARGING else 0
                adjustment_amps = int(-house_power / 200)
                target_amps = calc_base_amps + adjustment_amps
                logger.info(f"演算状況 → 目標: {target_amps}A (車両現在値: {raw_amps}A / ステータス: {charging_status})")

            if charging_status == STATUS_CHARGING:
                if target_amps < MIN_AMPS:
                    below_min_count += 1
                    if below_min_count < STOP_DEBOUNCE_CYCLES:
                        logger.info(f"余剰電力が{MIN_AMPS}A分（{MIN_AMPS * 200}W）を下回りました（{below_min_count}/{STOP_DEBOUNCE_CYCLES}回目）。瞬時的な変動の可能性があるため充電を継続します。")
                        if raw_amps > MIN_AMPS:
                            logger.info(f"買電を抑えるため電流を最小値へ調整: {raw_amps}A → {MIN_AMPS}A")
                            if set_charging_amps(vin, headers, MIN_AMPS):
                                logger.info(f"遠隔調整成功: {MIN_AMPS}A に固定されました。")
                    else:
                        logger.info(f"余剰電力が{MIN_AMPS}A分（{MIN_AMPS * 200}W）を{STOP_DEBOUNCE_CYCLES}サイクル連続で下回りました。充電を『一時停止』します。")
                        if send_charge_command(vin, headers, "charge_stop"):
                            logger.info("充電の停止を確認しました。")
                            below_min_count = 0
                            prev_charging_status = STATUS_STOPPED
                        else:
                            # 停止できていないのにカウンタを戻すと「停止したつもり」のまま
                            # 次の停止まで再びデバウンス分のサイクルを要してしまう。
                            logger.error("充電停止を確認できませんでした。次のサイクルで再試行します。")
                else:
                    below_min_count = 0
                    if target_amps > MAX_AMPS:
                        target_amps = MAX_AMPS
                    if target_amps != raw_amps:
                        logger.info(f"電流を変更調整: {raw_amps}A → {target_amps}A")
                        if set_charging_amps(vin, headers, target_amps):
                            logger.info(f"遠隔調整成功: {target_amps}A に固定されました。")
                    else:
                        logger.info(f"現在の {raw_amps}A のままでバランスが取れています。")
            else:
                below_min_count = 0
                if target_amps < START_AMPS:
                    logger.info(f"充電停止中。余剰電力が開始閾値（{START_AMPS}A分・{START_AMPS * 200}W）以上になるまで待機します。")
                else:
                    if target_amps > MAX_AMPS:
                        target_amps = MAX_AMPS
                    if manual_override:
                        logger.info(f"マニュアル・オーバーライドのため、充電を『再開』します（{target_amps}A）。")
                    else:
                        logger.info(f"開始閾値以上の余剰電力（{target_amps}A分）を検知！充電を『再開』します。")
                    # ここで prev_charging_status を Charging に先取りしないこと。
                    # 開始命令が result=true でも実際には充電が始まらなかった場合、次サイクルの
                    # Stopped を「ユーザーによる手動停止」と誤検知してオーバーライドを解除してしまう。
                    if send_charge_command(vin, headers, "charge_start"):
                        time.sleep(5)
                        if set_charging_amps(vin, headers, target_amps):
                            logger.info(f"遠隔調整成功: {target_amps}A に固定されました。")
                    else:
                        logger.error("充電の開始を確認できませんでした。次のサイクルで再試行します。")

        except Exception as e:
            logger.error(f"ループ内例外発生: {e}")

        time.sleep(180)

if __name__ == "__main__":
    main()