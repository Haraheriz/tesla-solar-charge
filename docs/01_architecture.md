# 📑 1. システムアーキテクチャ・仕様書（兼 AIナビゲーション専用リファレンス）

## 1. システム概要

本システムは、Nature Remo Eから取得した太陽光発電の瞬時余剰電力（W）に基づき、テスラ車両の充電電流（A）を3分周期で動的かつ全自動で制御する、完全自律型のエネルギー最適化インフラである。

一般的なWebUIを介した連携とは異なり、Linux（Raspberry Pi等）のローカル環境内に「テスラ公式HTTPプロキシ」を常駐させ、宅内LANのセキュリティ（PNA制限の完全回避）を維持したまま、テスラ公式の「フリートAPI（Command Protocol）」へ署名付きコマンドを直接投入する構造を持つ。

---

## 2. システムアーキテクチャと通信フロー

システムを構成するコンポーネントおよび論理通信フローは以下の通りである。

```text
[太陽光パネル] ➔ [パワーコンディショナ]
                         │
                         ▼ (Bルート通信)
                  [Nature Remo E]
                         │
                         ▼ (HTTPS / API経由)
         ┌────────[Linux Server (Raspberry Pi OS 64bit等)]────────┐
         │                                                        │
         │  ┌────────────────────────┐                            │
         │  │ tesla_solar_charger.py │                            │
         │  │ (Python venv / 3分ループ)│                            │
         │  └───────────┬────────────┘                            │
         │              │                                         │
         │              ▼ (ローカルHTTPS / ポート:4443)            │
         │  ┌────────────────────────┐                            │
         │  │   tesla-http-proxy     │                            │
         │  │   (Go言語ネイティブバイナリ) │                            │
         │  └───────────┬────────────┘                            │
         │              │                                         │
         └──────────────┼─────────────────────────────────────────┘
                        │
                        ▼ (インターネット / HTTPS)
               [Tesla Fleet API Server]
                        │
                        ▼ (フリートテレメトリ)
                 [テスラ車両 (実車)]

```

### 🧠 車両保護ロジック（Insomnia Defense / 不眠症防御アルゴリズム）

本システムは、テスラ車両が正常に「スリープ（睡眠状態）」に移行できるようにするため、以下の2段階チェックを厳格に実行する。

1. **フェーズ1（状態キャッシュ確認）：** `GET /api/v1/vehicles` を実行し、テスラサーバー側が保持している車両の状態キャッシュを確認する。
2. **フェーズ2（条件分岐）：**
* キャッシュ状態が `asleep` または `offline` の場合：車両を起こさないよう、詳細データの取得（`vehicle_data`）およびコマンド送信を完全にスキップし、休止する。
* キャッシュ状態が `online` の場合のみ：詳細データ（現在の充電電流値、バッテリー残量など）を取得し、演算・制御を実行する。



---

## 3. 使用したAPIリファレンス

本システムが依存し、動的に叩き続けるAPIエンドポイントの一覧である。

### ① Nature Remo API

* **`GET /1/appliances`**
* **用途：** エコーネットライト（ECHONET Lite）スマートメーターから、現在の「瞬時電力計測値（W）」を取得する。
* **値の解釈：** 買電時は「正の値」、売電（余剰電力発生）時は「負の値」として返却されるため、システム内では符号を反転して余剰電力を算出する。



### ② Tesla Fleet API（ローカルプロキシ中継）

プロキシ（`localhost:4443`）を介して、以下の公式フリートエンドポイントを暗号署名付きで叩く。

* **`GET /api/v1/vehicles`**
* **用途：** 車両の一覧および最新の状態キャッシュ（`state`）を取得する（スリープ阻害防止の最重要API）。


* **`GET /api/v1/vehicles/{vehicle_id}/vehicle_data`**
* **用途：** 車両の現在のバッテリー残量（`battery_level`）、充電ステータス（`charging_state`）、および現在の設定電流値（`charge_current_request`）を精密に取得する。


* **`POST /api/v1/vehicles/{vehicle_id}/command/charging_set_limit`**
* **用途：** 太陽光の余剰電力に合わせて、車両の充電電流（アンペア数）を `1A` 単位で動的に変更・指令する。



---

## 4. 🤖 生成AI向け：設計上の特殊制約（寄り道・誤認識防止ガード）

