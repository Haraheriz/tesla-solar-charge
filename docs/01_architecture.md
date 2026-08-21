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

#### PWA（Progressive Web App）対応

`control_server.py` は以下の資材を配信し、ホーム画面アイコンからアプリのように起動できるPWAとして構成されている。

* **`icons/icon-192.png` / `icons/icon-512.png`：** ダークネイビー地に緑の雷アイコンを描いたPNG画像（外部画像ライブラリ無しで、`struct`/`zlib`/`binascii.crc32` を用いた自前のPNGエンコーダで生成）。OSのマスク処理でアイコンの角が欠けないよう、図柄を全体の約80%サイズに縮小して中央配置（"maskable"対応）。
* **`/manifest.webmanifest`：** アプリ名・テーマカラー・アイコン・`start_url` を定義するWebアプリマニフェスト。`start_url` に実際の `CONTROL_TOKEN` を埋め込むため、このエンドポイントは他のページと同様に **トークン必須**（トークン漏洩防止のため）。
* **`/sw.js`：** インストール判定（Service Worker登録）のためだけに存在する最小限のService Worker。充電状態は常に最新を取得する必要があるため、実質的なキャッシュ戦略は持たない（オフライン時のフォールバック処理のみ）。機密情報を含まないため、トークン無しで公開配信する。
* **アイコン画像自体（`/icons/icon-192.png`, `/icons/icon-512.png`）：** 同様に機密情報を含まないため、トークン無しで公開配信する。

> **重要な制約（iOS/Android差異）：** iOS Safariの「ホーム画面に追加」は、平文HTTP・Service Worker無しでも `apple-touch-icon` と `apple-mobile-web-app-capable` 等のメタタグだけで機能する。一方、**Android Chromeは「インストール可能」と判定するために安全なコンテキスト（HTTPSまたは`localhost`）を要求する**ため、宅内LANの平文HTTP（`http://<ラズパイのIP>:8090/...`）ではService Workerの登録が静かに失敗し、Android側は正式なPWAインストール（ホーム画面追加は可能でも、スタンドアロン起動やインストールバナーは出ない）にはならない。Androidでも完全なPWA体験が必要な場合は、Tailscale等のVPN経由で到達可能なホスト名にTLS証明書を発行し、HTTPS経由でアクセスする構成を推奨する。

#### TailscaleによるHTTPS化（Funnelは意図的に不採用）

上記のAndroid制約に対応するため、ラズパイをTailscale（WireGuardベースのメッシュVPN）に参加させ、`tailscale serve --https=443 http://localhost:8090` でコントロールサーバーをHTTPS化している。Tailscale導入済みの場合、`tailscale status --self` から取得できるMagicDNSホスト名（`<ホスト名>.<tailnetドメイン>.ts.net`）でHTTPS証明書が自動発行され、Android Chromeでも正式なPWAインストールが可能になる。

* **Serve（採用）：** tailnet（自分の管理する端末群）内からのみHTTPSアクセス可能。同じtailnetにスマートフォンを参加させればよく、外出先からでも宅外公開なしでアクセスできる。
* **Funnel（不採用）：** tailnetの外、つまり公開インターネット上の誰でもアクセス可能になる機能。本システムの操作対象は車両の充電（フル充電モードのON/OFF）であり、防御線が`CONTROL_TOKEN`一本のみであることを踏まえ、トークン漏洩・総当たりのリスクが公開のメリットを上回ると判断し、意図的に有効化していない。宅外からの利用は、スマートフォン側にもTailscaleアプリを入れて同じtailnetに参加させることで、Funnelなしで同等のアクセスを実現する。

### 車両保護ロジック（Insomnia Defense / 不眠症防御アルゴリズム）

本システムは、テスラ車両が正常に「スリープ（睡眠状態）」に移行できるようにするため、以下の2段階チェックを厳格に実行する。

1. **フェーズ1（状態キャッシュ確認）：** `GET /api/1/vehicles` を実行し、テスラサーバー側が保持している車両の状態キャッシュを確認する。
2. **フェーズ2（条件分岐）：**
* キャッシュ状態が `asleep` または `offline` の場合：車両を起こさないよう、詳細データの取得（`vehicle_data`）およびコマンド送信を完全にスキップし、休止する。
* キャッシュ状態が `online` の場合のみ：詳細データ（現在の充電電流値、バッテリー残量など）を取得し、演算・制御を実行する。


### 充電場所の判別（自宅ウォールコネクターのローカルAPI）

本システムの制御は自宅の太陽光余剰を前提としている。同じ制御を外出先の充電に適用すると、利用者が意図した充電を妨害する（2026-08-07、外出先のスーパーチャージャーでの充電を繰り返し停止させた）。そのため、コマンドを送る前に「いま充電しているのは自宅か」を判定する。

**車両側のAPIからは判別できない。** `charge_state` の全フィールドを確認したが、接続先の充電器を識別する情報（シリアル・DIN・サイトID・位置）は一つも含まれていない。`conn_charge_cable` はケーブルの規格、`fast_charger_brand` / `fast_charger_type` はDC充電器のみを表す。

判別は宅内LAN上のウォールコネクター（Gen 3）を直接読むことで行う。

