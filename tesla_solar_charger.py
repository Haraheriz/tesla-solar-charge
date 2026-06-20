import os
import sys
import json
import time
import requests
import logging
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Optional, Dict, Any

# Windows環境での標準出力のエンコーディング問題を解決
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ==========================================
# ⚙️ 構造化ロギングの設定
# ==========================================
LOG_FILE: str = "tesla_solar_charger.log"
MAX_LOG_SIZE: int = 5 * 1024 * 1024  # 5MB
BACKUP_COUNT: int = 3

logger = logging.getLogger("SolarCharger")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# ==========================================
# ⚙️ 設定ファイルの自動読み込み ＆ 型ガード
# ==========================================
CONFIG_FILE: str = "tesla_config.json"
TOKEN_FILE: str = "tesla_tokens.json"

if not os.path.exists(CONFIG_FILE):
    logger.critical(f"設定ファイル（{CONFIG_FILE}）が見つかりません。")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config: Dict[str, Any] = json.load(f)

REMO_ACCESS_TOKEN: str = str(config.get("REMO_ACCESS_TOKEN", ""))
CLIENT_ID: str = str(config.get("CLIENT_ID", ""))
CLIENT_SECRET: str = str(config.get("CLIENT_SECRET", ""))
DOMAIN: str = str(config.get("DOMAIN", "localhost:8000"))

MIN_AMPS: int = int(config.get("MIN_AMPS", 3))
MAX_AMPS: int = int(config.get("MAX_AMPS", 48))

# 動作確認用：コマンドライン引数 --force-run または環境変数 FORCE_RUN=1 で
# 夜間休止モード（7:00-18:00以外は停止）を無視して常時稼働させる
FORCE_RUN: bool = "--force-run" in sys.argv or os.environ.get("FORCE_RUN") == "1"

# 動作確認用：環境変数 FORCE_HOUSE_POWER（W）を設定すると、Nature Remoの実測値を
# 無視してこの値を使う（負の値＝売電中・余剰あり、正の値＝買電中）
_force_house_power_env: Optional[str] = os.environ.get("FORCE_HOUSE_POWER")
FORCE_HOUSE_POWER: Optional[int] = int(_force_house_power_env) if _force_house_power_env is not None else None

AUTH_URL: str = "https://auth.tesla.com/oauth2/v3/token"
PROXY_HOST: str = "https://localhost:4443"

received_code: Optional[str] = None
refresh_token: Optional[str] = None
access_token: Optional[str] = None
token_expires_at: float = 0.0


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """手元PCでの完全自動着地用ローカルWEBサーバー"""
    def do_GET(self) -> None:
        global received_code
        if self.path.startswith("/callback"):
            query: Dict[str, list] = parse_qs(urlparse(self.path).query)
            if "code" in query:
                received_code = query["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("<h2>🎉 ノンコピペ認証に完全成功しました！</h2><p>この画面を閉じて、PowerShell側を確認してください。</p>".encode("utf-8"))
                return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format % args)


def get_remo_power() -> Optional[int]:
    url: str = "https://api.nature.global/1/appliances"
    headers: Dict[str, str] = {"Authorization": f"Bearer {REMO_ACCESS_TOKEN}"}
    try:
        response = requests.get(url, headers=headers, timeout=10)
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

def save_tokens(acc: str, ref: Optional[str], exp_in: int) -> None:
    global token_expires_at
    token_expires_at = time.time() + int(exp_in) - 1800
    token_data: Dict[str, Any] = {
        "access_token": acc, "refresh_token": ref, "token_expires_at": token_expires_at
    }
    tmp_file: str = TOKEN_FILE + ".tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
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
    global access_token, refresh_token
    if not refresh_token:
        logger.error("リフレッシュトークンが存在しないため、自動リフレッシュをスキップします。")
        return False
    logger.info("アクセストークンを自動リフレッシュ中...")
    payload: Dict[str, str] = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token
    }
    try:
        res = requests.post(AUTH_URL, data=payload, timeout=10)
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
        return True
    except Exception as e:
        logger.error(f"トークンのリフレッシュに失敗しました: {e}")
        return False

