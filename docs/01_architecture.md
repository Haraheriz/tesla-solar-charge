# 1. システムアーキテクチャ・仕様書（兼 AIナビゲーション専用リファレンス）

## 1. システム概要

本システムは、Nature Remo Eから取得した太陽光発電の瞬時余剰電力（W）に基づき、テスラ車両の充電電流（A）を3分周期で動的かつ全自動で制御する、完全自律型のエネルギー最適化インフラである。

一般的なWebUIを介した連携とは異なり、Linux（Raspberry Pi等）のローカル環境内に「テスラ公式HTTPプロキシ」を常駐させ、宅内LANのセキュリティ（PNA制限の完全回避）を維持したまま、テスラ公式の「フリートAPI（Command Protocol）」へ署名付きコマンドを直接投入する構造を持つ。

---

## 2. システムアーキテクチャと通信フロー

システムを構成するコンポーネントおよび論理通信フローは以下の通りである。

```text
[太陽光パネル] → [パワーコンディショナ]
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

### マニュアル・オーバーライド機構（スマホからのフル充電切替）

太陽光の発電状況に関わらず充電したい場合（来客時の急ぎ充電など）に備え、`control_server.py` が宅内LAN上で軽量HTTPサーバーとして常駐し、スマートフォンのブラウザから「フル充電モード」をワンタップでON/OFFできる。

```text
[スマートフォン (ブラウザ)]
         │ (HTTPS/HTTP、トークン付きURL)
         ▼
  ┌────────────────────────┐
  │   control_server.py     │  ← override_state.json を読み書き
  │   (stdlib http.server)  │
  └───────────┬─────────────┘
              │ (ファイル共有: override_state.json)
              ▼
  ┌────────────────────────┐
  │ tesla_solar_charger.py │  ← 毎サイクル override_state.json を確認
  └────────────────────────┘
```

* **`override_state.json`：** `{"manual_override": true/false, "updated_at": ...}` を保持する共有状態ファイル。`override_state.py` が原子的な読み書き（`save_tokens`と同様の tmp→rename 方式）を提供する。
* **`manual_override: true` の場合：** `tesla_solar_charger.py` は夜間休止モードおよびNature Remoの瞬時電力に基づく漸進的フィードバック制御（第4章）をすべてスキップし、車両を起動（必要な場合）して `MAX_AMPS` でのフル充電を維持する。
* **`manual_override: false` の場合：** 通常の太陽光追従ロジックに復帰する。
* **認証：** `control_server.py` はクエリパラメータ `?token=` またはヘッダー `X-Control-Token` で、`tesla_config.json` の `CONTROL_TOKEN`（ランダムな共有シークレット）との一致を要求する。トークンが一致しない場合はHTTP 403を返し、ページ・APIともに一切の情報を返さない。
* **UI：** トークン付きURL（例：`http://<ラズパイのIP>:8090/?token=<CONTROL_TOKEN>`）にアクセスすると、ON/OFFトグルボタン1つだけのモバイル向けページが表示される。スマホのホーム画面に追加（Webクリップ）しておけば、ネイティブアプリのように1タップで起動できる。

### 車両保護ロジック（Insomnia Defense / 不眠症防御アルゴリズム）

本システムは、テスラ車両が正常に「スリープ（睡眠状態）」に移行できるようにするため、以下の2段階チェックを厳格に実行する。

1. **フェーズ1（状態キャッシュ確認）：** `GET /api/1/vehicles` を実行し、テスラサーバー側が保持している車両の状態キャッシュを確認する。
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

* **`GET /api/1/vehicles`**
* **用途：** 車両の一覧および最新の状態キャッシュ（`state`）を取得する（スリープ阻害防止の最重要API）。


* **`GET /api/1/vehicles/{vehicle_id}/vehicle_data`**
* **用途：** 車両の現在のバッテリー残量（`battery_level`）、充電ステータス（`charging_state`）、および現在の設定電流値（`charge_current_request`）を精密に取得する。


* **`POST /api/1/vehicles/{vehicle_id}/command/set_charging_amps`**
* **用途：** 太陽光の余剰電力に合わせて、車両の充電電流（アンペア数）を `1A` 単位で動的に変更・指令する。



---

## 4. 充電電流の演算ロジック（漸進的フィードバック制御）

本システムの核心部分である「何Aに設定すべきか」の判定は、太陽光の発電量から絶対値として毎回ゼロ計算しているのではなく、**現在の充電電流値を基準に、Nature Remoが示す買電/売電量（W）に応じて加減算する漸進的フィードバック制御**である（サーモスタットの動作に近い）。

```python
calc_base_amps = raw_amps if charging_status == "Charging" else 0   # 充電中なら現在のA値を基準にする
adjustment_amps = int(-house_power / 200)                            # 買電/売電量(W)をA換算（200Wあたり1A）
target_amps = calc_base_amps + adjustment_amps
```

* `house_power` が負（売電中・余剰あり）の場合：`adjustment_amps` は正の値になり、電流を**増やす**方向に働く。
* `house_power` が正（買電中・余剰なし）の場合：`adjustment_amps` は負の値になり、電流を**減らす**方向に働く。