```text
GET http://<WALL_CONNECTOR_HOST>/api/1/vitals   → vehicle_connected
GET http://<WALL_CONNECTOR_HOST>/api/1/version  → serial_number（起動時の個体確認）
```

本質は個体シリアルの照合ではなく、**宅内LANから到達できること自体が「自宅の充電器である」ことを示す**点にある。車両が `Charging` を報告しているのに `vehicle_connected` が `false` なら、その充電は自宅ではない。DC・ACを問わず判別できるため、スーパーチャージャーだけでなく目的地充電器や知人宅でのAC充電も対象になる。**ただし `true` は逆向きの証明にならない**（後述）。

**この判定はTesla Fleet APIを一切使わない。**リクエスト数は1件も増えず、下記③の課金にも影響しない。OAuthスコープの追加も再認可も不要である（位置情報 `drive_state` を使う案は `vehicle_location` スコープの追加が必要で、この点で不利だった）。

判定するのは「外出先だと断定できたか」だけで、「自宅である」とは断定しない。根拠は2つあり、**車両側から先に評価する。**(1) DC急速充電を検知した — 自宅の充電設備はACであるため、DC充電なら自宅ではありえない。車両ごとの `charge_state` に基づくので、別の車が自宅の充電器を使っていても影響を受けない。(2) 自宅の充電器に何も繋がっていない。どちらも得られなければ従来どおりの動作に落ちる。詳細は `docs/03_operation.md` の「外出先での充電を制御しない」を参照。

エンドポイントはTeslaが公式に文書化したものではない。塞がれた場合は判定不能となり、自動的に従来動作へ縮退する。

**この判定は車両が1台であることを前提にしている。** ウォールコネクターのローカルAPIが返す `vehicle_connected` が示すのは「何らかの車が自宅の充電器に繋がっている」ことであって、「制御対象の車が繋がっている」ことではない（`/api/1/vitals` にVINは含まれない）。また制御対象は常に車両リストの先頭1台であり、2台目は扱えない。**テスラ車の複数所有、および自宅のウォールコネクターを複数の車両が使う運用は現時点で未対応であり、将来的に対応が必要である。**制約の詳細と対応の方向は `docs/03_operation.md` の「既知の制約：複数の車両に対応していない」を参照。


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


* **`POST /api/1/vehicles/{vehicle_id}/command/charge_start` / `charge_stop`**
* **用途：** 充電の開始・停止を指令する。


* **`POST /api/1/vehicles/{vehicle_id}/wake_up`**
* **用途：** 就寝中（`asleep` / `offline`）の車両を起こす。単価が突出して高いため、Insomnia Defense により送信条件を厳しく絞っている。



### ③ Tesla Fleet API の従量課金

Fleet APIは2025年1月から従量課金である。無料枠という形ではなく、**アカウントごとに月額の割引（クレジット）が付き、それを超えた分だけ課金**される。単価は2026-08-05にTesla開発者ダッシュボード（請求と使用状況）の実績値から逆算したもの。

| カテゴリ | 単価 | 該当エンドポイント |
| --- | --- | --- |
| データ | **¥0.287 / 件**（$0.002） | `vehicle_data` |
| コマンド | ¥0.145 / 件（$0.001） | `set_charging_amps`, `charge_start`, `charge_stop` |
| ウェイク | **¥2.75 / 件**（$0.02） | `wake_up` |
| ストリーミング | $0.0001 / シグナル | 本システムでは未使用 |

月額割引は **¥1,448**（$10相当）。別途アカウントに請求限度額（本番環境では¥1,000）を設定でき、割引適用後の金額がこれを超えて課金されることはない。ステータスコード500未満のリクエストはすべて課金対象で、**このシステムが頻繁に受ける408も課金される**。

**`GET /api/1/vehicles`（車両リスト）は課金されない。** 2026-08-01〜08-04の実測で、車両リストを386回叩いているのに対し「データ」の請求件数は143件であり、これは同期間の `vehicle_data` 呼び出し数（約142回）と一致する。この非対称性が、夜間休止中に10分毎のポーリングを行う設計の前提になっている（毎サイクル叩くのは車両リストのみで、`vehicle_data` は車両が `online` のときだけ呼ぶ）。

実績の消費は約¥15/日（約¥450/月）で、割引枠に収まっている。夜間に車両が一晩中オンラインである最悪ケースでも増加は約¥671/月であり、合計¥1,121/月と枠内に留まる。

**自宅ウォールコネクターのローカルAPIは課金対象外である。** Tesla Fleet APIではなく宅内LANの機器を直接読むため、上表のどのカテゴリにも該当しない。充電場所の判別を追加しても、リクエスト数と請求額はいずれも変化しない（第2章「充電場所の判別」を参照）。

**ウェイクはリトライ1回ごとに課金される。** `wake_up_vehicle()` は成功するまで最大5回POSTを繰り返すため、ログ上の「起動命令（Wake Up）を送信します」1行が課金1件とは限らない。2026-08-01はログ上4回の起動に対し課金は8件で、平均2回叩いていた。単価がデータの約10倍であり、コスト面で最も注意すべき呼び出しである。


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

### ③ スマホ操作用コントロールサーバー用：`/etc/systemd/system/tesla-override.service`

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