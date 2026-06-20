import os
import requests

# ==========================================
# 設定項目：環境変数 REMO_ACCESS_TOKEN から読み込み
# ==========================================
ACCESS_TOKEN = os.environ["REMO_ACCESS_TOKEN"]

URL = "https://api.nature.global/1/appliances"
HEADERS = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Accept": "application/json"
}

def get_cloud_power_data():
    try:
        print("Nature Remo クラウドサーバーに接続中...")
        response = requests.get(URL, headers=HEADERS, timeout=10)
        response.raise_for_status()
        
        appliances = response.json()
        remo_e_found = False
        
        for app in appliances:
            if app.get("type") == "EL_SMART_METER":
                remo_e_found = True
                print(f"\n--- 機器発見: {app.get('nickname')} ---")
                
                properties = app.get("smart_meter", {}).get("echonetlite_properties", [])
                
                for prop in properties:
                    # 【修正ポイント】16進数の 'e7' ではなく、10進数の 231 で判定します
                    if prop.get("epc") == 231:
                        # 取得した文字列（例: "602"）を数値に変換
                        instantaneous_w = int(prop.get("val"))
                        
                        print("----------------------------------------")
                        if instantaneous_w > 0:
                            print(f"【買電中】現在、電力会社から {instantaneous_w} W を買っています。")
                            print("太陽光の余剰はありません。")
                        elif instantaneous_w < 0:
                            surplus_w = abs(instantaneous_w)
                            print(f"【売電中（余剰あり）】現在 {surplus_w} W の余剰電力が発生しています！")
                            print(f"テスラに約 {round(surplus_w / 200, 1)} A の電流を割り当て可能です。")
                        else:
                            print("【均衡状態】消費電力と発電電力がちょうど同じ（0 W）です。")
                        print("----------------------------------------")
                        break
        
        if not remo_e_found:
            print("\nアカウントに紐づくNature Remo Eが見つかりませんでした。")

    except Exception as e:
        print(f"エラーが発生しました: {e}")

if __name__ == "__main__":
    get_cloud_power_data()