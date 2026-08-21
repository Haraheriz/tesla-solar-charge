# 2. 再構築・デプロイ手順書（再現性のためのマスターコマンド集）

## 1. 前提条件とディレクトリ構造

ハードウェア障害（SDカード突然死など）やOSアップデートに伴い、システムをゼロから再構築する際の手順である。本手順は、ターゲット環境が **Linux（Raspberry Pi OS 64bit等）** であり、一般的な非特権ユーザー権限で実行されることを想定している。

```Markdown
### 最終的な配置ディレクトリ構造（Linux側）
※ `venv/` 以外の全資材をWindowsから転送した後、Step 2のコマンドによってLinux側で `venv/` を自動生成させる。

/home/<username>/tesla-solar-charge/
├── cert.pem                   # [転送] TLS通信用 公開鍵証明書
├── key.pem                    # [転送] TLS通信用 秘密鍵（RSA形式）
├── tesla_app_key.pem          # [転送] テスラ車両コマンド用 秘密鍵（EC形式）
├── tesla_config.json          # [転送] システム設定ファイル
├── tesla_tokens.json          # [転送] テスラAPIリフレッシュトークン
├── tesla-http-proxy           # [転送] Go言語ネイティブバイナリ
├── tesla_solar_charger.py     # [転送] 充電制御メインスクリプト
├── control_server.py          # [転送] スマホ操作用コントロールサーバー
├── override_state.py          # [転送] マニュアル・オーバーライド状態の共有モジュール
├── wall_connector.py          # [転送] 自宅ウォールコネクターのローカルAPI読み取りモジュール
├── icons/                     # [転送] PWA用アプリアイコン（icon-192.png, icon-512.png）
└── venv/                      # [Linux側で生成] Python3 仮想環境（相対パスでの運用不可）

> **ファイル名の注意（Windows側 ↔ ラズパイ側の不一致）：**
> Windows側の開発・検証スクリプト（`poc/complete_tesla_activation.py`等）は、テスラ車両コマンド用の鍵ペアを **`private-key.pem` / `public-key.pem`** という名前で生成する。一方、ラズパイ側の `systemd` サービス定義（`tesla-proxy.service`）は **`tesla_app_key.pem`** という名前を前提にしている。
> 転送時は鍵の中身（バイト列）は同一のまま、**ファイル名だけ `tesla_app_key.pem` にリネームしてから配置すること**。リネームせずに別名のまま使う場合は、`/etc/systemd/system/tesla-proxy.service` 内の `-key-file` のパスも合わせて書き換える必要がある。

---

## 2. 【Step 1】開発環境でのクロスコンパイル（Windows側作業）

テスラ公式プロキシは、ラズパイOS（Linux ARM）上で直接ビルドするよりも、手元のWindows環境（PowerShell）からクロスコンパイルして実行バイナリを生成する方が効率的である。

PowerShellを開き、プロキシのGoソースコードが存在するディレクトリへ移動して以下のコマンドを実行する。

```powershell
# 64bit版 Linux ARM向けにターゲットを指定してビルド
$env:GOOS="linux"
$env:GOARCH="arm64"
go build -o tesla-http-proxy

```

* ※生成された拡張子のない `tesla-http-proxy` を、SCPやSFTP等を用いてLinux側の `/home/<username>/tesla-solar-charge/` ディレクトリへ転送する。

---

## 3. 【Step 2】Python 仮想環境（venv）の構築（Linux側作業）

OS全体のシステム環境（グローバル）の破損を防ぐため、Debian 12以降の作法に則り、プロジェクト専用の隔離されたPython実行環境を構築する。

Linuxのコンソールで対象ディレクトリに移動し、以下の2行を実行する。

```bash
# 1. ディレクトリ内に「venv」という名前の独立した仮想環境を生成
python3 -m venv /home/<username>/tesla-solar-charge/venv

# 2. 仮想環境専用の pip を使用して、依存ライブラリ（requests）をピンポイントで導入
/home/<username>/tesla-solar-charge/venv/bin/pip install requests

```

---

## 4. 【Step 3】資産パーミッションの厳格化と一括修復

機密情報（テスラ車両を遠隔操作可能な生のリフレッシュトークンや暗号鍵）をマルチユーザー環境から保護し、且つ `systemd` からの実行権限を担保するための厳格な権限設定コマンド群である。

```bash
# 1. ディレクトリ配下すべての所有者を、実行ユーザーに統一
sudo chown -R <username>:<username> /home/<username>/tesla-solar-charge/

# 2. ディレクトリ本体を「所有者以外立ち入り禁止（700）」に設定
chmod 700 /home/<username>/tesla-solar-charge/

