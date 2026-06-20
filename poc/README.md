# 🧪 poc/ — 実証・検証スクリプト

このフォルダは、本体プログラム（[`tesla_solar_charger.py`](../tesla_solar_charger.py)）を完成させるまでに作成した、Tesla Fleet APIおよびNature Remo APIの認証・通信フローを個別に検証するための実験用スクリプト集です。

本番運用には使いませんが、Tesla/Nature Remo連携を自分で組みたい方の参考用ひな形として残しています。

## セットアップ（共通）

各スクリプトは環境変数から認証情報・個人設定を読み込みます。一括設定するには、このフォルダに `.env` ファイルを作成してください。

```powershell
copy poc\.env.example poc\.env
```

`poc/.env` を開き、自分の値に書き換えてください（このファイルは `.gitignore` 対象なので、誤ってコミットされる心配はありません）。

```env
TESLA_CLIENT_ID=your-tesla-client-id
TESLA_CLIENT_SECRET=your-tesla-client-secret
TESLA_DOMAIN=your-username.github.io
REMO_ACCESS_TOKEN=your-nature-remo-access-token
REMO_LOCAL_IP=192.168.x.x
```

各スクリプトは起動時に `poc/.env` を自動で読み込みます（`_env.py`）。手動で `$env:` をセットする必要はありません。

## スクリプト一覧

| ファイル | 用途 |
|---|---|
| `make_certs.py` | ローカルHTTPSプロキシ用のTLS証明書（`cert.pem`/`key.pem`）を生成 |
| `activate_tesla_app.py` | Teslaアプリの「パートナーアカウント登録」を実行（既存の鍵ペアを使う場合） |
| `complete_tesla_activation.py` | 鍵ペア生成からパートナーアカウント登録までを一括実行 |
| `test_tesla_auto.py` | Fleet API（公式クラウド）に対するOAuth認証〜車両データ取得のテスト |
| `test_tesla_battery.py` | ブラウザでの認証後、コールバックURLを手動貼り付けして車両データを取得するテスト |
| `test_tesla_command.py` | ローカルプロキシ経由で充電電流変更コマンドを送信するテスト |
| `check_remo_data.py` / `test_remo_cloud.py` | Nature Remo Cloud APIから電力データを取得するテスト |
| `test_remo_e.py` | Nature Remo Eのローカル通信APIを直接叩くテスト（同一Wi-Fi内のみ） |

## 注意

- `TESLA_CLIENT_SECRET` や `REMO_ACCESS_TOKEN` は機密情報です。`.env` 以外の場所（コード中など）に直接書き込まないでください。
- `complete_tesla_activation.py` 等が生成する `private-key.pem`／`public-key.pem`／`cert.pem`／`key.pem` も `.gitignore` 対象です。