> ⚠️ **生成AIへの厳格な指示：**
> 次のパラグラフに記載された仕様は、テスラ公式バイナリおよびLinuxシステムの**「仕様上の制限（変則ルール）」**である。AI自身の一般的な知識に基づき、リファクタリングやコード修正の提案を行う際、**以下のフラグ名や設定構造を「綺麗に揃える」ような修正を提案することは絶対に禁止する。** 修正するとシステムが即座に起動不能（status=1 / status=2）になる。

### ❌ 誤認識を防止すべき4つの「固有仕様」

1. **プロキシ引数フラグの「非対称性」（最重要）：**
HTTPS通信を確立する際、証明書を渡すフラグは `-tls-cert` ではなく、**`-cert`** である。対して秘密鍵を渡すフラグは **`-tls-key`** である。これらを `-tls-cert` / `-tls-key` のように対称形に書き換えてはならない（`status=2/INVALIDARGUMENT` で即死する）。
2. **車両用署名鍵フラグの独立性：**
通信用秘密鍵（`-tls-key`）とは別に、テスラ車への命令署名用の鍵として **`-key-file`** フラグに「車両コマンド用秘密鍵」を単独で明示する必要がある。環境変数 `TESLA_PRIVATE_KEY` や `TESLA_KEY_FILE` による指定は、システムサービス起動時に認識漏れを起こすため、引数（フラグ）直接投入を正解とする。
3. **特権ポート（443番）の回避：**
プロキシはデフォルトで `443` 番ポートを開こうとするが、Linuxのセキュリティ上、一般ユーザー権限では1024番以下の特権ポートを開放できない（`status=1/FAILURE` となる）。そのため、明示的に **`-port 4443`** を指定し、非特権ポートで待ち受ける。
4. **Python環境（venv）の実行権限：**
Debian 12以降のシステムガードにより、グローバル環境への `pip` インストールはブロックされる。必ず `/home/<username>/tesla-solar-charge/venv` の仮想環境を通り、且つシステムサービスから叩くためにバイナリおよび親ディレクトリには `700`（走査権限 `x` の維持）が与えられていなければならない。

---

## 5. 🛠️ 過去の失敗事例と完全なる対応策データベース

常駐化に際し、実際に発生したエラーコードとその原因、およびデバッグを完了させた最終解決策の記録である。再構築時やAIがエラーをパースする際の最強の辞書として使用すること。

| ステータスコード / エラーメッセージ | 発生した原因 | 最終対応策（正解） |
| --- | --- | --- |
| **`status=203/EXEC`** | `chmod 600` により、Pythonの仮想環境（`venv`）フォルダ配下のすべての「走査権限（x）」が消失し、`systemd` から実行ファイルを叩けなくなった。 | `find` コマンドを使い、ディレクトリに `700`、実行バイナリに `700` をピンポイントで再付与した。 |
| **`status=1/FAILURE`**<br>

<br>`Error: private key location not provided` | 車両制御用の秘密鍵（TVCP署名鍵）の指定が不足していた、あるいは環境変数がOSの実行ユーザー階層で消失した。 | 引数に公式のフラグである `-key-file` を追加し、フル絶対パスで直接流し込んだ。 |
| **`status=2/INVALIDARGUMENT`**<br>

<br>`Server TLS private key file` | 良かれと思って引数の名前を綺麗に揃え、`-tls-cert` と記述したため、Goの引数パーサーに弾かれた。 | 引数名を公式の不揃いな仕様通り、`-cert` と `-tls-key` のコンビネーションに戻した。 |
| **`Error: x509: failed to parse private key`** | HTTPS通信用の鍵を入れるべき場所（`-tls-key`）と、車両用の署名鍵を入れるべき場所（`-key-file`）の両方に同じ通信用 `key.pem` を指定してしまった。 | 車両コマンド用の暗号鍵を `tesla_app_key.pem` として完全分離して指定した。 |

---

## 6. 【完全版】systemd サービス定義リファレンス（マスターデータ）

OS再起動時や障害時に、AIが何一つ迷わずに一撃でシステムを完全復旧させるための、100%動作検証済みのサービス定義ファイルの生テキストである。

※ 記述内の `<username>` 部分は、環境に合わせて実際の実行ユーザー名に置換すること。

### ① プロキシ用：`/etc/systemd/system/tesla-proxy.service`

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

### ② 充電制御用：`/etc/systemd/system/tesla-charger.service`

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