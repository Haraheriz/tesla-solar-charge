import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import requests

# ==========================================
# 設定項目：ご自身の情報を貼り付けてください
# ==========================================
CLIENT_ID = "***REMOVED_CLIENT_ID***"
CLIENT_SECRET = "***REMOVED_CLIENT_SECRET***"
DOMAIN = "haraheriz.github.io"

AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"
API_HOST = "https://localhost:4443"

received_code = None

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global received_code
        if self.path.startswith("/callback"):
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                received_code = query["code"][0]
                self.send_response(200)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("""
                <html>
                <body style="font-family: sans-serif; text-align: center; padding-top: 50px;">
                    <h2 style="color: #2e7d32;">🔓 認証成功！車へコマンドを送信します...</h2>
                    <p>PowerShell画面に戻って結果を確認してください。</p>
                </body>
                </html>
                """.encode("utf-8"))
                return
        self.send_response(404)
        self.end_headers()

def main():
    global received_code
    
    # 認証用URL（今回はトンネルなしなので、リダイレクト先はlocalhostのままで大丈夫です）
    login_url = f"https://auth.tesla.com/oauth2/v3/authorize?client_id={CLIENT_ID}&redirect_uri=http://localhost:8000/callback&response_type=code&scope=openid%20offline_access%20vehicle_device_data%20vehicle_charging_cmds&state=12345"
    
    print("=========================================================================")
    print(" ① 以下のURLをコピーして、ブラウザの『新しいタブ』で開いて承認してください：")
    print("=========================================================================")
    print(f"\n{login_url}\n")
    print("=========================================================================")
    
    server_address = ('', 8000)
    httpd = HTTPServer(server_address, OAuthCallbackHandler)
    
    print("⏳ ブラウザからのログイン完了を待っています...")
    while received_code is None:
        httpd.handle_request()
        
    print("\n⚡ 認証コードをキャッチしました。トークンを交換します...")
    
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": received_code,
        "redirect_uri": "http://localhost:8000/callback"
    }
    
    try:
        response = requests.post(AUTH_URL, data=token_payload, timeout=10)
        response.raise_for_status()
        access_token = response.json().get("access_token")
        
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # 1. 車両のVINコードを取得
        print("車両リストを取得しています...")
        v_response = requests.get(f"{API_HOST}/api/1/vehicles", headers=headers, timeout=10, verify='cert.pem')
        v_response.raise_for_status()
        vehicles = v_response.json().get("response", [])
        
        if not vehicles:
            print("車両が見つかりませんでした。")
            return
            
        vin = vehicles[0].get("vin")
        display_name = vehicles[0].get("display_name")
        print(f"➔ 発見した車両: {display_name} (VIN: {vin})")
        
        # 2. 【本番】充電電流を 6A に書き換えるコマンドを送信！
        print("\n🚀 運命の瞬間：充電電流を『6A』に変更する命令を送信中...")
        
        command_url = f"{API_HOST}/api/1/vehicles/{vin}/command/set_charging_amps"
        command_payload = {
            "charging_amps": 6
        }
        
        cmd_response = requests.post(command_url, headers=headers, json=command_payload, timeout=15, verify='cert.pem')
        cmd_response.raise_for_status()
        
        result_data = cmd_response.json()
        print("\n========================================")
        print(" 📬 テスラサーバーからの応答データ：")
        print(result_data)
        print("========================================")
        
        if result_data.get("response", {}).get("result") is True:
            print("\n 🎉🎉🎉 コマンド送信に完全成功しました！！！ 🎉🎉🎉")
            print("今この瞬間、Model3Xの充電設定が『6A』に遠隔で書き換わりました！")
            print("スマホのテスラアプリを開いて、本当に6Aに変わっているか確認してみてください！")
        else:
            print("\nコマンドは届きましたが、車側が拒否した可能性があります（スリープ中など）。")

    except Exception as e:
        print(f"\nエラーが発生しました: {e}")
        if 'cmd_response' in locals():
            print(f"詳細: {cmd_response.text}")

if __name__ == "__main__":
    main()