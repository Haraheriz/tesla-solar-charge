import os
import requests
from urllib.parse import urlparse, parse_qs
from _env import load_env

load_env()

# ==========================================
# 設定項目：環境変数 TESLA_CLIENT_ID / TESLA_CLIENT_SECRET から読み込み
# ==========================================
CLIENT_ID = os.environ["TESLA_CLIENT_ID"]
CLIENT_SECRET = os.environ["TESLA_CLIENT_SECRET"]

# ブラウザのアドレスバーからコピーした「http://localhost:8000/callback?code=...」のURLをそのまま貼り付け
REDIRECTED_URL = os.environ.get("TESLA_REDIRECTED_URL", "http://localhost:8000/callback?code=PASTE_CODE_HERE&issuer=https%3A%2F%2Fauth.tesla.com%2Foauth2%2Fv3&state=12345")

# ------------------------------------------
# 1. URLから認可コード（code）を自動抽出
# ------------------------------------------
try:
    parsed_url = urlparse(REDIRECTED_URL)
    code = parse_qs(parsed_url.query)["code"][0]
except Exception:
    print("エラー: URLから認証コード（code）を読み取れませんでした。貼り付けたURLを確認してください。")
    exit()

# テスラAPIのエンドポイント設定
AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"
API_HOST = "https://fleet-api.prd.na.vn.cloud.tesla.com" # グローバル（日本含む）エンドポイント

def main():
    # ------------------------------------------
    # 2. 引換券（code）を本物の「アクセストークン」に交換する
    # ------------------------------------------
    print("テスラサーバーにアクセストークンを要求中...")
    
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": "http://localhost:8000/callback"
    }
    
    try:
        response = requests.post(AUTH_URL, data=token_payload, timeout=10)
        response.raise_for_status()
        token_data = response.json()
        
        # 本物の合鍵（アクセストークン）をゲット
        access_token = token_data.get("access_token")
        print("アクセストークンの取得に成功しました！")
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
        
        # ------------------------------------------
        # 3. あなたのアカウントに紐づくテスラ車の一覧を取得
        # ------------------------------------------
        print("\n車両リストを取得しています...")
        vehicles_url = f"{API_HOST}/api/1/vehicles"
        
        v_response = requests.get(vehicles_url, headers=headers, timeout=10)
        v_response.raise_for_status()
        vehicles = v_response.json().get("response", [])
        
        if not vehicles:
            print("アカウントに紐づく車両が見つかりませんでした。")
            return
            
        # 1台目の車両情報を取得
        vehicle = vehicles[0]
        vehicle_id = vehicle.get("id_str")
        display_name = vehicle.get("display_name")
        state = vehicle.get("state")
        
        print(f"--- 車両発見: {display_name} (状態: {state}) ---")
        
        # 車が眠っている（asleep）場合は、詳細データが取れないため判定
        if state != "online":
            print("車が現在スリープ状態（仮眠中）です。")
            print("バッテリー残量を取得するには、スマホのテスラアプリを開くなどして車を起こしてから、再度このプログラムを実行してください。")
            return
            
        # ------------------------------------------
        # 4. 車両のライブデータ（バッテリー残量）を取得
        # ------------------------------------------
        print("車両の詳細データを取得中...")
        data_url = f"{API_HOST}/api/1/vehicles/{vehicle_id}/vehicle_data"
        
        # 必要なデータカテゴリを指定して通信量を節約
        params = {"endpoints": "charge_state"}
        
        d_response = requests.get(data_url, headers=headers, params=params, timeout=10)
        d_response.raise_for_status()
        
        v_data = d_response.json().get("response", {})
        charge_state = v_data.get("charge_state", {})
        
        # バッテリー残量（%）と、現在の充電設定アンペア数を取得
        battery_level = charge_state.get("battery_level")
        charge_current_request = charge_state.get("charge_current_request")
        
        print("\n========================================")
        print(f" 🔋 バッテリー残量: {battery_level} %")
        print(f" ⚡ 現在の充電設定: {charge_current_request} A")
        print("========================================")
        print("テスラAPIの読み込みテスト、大成功です！")

    except requests.exceptions.HTTPError as e:
        print(f"\nエラーが発生しました。引換券（URL）の期限が切れた可能性があります。")
        print(f"詳細: {e.response.text}")
    except Exception as e:
        print(f"\n予期せぬエラーが発生しました: {e}")

if __name__ == "__main__":
    main()