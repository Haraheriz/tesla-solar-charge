import requests
import json

# ==========================================
# 設定項目：ご自身のアクセストークンを貼り付けてください
# ==========================================
ACCESS_TOKEN = "***REMOVED_ACCESS_TOKEN***"

URL = "https://api.nature.global/1/appliances"
HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

def check_data():
    try:
        print("Nature Remo クラウドサーバーに接続中...")
        response = requests.get(URL, headers=HEADERS, timeout=10)
        response.raise_for_status()
        
        appliances = response.json()
        
        for app in appliances:
            if app.get("type") == "EL_SMART_METER":
                print(f"\n--- 機器発見: {app.get('nickname')} ---")
                print("中身のデータをそのまま表示します：")
                
                # スマートメーターのデータを綺麗に整形して丸ごと表示
                smart_meter_data = app.get("smart_meter", {})
                print(json.dumps(smart_meter_data, indent=2, ensure_ascii=False))
                return
                
        print("\nスマートメーターのデータが見つかりませんでした。")

    except Exception as e:
        print(f"エラーが発生しました: {e}")

if __name__ == "__main__":
    check_data()