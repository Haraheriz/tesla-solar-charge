import requests
import json

# ==========================================
# 設定項目：ご自宅のNature Remo EのIPアドレスに書き換えてください
# ==========================================
REMO_IP = "192.168.8.143" 

# ローカルAPIのURL
URL = f"http://{REMO_IP}/local_api/broute"

# Nature Remo EのローカルAPIを叩く際は、このヘッダーが必須です
HEADERS = {
    "X-Requested-With": "local_api"
}

def get_power_data():
    try:
        print(f"Nature Remo E ({REMO_IP}) に接続中...")
        # タイムアウトを5秒に設定してリクエストを送信
        response = requests.get(URL, headers=HEADERS, timeout=5)
        
        # ステータスコードが200（成功）かチェック
        response.raise_for_status()
        
        # 取得したデータをJSONとして解析
        data = response.json()
        
        # 瞬時電力計測値（W）を取得
        instantaneous_w = data.get("measured_instantaneous")
        
        print("\n--- 取得成功！ ---")
        if instantaneous_w is not None:
            if instantaneous_w > 0:
                print(f"【買電中】現在、電力会社から {instantaneous_w} W を買っています。")
                print("太陽光の余剰はありません。")
            elif instantaneous_w < 0:
                # マイナスの値をプラスに変換して表示
                surplus_w = abs(instantaneous_w)
                print(f"【売電中（余剰あり）】現在 {surplus_w} W の余剰電力が発生しています！")
                print(f"テスラに約 {round(surplus_w / 200, 1)} A の電流を割り当て可能です。")
            else:
                print("【均衡状態】消費電力と発電電力がちょうど同じ（0 W）です。")
        else:
            print("データ内に 'measured_instantaneous' が見つかりませんでした。")
            print(f"受信データ: {data}")

    except requests.exceptions.Timeout:
        print("エラー: タイムアウトしました。IPアドレスが正しいか、または同じWi-Fiに繋がっているか確認してください。")
    except requests.exceptions.ConnectionError:
        print("エラー: Nature Remo E に接続できませんでした。ネットワーク環境を確認してください。")
    except Exception as e:
        print(f"予期せぬエラーが発生しました: {e}")

if __name__ == "__main__":
    get_power_data()