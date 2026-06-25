import os
import sys
import secrets
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests
from _env import load_env

load_env()

# ==========================================
# 設定項目：環境変数 TESLA_CLIENT_ID / TESLA_CLIENT_SECRET / TESLA_DOMAIN から読み込み
# ==========================================
CLIENT_ID = os.environ["TESLA_CLIENT_ID"]
CLIENT_SECRET = os.environ["TESLA_CLIENT_SECRET"]
REDIRECT_URI = f"https://{os.environ['TESLA_DOMAIN']}/callback"

# テスラAPIのエンドポイント
AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"
API_HOST = "https://fleet-api.prd.na.vn.cloud.tesla.com"

# ブラウザから受け取ったコードを一時保存する変数
received_code = None
expected_oauth_state = secrets.token_urlsafe(32)

cloud_session = requests.Session()
cloud_session.verify = True

# ------------------------------------------
# ローカルWEBサーバーの受付窓口ロジック
# ------------------------------------------
class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global received_code
        # テスラからのリダイレクト（/callback）をキャッチ
        if self.path.startswith("/callback"):
            query = parse_qs(urlparse(self.path).query)
            if "code" in query and query.get("state", [None])[0] == expected_oauth_state:
                received_code = query["code"][0]

                # ブラウザ側に成功画面を表示
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("""
                <html>
                <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                    <h2 style="color: #2e7d32;">テスラアカウントの認証に成功しました！</h2>
                    <p>プログラムが自動でデータを取得しています。このタブを閉じてPowerShell画面に戻ってください。</p>
                </body>
                </html>
                """.encode("utf-8"))
                return

        # それ以外の不要なアクセス（faviconなど）は404を返す
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # 画面を綺麗に保つためログ出力を非表示にする
        return

def main():
    global received_code

    # 1. ログイン用URLの生成
    login_url = f"https://auth.tesla.com/oauth2/v3/authorize?client_id={CLIENT_ID}&redirect_uri={REDIRECT_URI}&response_type=code&scope=openid%20offline_access%20vehicle_device_data%20vehicle_charging_cmds&state={expected_oauth_state}"

    print("=========================================================================")
    print("① 以下のURLをコピーして、ブラウザの『新しいタブ』で開いてログインしてください：")
    print("=========================================================================")
    print(f"\n{login_url}\n")
    print("=========================================================================")

    # 2. ポート8000番で待ち伏せサーバーを起動
    server_address = ('', 8000)
    httpd = HTTPServer(server_address, OAuthCallbackHandler)

    print("ブラウザからのログイン完了を待っています...（PowerShellはこのまま待機）")

    # コードが取れるまでサーバーを回す
    while received_code is None:
        httpd.handle_request()

    print("\n【通信成功】ブラウザから認証コードを自動でキャッチしました！")

    # 3. キャッチしたコードを即座にアクセストークンに交換
    print("テスラサーバーにアクセストークンを要求中...")
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": received_code,
        "redirect_uri": REDIRECT_URI
    }

    try:
        response = cloud_session.post(AUTH_URL, data=token_payload, timeout=10)
        response.raise_for_status()
        token_data = response.json()

        access_token = token_data.get("access_token")
        print("アクセストークンの取得に成功しました！")

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }

        # 4. 車両リストの取得
        print("\n車両リストを取得しています...")
        vehicles_url = f"{API_HOST}/api/1/vehicles"
        v_response = cloud_session.get(vehicles_url, headers=headers, timeout=10)
        v_response.raise_for_status()
        vehicles = v_response.json().get("response", [])

        if not vehicles:
            print("アカウントに紐づく車両が見つかりませんでした。")
            return

        vehicle = vehicles[0]
        vehicle_id = vehicle.get("vin")
        display_name = vehicle.get("display_name")
        state = vehicle.get("state")

        print(f"--- 車両発見: {display_name} (状態: {state}) ---")

        if state != "online":
            print("車が現在スリープ状態（仮眠中）です。")
            print("スマホのテスラアプリを開くなどして車を起こしてから、再度このプログラムを実行してください。")
            return

        # 5. バッテリー残量の取得
        print("車両の詳細データを取得中...")
        data_url = f"{API_HOST}/api/1/vehicles/{vehicle_id}/vehicle_data"
        params = {"endpoints": "charge_state"}

        d_response = cloud_session.get(data_url, headers=headers, params=params, timeout=10)
        d_response.raise_for_status()

        v_data = d_response.json().get("response", {})
        charge_state = v_data.get("charge_state", {})

        battery_level = charge_state.get("battery_level")
        charge_current_request = charge_state.get("charge_current_request")

        print("\n========================================")
        print(f" バッテリー残量: {battery_level} %")
        print(f" 現在の充電設定: {charge_current_request} A")
        print("========================================")
        print("自動認証テストが完了しました。")

    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        if 'response' in locals() and response.text:
            print(f"詳細: {response.text}")

if __name__ == "__main__":
    main()