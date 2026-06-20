import os
import requests
from _env import load_env

load_env()

# ==========================================
# 設定項目：環境変数 TESLA_CLIENT_ID / TESLA_CLIENT_SECRET / TESLA_DOMAIN から読み込み
# ==========================================
CLIENT_ID = os.environ["TESLA_CLIENT_ID"]
CLIENT_SECRET = os.environ["TESLA_CLIENT_SECRET"]
DOMAIN = os.environ["TESLA_DOMAIN"]

AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"
API_HOST = "https://fleet-api.prd.na.vn.cloud.tesla.com" # 日本の車両が属するリージョン

def activate_app():
    # ------------------------------------------
    # 1. パートナートークン（アプリ自体の身分証）の取得
    # ------------------------------------------
    print("① アプリ自身のアクティベート用トークン（パートナートークン）を要求中...")

    token_payload = {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "scope": "openid vehicle_device_data vehicle_charging_cmds"
    }

    try:
        response = requests.post(AUTH_URL, data=token_payload, timeout=10)
        response.raise_for_status()
        partner_token = response.json().get("access_token")
        print("→ パートナートークンの取得に成功しました！")

        # ------------------------------------------
        # 2. テスラ地域サーバーへのアクティベート（登録）申請
        # ------------------------------------------
        print("\n② テスラの地域サーバー（アジア・北米）にアプリの有効化を申請中...")

        headers = {
            "Authorization": f"Bearer {partner_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # アプリ作成時に指定したリダイレクトURLのドメイン（今回はlocalhost）を申請します
        register_url = f"{API_HOST}/api/1/partner_accounts"
        register_payload = {
            "domain": DOMAIN
        }

        reg_response = requests.post(register_url, headers=headers, json=register_payload, timeout=10)

        if reg_response.status_code in [200, 201]:
            print("\n========================================================")
            print("アプリのアクティベートに成功しました！")
            print("========================================================")
            print("テスラの地域サーバーにアプリが正式に登録されました。")
            print("これでこのアプリの認証情報を使って車両データへアクセスできるようになります。")
        else:
            print(f"\n登録が拒否されました（ステータスコード: {reg_response.status_code}）")
            print(f"詳細: {reg_response.text}")

    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        if 'reg_response' in locals():
            print(f"サーバーからの返答: {reg_response.text}")

if __name__ == "__main__":
    activate_app()