### 具体例

現在 `15A` で充電中に、買電量が `1000W`（余剰なし）になった場合：

```
adjustment_amps = -1000 / 200 = -5A
target_amps = 15 + (-5) = 10A
```

「15Aで充電しているせいで1000W分買電になっている。5A（=1000W）減らせば収支がゼロに近づく」という考え方で、**即座に停止するのではなく電流を絞る**。`target_amps`（`10A`）が `MIN_AMPS`（既定 `3A`）以上である限り、充電は継続される。

### 充電停止の条件

`target_amps` が `MIN_AMPS` を下回った場合のみ、「どれだけ電流を絞っても充電する価値がない」と判断し、`charge_stop` を発行する。1サイクルの買電検知だけで即座に止まるわけではなく、何サイクルかかけて収支ゼロに近づけながら、それでも電流が確保できないと判明した時点で停止する設計である。

---

## 5. 設計上の注意点

以下は、テスラ公式バイナリおよびLinuxシステムの**仕様上の制限（変則ルール）**である。一般的な命名規則に合わせて「綺麗に揃える」ような修正をすると、システムが即座に起動不能（status=1 / status=2）になるため注意すること。

### 誤認識を防止すべき4つの「固有仕様」

1. **プロキシ引数フラグの「非対称性」（最重要）：**
HTTPS通信を確立する際、証明書を渡すフラグは `-tls-cert` ではなく、**`-cert`** である。対して秘密鍵を渡すフラグは **`-tls-key`** である。これらを `-tls-cert` / `-tls-key` のように対称形に書き換えてはならない（`status=2/INVALIDARGUMENT` で即死する）。
2. **車両用署名鍵フラグの独立性：**
通信用秘密鍵（`-tls-key`）とは別に、テスラ車への命令署名用の鍵として **`-key-file`** フラグに「車両コマンド用秘密鍵」を単独で明示する必要がある。環境変数 `TESLA_PRIVATE_KEY` や `TESLA_KEY_FILE` による指定は、システムサービス起動時に認識漏れを起こすため、引数（フラグ）直接投入を正解とする。
3. **特権ポート（443番）の回避：**
プロキシはデフォルトで `443` 番ポートを開こうとするが、Linuxのセキュリティ上、一般ユーザー権限では1024番以下の特権ポートを開放できない（`status=1/FAILURE` となる）。そのため、明示的に **`-port 4443`** を指定し、非特権ポートで待ち受ける。
4. **Python環境（venv）の実行権限：**
Debian 12以降のシステムガードにより、グローバル環境への `pip` インストールはブロックされる。必ず `/home/<username>/tesla-solar-charge/venv` の仮想環境を通り、且つシステムサービスから叩くためにバイナリおよび親ディレクトリには `700`（走査権限 `x` の維持）が与えられていなければならない。

---

## 6. 過去の失敗事例と完全なる対応策データベース

常駐化に際し、実際に発生したエラーコードとその原因、およびデバッグを完了させた最終解決策の記録である。再構築時やAIがエラーをパースする際の参照資料として使用すること。

| ステータスコード / エラーメッセージ | 発生した原因 | 最終対応策（正解） |
| --- | --- | --- |
| **`status=203/EXEC`** | `chmod 600` により、Pythonの仮想環境（`venv`）フォルダ配下のすべての「走査権限（x）」が消失し、`systemd` から実行ファイルを叩けなくなった。 | `find` コマンドを使い、ディレクトリに `700`、実行バイナリに `700` をピンポイントで再付与した。 |
| **`status=1/FAILURE`**<br>

<br>`Error: private key location not provided` | 車両制御用の秘密鍵（TVCP署名鍵）の指定が不足していた、あるいは環境変数がOSの実行ユーザー階層で消失した。 | 引数に公式のフラグである `-key-file` を追加し、フル絶対パスで直接流し込んだ。 |
| **`status=2/INVALIDARGUMENT`**<br>

<br>`Server TLS private key file` | 良かれと思って引数の名前を綺麗に揃え、`-tls-cert` と記述したため、Goの引数パーサーに弾かれた。 | 引数名を公式の不揃いな仕様通り、`-cert` と `-tls-key` のコンビネーションに戻した。 |
| **`Error: x509: failed to parse private key`** | HTTPS通信用の鍵を入れるべき場所（`-tls-key`）と、車両用の署名鍵を入れるべき場所（`-key-file`）の両方に同じ通信用 `key.pem` を指定してしまった。 | 車両コマンド用の暗号鍵を `tesla_app_key.pem` として完全分離して指定した。 |

---

## 7. 【完全版】systemd サービス定義リファレンス（マスターデータ）

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

### ③ スマホ操作用コントロールサーバー用：`/etc/systemd/system/tesla-control.service`

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

`tesla-charger.service` とは独立して起動・停止できる（`Requires=`の依存関係なし）。コントロールサーバーが落ちていても充電制御自体は通常運転を継続する。