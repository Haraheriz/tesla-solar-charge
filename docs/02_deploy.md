# 📑 2. 再構築・デプロイ手順書（再現性のためのマスターコマンド集）

## 1. 前提条件とディレクトリ構造

ハードウェア障害（SDカード突然死など）やOSアップデートに伴い、システムをゼロから再構築する際の手順である。本手順は、ターゲット環境が **Linux（Raspberry Pi OS 64bit等）** であり、一般的な非特権ユーザー権限で実行されることを想定している。

```Markdown
### 📁 最終的な配置ディレクトリ構造（Linux側）
※ `venv/` 以外の全資材をWindowsから転送した後、Step 2のコマンドによってLinux側で `venv/` を自動生成させる。

/home/<username>/tesla-solar-charge/
├── cert.pem                   # [転送] TLS通信用 公開鍵証明書
├── key.pem                    # [転送] TLS通信用 秘密鍵（RSA形式）
├── tesla_app_key.pem          # [転送] テスラ車両コマンド用 秘密鍵（EC形式）
├── tesla_config.json          # [転送] システム設定ファイル
├── tesla_tokens.json          # [転送] テスラAPIリフレッシュトークン
├── tesla-http-proxy           # [転送] Go言語ネイティブバイナリ
├── tesla_solar_charger.py     # [転送] 充電制御メインスクリプト
└── venv/                      # [Linux側で生成] Python3 仮想環境（相対パスでの運用不可）

> ⚠️ **ファイル名の注意（Windows側 ↔ ラズパイ側の不一致）：**
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

機密情報（テスラ車両を遠隔操作可能な生のリフレッシュトークンや暗号鍵）をマルチユーザー環境から保護し、且つ `systemd` からの実行権限を担保するための鉄壁の権限設定コマンド群である。

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

## 5. 【Step 4】systemd へのサービス登録と自動常駐化

OS起動時に「プロキシ ➔ 充電制御スクリプト」の順で安全にバックグラウンド連動起動させるため、システムマネージャへ登録を行う。

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

### 3. サービスの有効化と即時起動コマンド

ファイルを配置後、システムに認識させて一気に起動する。

```bash
# systemdのマネージャ設定をリロード（変更の反映）
sudo systemctl daemon-reload

# OS起動時の自動実行を有効化
sudo systemctl enable tesla-proxy.service
sudo systemctl enable tesla-charger.service

# サービスを今すぐ手動起動
sudo systemctl start tesla-proxy.service
sudo systemctl start tesla-charger.service

```

---

## 6. 【Step 5】稼働・正常性確認チェック

デプロイが完璧に完了したか、以下のコマンドで最終確認を行う。

```bash
# 2つのサービスが揃って緑文字の「active (running)」になっているか確認
sudo systemctl status tesla-proxy.service tesla-charger.service

# メモリ上でプロセスが物理的に2つ並んで実在しているか確認
ps aux | grep tesla

```