def wake_up_vehicle(vin: str, headers: Dict[str, str]) -> bool:
    logger.info("車両へ起床命令（Wake Up）を送信します...")
    url: str = f"{PROXY_HOST}/api/1/vehicles/{vin}/wake_up"
    for i in range(5):
        try:
            res = requests.post(url, headers=headers, timeout=15, verify='cert.pem')
            if res.status_code == 200:
                state: str = res.json().get("response", {}).get("state", "")
                if state == "online":
                    logger.info("車両がオンラインになりました！データ同期のため60秒間待機します...")
                    time.sleep(60)
                    return True
        except Exception:
            pass
        logger.info(f"起床待機中 ({i+1}/5回)...")
        time.sleep(10)
    return False

def main() -> None:
    global received_code, access_token, refresh_token, token_expires_at
    
    logger.info("=========================================================================")
    logger.info(" 🔌 太陽光自動充電制御システム（トークン手元調達・密輸型決定版）起動")
    logger.info("=========================================================================")

    if load_tokens():
        logger.info("💾 有効なトークンファイルを発見。サイレントログインします。")
        if time.time() > token_expires_at:
            if not refresh_tesla_token():
                logger.error("トークンリフレッシュに失敗。再認証が必要です。")
                if os.path.exists(TOKEN_FILE): os.remove(TOKEN_FILE)
    
    # 🚨 初回生成（手元のWindows PC実行時のみここを通る）
    if not os.path.exists(TOKEN_FILE) or refresh_token is None:
        redirect_uri = f"http://{DOMAIN}/callback"
        login_url: str = f"https://auth.tesla.com/oauth2/v3/authorize?client_id={CLIENT_ID}&redirect_uri={redirect_uri}&response_type=code&scope=openid%20offline_access%20vehicle_device_data%20vehicle_charging_cmds&state=12345"
        
        logger.warning("⚠️ 初回認証手続きを開始します。ブラウザが自動起動しない場合は以下を開いてください：")
        logger.warning(f"\n{login_url}\n")
        
        # Windows環境ならブラウザを自動で叩き起こす
        try:
            import webbrowser
            webbrowser.open(login_url)
        except Exception:
            pass
            
        # 同じPC内のブラウザからの自動着地を待ち伏せる
        httpd = HTTPServer(('127.0.0.1', 8000), OAuthCallbackHandler)
        while received_code is None:
            httpd.handle_request()
            
        logger.info("🎯 着地を検知！ 初回アクセストークンを取得中...")
        token_payload: Dict[str, str] = {
            "grant_type": "authorization_code", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "code": received_code, "redirect_uri": redirect_uri
        }
        res = requests.post(AUTH_URL, data=token_payload, timeout=10)
        res.raise_for_status()
        
        token_data: Dict[str, Any] = res.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        save_tokens(
            str(access_token) if access_token else "",
            refresh_token,
            int(token_data.get("expires_in", 28800))
        )
        logger.info("🎯 手元でのトークン生成に完全成功！『tesla_tokens.json』が生成されました。")

    headers: Dict[str, str] = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "Accept": "application/json"}

    v_res = requests.get(f"{PROXY_HOST}/api/1/vehicles", headers=headers, timeout=10, verify='cert.pem')
    vehicles = v_res.json().get("response", [])
    if not vehicles:
        logger.critical("車両リストを取得できませんでした。終了します。")
        sys.exit(1)
        
    vin: str = vehicles[0].get("vin", "")
    logger.info(f"🚗 対象車両 (VIN: {vin}) を捕捉。常駐ループ稼働を開始します。")
    print("-------------------------------------------------------------------------")

    if FORCE_RUN:
        logger.warning("⚠️ FORCE_RUNモード：夜間休止モードを無視して常時稼働します（動作確認専用）。")
    if FORCE_HOUSE_POWER is not None:
        logger.warning(f"⚠️ FORCE_HOUSE_POWERモード：Nature Remoの実測値を無視し、house_power={FORCE_HOUSE_POWER}Wとして計算します（動作確認専用）。")

    while True:
        now = time.localtime()
        if not FORCE_RUN and not (7 <= now.tm_hour < 18):
            logger.info("--- 定期チェック開始 ---")
            logger.info(f"🌙 夜間休止モード中（現在時刻 {time.strftime('%H:%M:%S')}）")
            logger.info("⏳ 次の稼働チェックまで10分間スリープします...")
            time.sleep(600)
            continue

        if time.time() > token_expires_at:
            if not refresh_tesla_token():
                time.sleep(180)
                continue
            headers["Authorization"] = f"Bearer {access_token}"

        logger.info("--- 定期チェック開始 ---")
        if FORCE_HOUSE_POWER is not None:
            house_power: Optional[int] = FORCE_HOUSE_POWER
        else:
            house_power = get_remo_power()
            if house_power is None:
                time.sleep(180)
                continue
            
        try:
            v_res = requests.get(f"{PROXY_HOST}/api/1/vehicles", headers=headers, timeout=10, verify='cert.pem')
            if v_res.status_code != 200:
                logger.warning(f"車両リスト取得エラー (HTTP {v_res.status_code})。10分待機します。")
                time.sleep(600)
                continue
                
            vehicles = v_res.json().get("response", [])
            if not vehicles:
                time.sleep(180)
                continue
                
            vin = vehicles[0].get("vin", "")
            vehicle_state: str = vehicles[0].get("state", "")
            logger.info(f"車両状態: 『{vehicle_state}』 (RemoE瞬時電力: {house_power} W)")

            if vehicle_state in ["asleep", "offline"]:
                if house_power >= -(MIN_AMPS * 200):
                    logger.info(f"車両は就寝中、かつ余剰が{MIN_AMPS * 200}W未満のため、このまま寝かせます。")
                    time.sleep(180)
                    continue
                else:
                    logger.info(f"十分な余剰電力（{MIN_AMPS * 200}W以上）を検知したため、車両を叩き起こします。")
                    if not wake_up_vehicle(vin, headers):
                        time.sleep(180)
                        continue

            state_url: str = f"{PROXY_HOST}/api/1/vehicles/{vin}/vehicle_data?endpoints=charge_state"
            s_res = requests.get(state_url, headers=headers, timeout=10, verify='cert.pem')
            
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
                
            charge_state = response_json.get("charge_state", {})
            raw_amps = charge_state.get("charge_current_request")
            charging_status = str(charge_state.get("charging_state", ""))
            
            if raw_amps is None:
                raw_amps = MIN_AMPS
            
            calc_base_amps = raw_amps if charging_status == "Charging" else 0
            adjustment_amps = int(-house_power / 200)
            target_amps = calc_base_amps + adjustment_amps
            
            logger.info(f"📋 演算状況 ➔ 目標: {target_amps}A (車両現在値: {raw_amps}A / ステータス: {charging_status})")

            if target_amps < MIN_AMPS:
                if charging_status == "Charging":
                    logger.info(f"📉 余剰電力が{MIN_AMPS}A分（{MIN_AMPS * 200}W）を下回りました。充電を『一時停止』します。")
                    requests.post(f"{PROXY_HOST}/api/1/vehicles/{vin}/command/charge_stop", headers=headers, timeout=15, verify='cert.pem')
                else:
                    logger.info(f"😴 充電停止中。余剰電力が{MIN_AMPS * 200}W以上回復するまで待機します。")
            else:
                if target_amps > MAX_AMPS:
                    target_amps = MAX_AMPS
                
                if charging_status != "Charging":
                    logger.info(f"☀️ 余剰電力（{target_amps}A分）を検知！充電を『再開』します。")
                    requests.post(f"{PROXY_HOST}/api/1/vehicles/{vin}/command/charge_start", headers=headers, timeout=15, verify='cert.pem')
                    time.sleep(5)
                    raw_amps = MIN_AMPS
                
                if target_amps != raw_amps or charging_status != "Charging":
                    logger.info(f"🚀 電流を変更調整: {raw_amps}A ➔ {target_amps}A")
                    cmd_url = f"{PROXY_HOST}/api/1/vehicles/{vin}/command/set_charging_amps"
                    cmd_res = requests.post(cmd_url, headers=headers, json={"charging_amps": target_amps}, timeout=15, verify='cert.pem')
                    if cmd_res.json().get("response", {}).get("result") is True:
                        logger.info(f"🎯 遠隔調整成功: {target_amps}A に固定されました。")
                else:
                    logger.info(f"✅ 現在の {raw_amps}A のままでバランスが取れています。")

        except Exception as e:
            logger.error(f"ループ内例外発生: {e}")
            
        time.sleep(180)

if __name__ == "__main__":
    main()