import os
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests

# cryptographyライブラリから必要なモジュールをインポート
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

# ==========================================
# 設定項目：ご自身の情報を貼り付けてください
# ==========================================
CLIENT_ID = "***REMOVED_CLIENT_ID***"
CLIENT_SECRET = "***REMOVED_CLIENT_SECRET***"
DOMAIN = "haraheriz.github.io"

AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"
API_HOST = "https://fleet-api.prd.na.vn.cloud.tesla.com"

# ------------------------------------------
# 1. テスラ仕様の鍵ペア（P-256 ECC鍵）の自動生成
# ------------------------------------------
private_key_file = "private-key.pem"
public_key_file = "public-key.pem"

print("🔑 鍵ペアの確認中...")
if not os.path.exists(private_key_file):
    print("➔ テスラ仕様のEC鍵ペア（P-256）を新規生成します...")
    # テスラ車が要求する NIST P-256 (secp256r1) 曲線で鍵を生成
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()
    
    # 秘密鍵の保存（これは絶対に他人に教えてはいけません）
    with open(private_key_file, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    # 公開鍵の保存（これをテスラに配ります）
    with open(public_key_file, "wb") as f:
        f.write(public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ))
    print("➔ 鍵ペアの生成が完了しました。")
else:
    print("➔ 既存の鍵ペアが見つかりました。これを使用します。")

# 生成した公開鍵のテキストを読み込む
with open(public_key_file, "r") as f:
    PUBLIC_KEY_PEM = f.read()


# ------------------------------------------
# 2. テスラからの「公開鍵ちょうだい」アクセスを待ち受ける窓口（WEBサーバー）
# ------------------------------------------
class TeslaKeyDeliveryHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # テスラが指定してくる特殊な通り道（パス）を判定
        if self.path == "/.well-known/appspecific/com.tesla.3p.public-key.pem":
            self.send_response(200)
            self.send_header("Content-Type", "application/x-pem-file")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # 公開鍵をテスラサーバーに手渡す
            self.wfile.write(PUBLIC_KEY_PEM.encode("utf-8"))
            print("📡 【通信感知】テスラサーバーがあなたのパソコンから公開鍵をダウンロードしました！")
            return
            
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        return # 画面を綺麗にするためログは非表示


def start_web_server():
    server_address = ('localhost', 8000)
    httpd = HTTPServer(server_address, TeslaKeyDeliveryHandler)
    print("⏳ ローカル窓口（ポート8000番）を開放しました。テスラからの接続を待機中...")
    # テスラが取りに来るまでサーバーを維持（30秒間）
    end_time = time.time() + 30
    while time.time() < end_time:
        httpd.handle_request()


# ------------------------------------------
# 3. 窓口が開いている間に、テスラへ登録申請を送りつける処理
# ------------------------------------------
def send_activation_request():
    print("① アプリ自身のアクティベート用トークンを要求中...")
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
        
        print("② 窓口が開いたのを見計らって、テスラサーバーに最終登録を申請します...")
        time.sleep(3) # WEBサーバーが完全に立ち上がるまで少し待つ
        
        headers = {
            "Authorization": f"Bearer {partner_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        register_url = f"{API_HOST}/api/1/partner_accounts"
        register_payload = {"domain": DOMAIN}
        
        reg_response = requests.post(register_url, headers=headers, json=register_payload, timeout=15)
        
        print("\n========================================================")
        if reg_response.status_code in [200, 201]:
            print(" 🎉🎉🎉 アプリのアクティベートに完全成功しました！！！ 🎉🎉🎉")
            print("========================================================")
            print("テスラAPIのすべてのセキュリティガードを完全突破しました。")
            print("これで地﨑様のアプリは、テスラ公式に認められた正規アプリになりました！")
        else:
            print(f" 登録拒否（ステータスコード: {reg_response.status_code}）")
            print(f"詳細: {reg_response.text}")
        print("========================================================\n")

    except Exception as e:
        print(f"エラーが発生しました: {e}")


if __name__ == "__main__":
    # WEBサーバーを裏側のスレッドで起動
    server_thread = threading.Thread(target=start_web_server)
    server_thread.daemon = True
    server_thread.start()
    
    # 表側のメイン処理でアクティベート申請を送信
    send_activation_request()
    
    # サーバーの終了を少し待つ
    time.sleep(2)