# 3. 内部の通常ファイルをすべて一旦「所有者のみ読み書き（600）」に制限
chmod 600 /home/<username>/tesla-solar-charge/*

# 4. Goプロキシバイナリに「実行権限（700）」を付与
chmod 700 /home/<username>/tesla-solar-charge/tesla-http-proxy

# 5. 【最重要】Python仮想環境（venv）の走査権限（x）および実行権限の完全修復
find /home/<username>/tesla-solar-charge/venv -type d -exec chmod 700 {} +
find /home/<username>/tesla-solar-charge/venv -type f -exec chmod 600 {} +
chmod 700 /home/<username>/tesla-solar-charge/venv/bin/*

```

---

## 5. 【Step 4】`tesla_config.json` の設定

`tesla_config.json` は `.gitignore` 対象のため、リポジトリを取得しただけでは存在しない。**`tesla_config.json.template` をコピーして作成する。**

```bash
cp tesla_config.json.template tesla_config.json
chmod 600 tesla_config.json
```

### 設定キー一覧

**サービスを起動する前に、この表を上から確認すること。**「必須」のキーが未設定だと起動しないか、意図した制御が行われない。

| キー | 必須 | 既定値 | 内容 |
| --- | --- | --- | --- |
| `CLIENT_ID` / `CLIENT_SECRET` | ○ | `""` | Tesla開発者ポータルで発行したアプリケーションの資格情報 |
| `DOMAIN` | ○ | `localhost:8000` | OAuthのリダイレクト先ドメイン |
| `REMO_ACCESS_TOKEN` | ○ | `""` | Nature Remo のアクセストークン（余剰電力の測定に使う） |
| **`WALL_CONNECTOR_HOST`** | **○** | `""` | **自宅ウォールコネクター（Gen 3）のIPアドレス。未設定だと外出先の充電も制御対象になる**（下記） |
| `WALL_CONNECTOR_SERIAL` | | `""` | 任意。設定するとIPアドレスが別の機器に変わった場合を起動時に検知できる |
| `WALL_CONNECTOR_TIMEOUT_SEC` | | `5` | ウォールコネクターへのHTTPタイムアウト秒 |
| `WALL_CONNECTOR_ATTEMPTS` | | `2` | 読み取り失敗時に、その場で取り直す回数（1回目を含む） |
| `FAST_CHARGER_POWER_KW` | | `15` | 外出先判定のフォールバック閾値 |
| `MIN_AMPS` | | `4` | 充電を維持する下限電流。これを下回ると停止する |
| `MAX_AMPS` | | `48` | 上限電流。**充電設備とブレーカーの容量に合わせること** |
| `START_AMPS` | | `MIN_AMPS + 2` | 充電開始・車両起動に要求する余剰（ヒステリシス用） |
| `STOP_DEBOUNCE_CYCLES` | | `2` | 余剰不足が何サイクル続いたら停止するか |
| `COMMAND_RETRIES` | | `3` | 充電コマンドのリトライ回数 |
| `TERMINAL_BACKOFF_SEC` | | `600` | 満充電・ケーブル未接続を観測した後の待機秒 |
| `TERMINAL_WAKE_SUPPRESS_SEC` | | `3600` | 同上の状態で車両を起こさない秒数。**必ず `TERMINAL_BACKOFF_SEC` より長くすること** |
| `NIGHT_STOP_MAX_ATTEMPTS` | | `6` | 夜間の停止確認をあきらめるまでの回数 |
| `NIGHT_GET_ATTEMPTS` | | `2` | 夜間のGETをその場で再試行する回数 |
| `CONTROL_PORT` | | `8090` | スマホ操作用サーバーの待受ポート |
| `CONTROL_TOKEN` | ○ | `""` | スマホ操作用サーバーの共有シークレット。空だとサーバーは起動しない |

### 自宅ウォールコネクターの設定

**`WALL_CONNECTOR_HOST` を設定しないと、システムは充電している場所を区別しない。**スーパーチャージャーや目的地充電器での充電も停止・電流変更の対象になる（`docs/03_operation.md` の「外出先での充電を制御しない」を参照）。

対象は **Gen 3（Wi-Fi対応）に限る。**Gen 2 や、Wi-Fiを宅内LANに接続していない個体では応答しないため、この機能は利用できない。その場合は空のままにする（全経路が従来どおり動作する）。

1. **IPアドレスを確認する。**Teslaアプリの充電器設定画面、またはルーターのDHCPクライアント一覧で調べる
2. **ルーター側でDHCP予約を設定する。**アドレスが変わると判定不能に落ちるため、必ず固定する
3. **ラズパイから到達できることを確認する。**

```bash
curl -s --max-time 5 http://<ウォールコネクターのIP>/api/1/version
curl -s --max-time 5 http://<ウォールコネクターのIP>/api/1/vitals
```

4. **`tesla_config.json` に設定する。**`WALL_CONNECTOR_SERIAL` には `/api/1/version` の `serial_number` を転記する

```json
"WALL_CONNECTOR_HOST": "<ウォールコネクターのIP>",
"WALL_CONNECTOR_SERIAL": "<シリアル番号>"
```

サービス起動後、ログに以下が出れば正しく紐づいている。

```text
[INFO] 自宅ウォールコネクター（<ウォールコネクターのIP> / シリアル <シリアル番号>）を確認しました。
```

未設定の場合は、起動時に次の行が出る。意図してそうしているのでなければ、上の手順で設定すること。

```text
[ATTENTION] 自宅ウォールコネクターが未設定のため、外出先の充電判定は行いません。スーパーチャージャー等での充電も停止・電流変更の対象になります。
```

---

## 6. 【Step 5】systemd へのサービス登録と自動常駐化

OS起動時に「プロキシ → 充電制御スクリプト」の順で安全にバックグラウンド連動起動させるため、システムマネージャへ登録を行う。

### 1. プロキシ用設定ファイルの配置

```bash
sudo nano /etc/systemd/system/tesla-proxy.service

```

（以下をコピペして保存、`<username>` は実際の実行ユーザー名に置換すること）

```ini
[Unit]
Description=Tesla HTTP Proxy Server for TVCP
After=network.target

[Service]
Type=simple
User=<username>
WorkingDirectory=/home/<username>/tesla-solar-charge
ExecStart=/home/<username>/tesla-solar-charge/tesla-http-proxy -cert /home/<username>/tesla-solar-charge/cert.pem -tls-key /home/<username>/tesla-solar-charge/key.pem -key-file /home/<username>/tesla-solar-charge/tesla_app_key.pem -port 4443
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target

```

### 2. 充電制御用設定ファイルの配置

```bash
sudo nano /etc/systemd/system/tesla-charger.service

```

（以下をコピペして保存、`<username>` は実際の実行ユーザー名に置換すること）

```ini
[Unit]
Description=Tesla Solar Automated Charging Controller
Requires=tesla-proxy.service
After=network.target tesla-proxy.service

[Service]
Type=simple
User=<username>
WorkingDirectory=/home/<username>/tesla-solar-charge
ExecStart=/home/<username>/tesla-solar-charge/venv/bin/python tesla_solar_charger.py
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target

```

### 3. スマホ操作用コントロールサーバー設定ファイルの配置

`tesla_config.json` に `CONTROL_PORT`（既定8090）と `CONTROL_TOKEN`（`openssl rand -hex 32` 等で生成したランダムな共有シークレット）を設定したうえで、以下を配置する。

```bash
sudo nano /etc/systemd/system/tesla-override.service

```

（以下をコピペして保存、`<username>` は実際の実行ユーザー名に置換すること）

```ini
[Unit]
Description=Tesla Solar Charger Manual Override Control Server
After=network.target

[Service]
Type=simple
User=<username>
WorkingDirectory=/home/<username>/tesla-solar-charge
ExecStart=/home/<username>/tesla-solar-charge/venv/bin/python control_server.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target

```

### 4. 観測ツール用設定ファイルの配置（任意）

`tools/observe_only.py` を使う場合のみ配置する。**`enable` しないこと**（Tesla APIの課金が継続するため。詳細は `docs/03_operation.md` の「読み取り専用の観測ツール」）。

```bash
sudo nano /etc/systemd/system/tesla-observer.service
```

定義は `docs/01_architecture.md` 第7章④を参照。配置後は `sudo systemctl daemon-reload` のみ実行し、次項の一括 `enable` の対象には含めない。

### 5. サービスの有効化と即時起動コマンド

ファイルを配置後、システムに認識させて一気に起動する。

```bash
# systemdのマネージャ設定をリロード（変更の反映）
sudo systemctl daemon-reload

# OS起動時の自動実行を有効化
sudo systemctl enable tesla-proxy.service
sudo systemctl enable tesla-charger.service
sudo systemctl enable tesla-override.service

# サービスを今すぐ手動起動
sudo systemctl start tesla-proxy.service
sudo systemctl start tesla-charger.service
sudo systemctl start tesla-override.service

```

---

## 7. 【Step 6】稼働・正常性確認チェック

デプロイが正常に完了したか、以下のコマンドで最終確認を行う。

```bash
# 3つのサービスが揃って緑文字の「active (running)」になっているか確認
sudo systemctl status tesla-proxy.service tesla-charger.service tesla-override.service

# メモリ上でプロセスが物理的に3つ並んで実在しているか確認
ps aux | grep tesla

```