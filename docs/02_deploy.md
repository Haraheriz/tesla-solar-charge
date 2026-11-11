# 2. 再構築・デプロイ手順書（再現性のためのマスターコマンド集）

## 1. 前提条件とディレクトリ構造

ハードウェア障害（SDカードの突然の故障など）やOSアップデートに伴い、システムをゼロから再構築する際の手順である。本手順は、ターゲット環境が Linux（Raspberry Pi OS 64bit等）であり、一般的な非特権ユーザー権限で実行されることを想定している。

```Markdown
### 最終的な配置ディレクトリ構造（Linux側）
※ `venv/` 以外の全資材をWindowsから転送した後、Step 2のコマンドによってLinux側で `venv/` を自動生成させる。

/home/<username>/tesla-solar-charge/
├── cert.pem                   # [転送] TLS通信用 公開鍵証明書
├── key.pem                    # [転送] TLS通信用 秘密鍵（RSA形式）
├── tesla_app_key.pem          # [転送] テスラ車両コマンド用 秘密鍵（EC形式）
├── tesla_config.json          # [転送] システム設定ファイル
├── tesla_tokens.json          # [転送] テスラAPIリフレッシュトークン
├── tesla-http-proxy           # [転送] Go言語ネイティブバイナリ（版は deploy/proxy/VERSION、手順は第2章）
├── tesla_solar_charger.py     # [転送] 充電制御メインスクリプト
├── tesla_auth.py              # [転送] トークンファイルの読み書きと更新ロック。充電制御と観測ツールが import する
├── control_server.py          # [転送] スマホ操作用コントロールサーバー
├── control_templates/         # [転送] 操作画面。中身は page.html、manifest.webmanifest、sw.js
├── override_state.py          # [転送] フル充電モード・外出先の充電記録・目標充電率の状態を共有するモジュール
├── vehicle_status.py          # [転送] 最後に読んだ車両の状態と、車両側の充電上限の元の値を保存するモジュール
├── config_loader.py           # [転送] 設定値を起動時に検証して確定させるモジュール。日中の時刻（7時と18時）もここにある
├── wall_connector.py          # [転送] 自宅ウォールコネクターのローカルAPI読み取りモジュール
├── icons/                     # [転送] PWA用アプリアイコン（icon-*.png, icon-maskable-*.png, apple-touch-icon-180.png）
├── tools/observe_only.py      # [転送・任意] 読み取り専用の観測ツール。使うときだけ置く
├── deploy/systemd/            # [転送] systemd ユニットファイル4つ（第6章で /etc/systemd/system/ へ配置）
└── venv/                      # [Linux側で生成] Python3 仮想環境（相対パスでの運用不可）
```

> **ファイル名の注意（Windows側 ↔ ラズパイ側の不一致）：**
> Windows側の開発・検証スクリプト（`poc/complete_tesla_activation.py`等）は、テスラ車両コマンド用の鍵ペアを `private-key.pem` / `public-key.pem` という名前で生成する。一方、ラズパイ側の `systemd` サービス定義（`tesla-proxy.service`）は `tesla_app_key.pem` という名前を前提にしている。
> 転送時は鍵の中身（バイト列）は同一のまま、**ファイル名だけ `tesla_app_key.pem` にリネームしてから配置すること**。リネームせずに別名のまま使う場合は、`/etc/systemd/system/tesla-proxy.service` 内の `-key-file` のパスも合わせて書き換える必要がある。

---

## 2. 【Step 1】開発環境でのクロスコンパイル（Windows側作業）

テスラ公式プロキシ（[teslamotors/vehicle-command](https://github.com/teslamotors/vehicle-command) の `cmd/tesla-http-proxy`）は、ラズパイOS（Linux ARM）上で直接ビルドするよりも、手元のWindows環境（PowerShell）からクロスコンパイルして実行バイナリを生成する方が効率的である。ラズパイには Go を入れていない。

バイナリはこのリポジトリに入れない（`.gitignore` 3項）。代わりに、取得元・タグ・コミット・Go の版を [`deploy/proxy/VERSION`](../deploy/proxy/VERSION) に固定し、その版でビルドする手順を [`deploy/proxy/build.ps1`](../deploy/proxy/build.ps1) にしてある。理由は次の3点である。

* 公式はソースからのビルドと Docker イメージで配布している（[README](https://github.com/teslamotors/vehicle-command#installation-and-configuration)）
* ライセンスは [Apache-2.0](https://github.com/teslamotors/vehicle-command/blob/main/LICENSE) であり、バイナリを同梱すると第4条の再配布の条件（ライセンス文の添付など）が付く
* GitHub はバイナリの配布に Git ではなく Releases を使うよう案内している（[GitHub Docs「About large files on GitHub」](https://docs.github.com/ja/repositories/working-with-files/managing-large-files/about-large-files-on-github#distributing-large-binaries)）

### 現在の版（2026-11-11 確認）

| 項目 | 値 |
| --- | --- |
| 取得元 | `https://github.com/teslamotors/vehicle-command` |
| タグ | `v0.4.1`（2026-02-04） |
| コミット | `49977a18fd68567501d59e16a6c9e4a8b9348544` |
| Go | `go1.26.3`（`GOOS=linux` / `GOARCH=arm64` / `CGO_ENABLED=0`） |
| ラズパイ上のバイナリ | 17,757,394 バイト、SHA-256 `3ff5c5cfe4a41326f14cace68d8dfabf478f8e8fa093d440d67a0a010f830789`、2026-06-20 23:39 配置 |

この値は、ラズパイのバイナリに埋め込まれたビルド情報（`strings` で読める `mod` / `build vcs.revision` の行）と、Windows側のチェックアウト `vehicle-command/` の `git rev-parse HEAD` が一致することで確認した。ビルド情報には `v0.4.1+dirty`（`vcs.modified=true`）と出るが、チェックアウトの追跡対象ファイルに変更は無く、未追跡のビルド成果物だけがある。Go は未追跡ファイルがあるときも `modified=true` を付ける。

### ビルド手順

リポジトリのルートで PowerShell を開き、次を実行する。

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\proxy\build.ps1
```

スクリプトは次の順に処理し、確認が1つでも合わなければ中止する。

1. 手元の Go の版が `VERSION` の `GO_VERSION` と同じかを確認する（版を上げる作業のときだけ `-AllowGoMismatch` を付ける）
2. リポジトリ直下の `vehicle-command/`（`.gitignore` で除外済み）が無ければ clone する。origin が `REPO` と同じか、タグ `TAG` が `COMMIT` を指すか、追跡対象ファイルに変更が無いかを確認し、`COMMIT` へ切り替える
3. `cmd/tesla-http-proxy` で `GOOS` / `GOARCH` / `CGO_ENABLED=0` を指定して `go build` し、`vehicle-command/cmd/tesla-http-proxy/tesla-http-proxy-arm64` を出力する。環境変数は終了時に元へ戻す
4. SHA-256 を表示し、`VERSION` の `SHA256`（ラズパイで動いているバイナリの値）と一致するかを表示する

Go の出力は、ビルドしたディレクトリのパスと未追跡ファイルの有無でも変わる。別のPCや別のディレクトリでビルドした場合、SHA-256 は一致しなくてよい。そのときは配置後に新しい値を `docs/DEPLOY_LOG.md` に記録し、`VERSION` の `SHA256` を更新する。

* ※生成された `tesla-http-proxy-arm64` を、**`tesla-http-proxy` という名前で**、SCPやSFTP等を用いてLinux側の `/home/<username>/tesla-solar-charge/` ディレクトリへ転送する。
* 転送後、ラズパイで `sha256sum /home/<username>/tesla-solar-charge/tesla-http-proxy` を実行し、表示された値と一致することを確認する。

### 版を上げるとき

1. 公式の [Releases](https://github.com/teslamotors/vehicle-command/releases) で新しいタグと変更内容を確認する
2. `VERSION` の `TAG` / `COMMIT`（そのタグが指すコミット）/ 必要なら `GO_VERSION` を書き換え、PR にする
3. `build.ps1` でビルドし、ラズパイの旧バイナリを残したまま（例：`backup-YYYYMMDD/` へコピー）転送して `tesla-proxy.service` を再起動する
4. `docs/DEPLOY_LOG.md` に版と SHA-256 を記録し、`VERSION` の `SHA256` を更新する

---

## 3. 【Step 2】Python 仮想環境（venv）の構築（Linux側作業）

OS全体のシステム環境（グローバル）の破損を防ぐため、Debian 12以降の作法に則り、プロジェクト専用の隔離されたPython実行環境を構築する。

Linuxのコンソールで対象ディレクトリに移動し、以下の2行を実行する。

```bash
# 1. ディレクトリ内に「venv」という名前の独立した仮想環境を生成
python3 -m venv /home/<username>/tesla-solar-charge/venv

# 2. 仮想環境専用の pip を使用して、依存ライブラリ（requests）を導入する。
#    版は requirements-dev.txt に固定してある値と同じにする（CIがテストした版を本番へ入れる）
/home/<username>/tesla-solar-charge/venv/bin/pip install "$(grep '^requests==' /home/<username>/tesla-solar-charge/requirements-dev.txt)"

```

---

## 4. 【Step 3】資産パーミッションの厳格化と一括修復

機密情報（テスラ車両を遠隔操作可能な生のリフレッシュトークンや暗号鍵）をマルチユーザー環境から保護し、且つ `systemd` からの実行権限を担保するための厳格な権限設定コマンド群である。

```bash
# 1. ディレクトリ配下すべての所有者を、実行ユーザーに統一
sudo chown -R <username>:<username> /home/<username>/tesla-solar-charge/

# 2. ディレクトリ本体を「所有者以外立ち入り禁止（700）」に設定
chmod 700 /home/<username>/tesla-solar-charge/

# 3. 直下の通常ファイルを「所有者のみ読み書き（600）」に制限
#    このコマンドはディレクトリにも当たる。control_templates / icons / tools / deploy の走査権限（x）が
#    消えると、操作サーバーはテンプレートを開けず、アイコンも配信できず、第6章でユニットファイルを読めない。
#    直後の find で戻す。
chmod 600 /home/<username>/tesla-solar-charge/*
for d in control_templates icons tools deploy; do
  dir="/home/<username>/tesla-solar-charge/$d"
  if [ -d "$dir" ]; then
    find "$dir" -type d -exec chmod 700 {} +
    find "$dir" -type f -exec chmod 600 {} +
  fi
done

# 4. Goプロキシバイナリに「実行権限（700）」を付与
chmod 700 /home/<username>/tesla-solar-charge/tesla-http-proxy

# 5. 【最重要】Python仮想環境（venv）の走査権限（x）および実行権限の完全修復
find /home/<username>/tesla-solar-charge/venv -type d -exec chmod 700 {} +
find /home/<username>/tesla-solar-charge/venv -type f -exec chmod 600 {} +
chmod 700 /home/<username>/tesla-solar-charge/venv/bin/*

```

`tesla_solar_charger.py` は `main()` の `load_runtime_config()` で、`control_server.py` はプロセス起動時に、`tesla_config.json`（充電制御側は `tesla_tokens.json` も）の権限を確かめる。所有者以外に読み書き実行のいずれかを許すビットが立っていれば次の行を出す。起動は止めない。この行が出たら上記の `chmod 600` を実行する。`import` しただけでは設定ファイルは読まれない。

トークン更新と操作状態の更新は、実行中にロックファイルを作る。転送しない。権限はプロセスが `0600` で作る。

* `tesla_tokens.json.lock` … `tesla_auth.py`。充電制御と観測ツールがトークンを更新するあいだだけ取る
* `override_state.json.lock` … `override_state.py`。フル充電モード、外出先の充電記録、目標充電率を書き換えるあいだだけ取る

```text
[ATTENTION] /home/<username>/tesla-solar-charge/tesla_config.json の権限が -rw-r--r-- で、所有者以外からアクセスできます。chmod 600 /home/<username>/tesla-solar-charge/tesla_config.json を実行してください（docs/02_deploy.md 第4章）。
```

---

## 5. 【Step 4】`tesla_config.json` の設定

`tesla_config.json` は `.gitignore` 対象のため、リポジトリを取得しただけでは存在しない。`tesla_config.json.template` をコピーして作成する。

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
| `WALL_CONNECTOR_HOST` | ○ | `""` | 自宅ウォールコネクター（Gen 3）のIPアドレス。未設定だと外出先の充電も制御対象になる（下記） |
| `WALL_CONNECTOR_SERIAL` | | `""` | 任意。設定するとIPアドレスが別の機器に変わった場合を起動時に検知できる |
| `WALL_CONNECTOR_TIMEOUT_SEC` | | `5` | ウォールコネクターへのHTTPタイムアウト秒 |
| `WALL_CONNECTOR_ATTEMPTS` | | `2` | 読み取り失敗時に、その場で取り直す回数（1回目を含む） |
| `DISCONNECTED_PROBE_INTERVAL_SEC` | | `600` | 外出先の充電記録がONのとき、ケーブル未接続の車両データを読み直す間隔（秒）。ON/OFFはスマホアプリで切替える |
| `FAST_CHARGER_POWER_KW` | | `15` | 外出先判定のフォールバック閾値 |
| `MIN_AMPS` | | `4` | 充電を維持する下限電流。これを下回ると停止する |
| `MAX_AMPS` | | `48` | 上限電流。**充電設備とブレーカーの容量に合わせること** |
| `START_AMPS` | | `MIN_AMPS + 2` | 充電開始・車両起動に要求する余剰（ヒステリシス用） |
| `STOP_DEBOUNCE_CYCLES` | | `2` | 余剰不足が何サイクル続いたら停止するか |
| `WAKE_DEBOUNCE_CYCLES` | | `2` | 就寝中の車を起こすのに、余剰が何サイクル続くことを要求するか |
| `REMO_SAMPLES` | | `1` | 1サイクルあたりのRemo取得回数。**増やす前に下記を読むこと** |
| `REMO_SAMPLE_INTERVAL_SEC` | | `10` | 同上の間隔秒 |
| `COMMAND_RETRIES` | | `3` | 充電コマンドのリトライ回数 |
| `TERMINAL_BACKOFF_SEC` | | `600` | 満充電・ケーブル未接続を観測した後の待機秒 |
| `TERMINAL_WAKE_SUPPRESS_SEC` | | `3600` | 同上の状態で車両を起こさない秒数。**必ず `TERMINAL_BACKOFF_SEC` より長くすること** |
| `NIGHT_STOP_MAX_ATTEMPTS` | | `6` | 夜間の停止確認をあきらめるまでの回数 |
| `NIGHT_GET_ATTEMPTS` | | `2` | 夜間のGETをその場で再試行する回数 |
| `CONTROL_PORT` | | `8090` | スマホ操作用サーバーの待受ポート |
| `CONTROL_TOKEN` | ○ | `""` | スマホ操作用サーバーの共有シークレット。文字列でない値、32文字未満、テンプレート値、表示可能なASCII文字（0x21〜0x7E）以外を含む値では起動しない。`openssl rand -hex 32` で生成する |

### `REMO_SAMPLES` を増やす前に

平滑化のつもりで増やしても効果がない。スマートメーターの更新間隔は中央値61秒（2026-08-09〜18 の12,055回の観測で95%が60秒）であり、10秒間隔で複数回読んでも同じ値が返る。3回×10秒だった頃は、同じ値を3回平均したうえで1サイクルにつき20秒とAPI 2回分を費やしていた。

複数の更新をまたぐには120〜180秒の窓が要るが、制御サイクル自体が180秒であるため、サイクル内では実現できない。平滑化が必要なら、サイクルをまたいで直近N回の値を使う別の設計を検討すること。

### 自宅ウォールコネクターの設定

`WALL_CONNECTOR_HOST` を設定しないと、システムは充電している場所を区別しない。スーパーチャージャーや目的地充電器での充電も停止・電流変更の対象になる（`docs/03_operation.md` の「外出先での充電を制御しない」を参照）。

対象は Gen 3（Wi-Fi対応）に限る。Gen 2 や、Wi-Fiを宅内LANに接続していない個体では応答しないため、この機能は利用できない。その場合は空のままにする（全経路が従来どおり動作する）。

1. IPアドレスを確認する。Teslaアプリの充電器設定画面、またはルーターのDHCPクライアント一覧で調べる
2. ルーター側でDHCP予約を設定する。アドレスが変わると判定不能に落ちるため、必ず固定する
3. **ラズパイから到達できることを確認する。**

```bash
curl -s --max-time 5 http://<ウォールコネクターのIP>/api/1/version
curl -s --max-time 5 http://<ウォールコネクターのIP>/api/1/vitals
```

4. `tesla_config.json` に設定する。`WALL_CONNECTOR_SERIAL` には `/api/1/version` の `serial_number` を転記する

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

OS起動時に「プロキシ → 充電制御スクリプト」の順で安全にバックグラウンド連動起動させるため、システムマネージャへ登録する。

### 1. ユニットファイルの配置

ユニットファイルの実物は [`deploy/systemd/`](../deploy/systemd/) にある。4つとも `<username>` を実行ユーザー名に置き換えて `/etc/systemd/system/` に置く。各設定値の理由は `docs/01_architecture.md` 第5章・第7章にある。

| ファイル | 役割 | `enable` |
| --- | --- | --- |
| [`tesla-proxy.service`](../deploy/systemd/tesla-proxy.service) | テスラ公式プロキシ（4443番） | する |
| [`tesla-charger.service`](../deploy/systemd/tesla-charger.service) | 充電制御（プロキシに依存） | する |
| [`tesla-override.service`](../deploy/systemd/tesla-override.service) | スマホ操作用コントロールサーバー | する |
| [`tesla-observer.service`](../deploy/systemd/tesla-observer.service) | 読み取り専用の観測ツール（任意） | **しない** |

Windows側からリポジトリの `deploy/systemd/` をラズパイの `~/tesla-solar-charge/deploy/systemd/` へ転送したうえで、ラズパイで次を実行する。

```bash
cd /home/<username>/tesla-solar-charge
for u in tesla-proxy tesla-charger tesla-override tesla-observer; do
  sed "s/<username>/$USER/g" "deploy/systemd/$u.service" | sudo tee "/etc/systemd/system/$u.service" >/dev/null
done
```

配置済みのユニットがリポジトリと同じかは、次のコマンドで確認できる。差分が無ければ何も表示されない。

```bash
cd /home/<username>/tesla-solar-charge
for u in tesla-proxy tesla-charger tesla-override tesla-observer; do
  sed "s/<username>/$USER/g" "deploy/systemd/$u.service" | diff -u - "/etc/systemd/system/$u.service"
done
```

ユニットを変えるときは、`deploy/systemd/` を PR で変更してから上のコマンドで配置する。ラズパイ上で直接 `nano` で編集すると、リポジトリと食い違う。

### 2. プロキシ用ユニットの注意

`tesla-proxy.service` の引数は `-cert` と `-tls-key` の組み合わせで、名前が対称ではない。公式 README の [HTTP proxy の例](https://github.com/teslamotors/vehicle-command#using-the-http-proxy)と `cmd/tesla-http-proxy/main.go` のフラグ定義に合わせてある。揃えて書き換えると起動しない（`docs/01_architecture.md` 第5章）。

### 3. スマホ操作用コントロールサーバーの前提

`tesla_config.json` に `CONTROL_PORT`（既定8090）と `CONTROL_TOKEN`（`openssl rand -hex 32` 等で生成したランダムな共有シークレット）を設定しておく。操作サーバーは `0.0.0.0:8090` で待ち受け、宅内LANから直接アクセスできる。Tailscale経由でも利用する場合は、ラズパイとスマートフォンを同じtailnetに参加させ、ラズパイでTailscale ServeのHTTPS転送先を `http://localhost:8090` に設定する。Funnelとルーターの8090番ポート転送は有効にしない。宅内LANから直接アクセスする場合、操作トークンは平文HTTPのURLで送られる。

`tesla-override.service` の起動または再起動前に、ラズパイ上で次の形式確認を実行する。`CONTROL_TOKEN` がJSON文字列でない、32文字未満、`YOUR_RANDOM_CONTROL_TOKEN_HERE`、または表示可能なASCII文字（0x21〜0x7E）以外を含む場合、スクリプトはエラーで終了し、`control_server.py` も起動を中止する。この確認はトークンの値を出力しない。

```bash
cd /home/<username>/tesla-solar-charge
python3 - <<'PY'
import json
from pathlib import Path

token = json.loads(Path("tesla_config.json").read_text(encoding="utf-8")).get("CONTROL_TOKEN")
if not (isinstance(token, str) and len(token) >= 32 and token != "YOUR_RANDOM_CONTROL_TOKEN_HERE"
        and all(0x21 <= ord(c) <= 0x7E for c in token)):
    raise SystemExit("CONTROL_TOKEN の形式が無効です")
print("CONTROL_TOKEN の形式を確認しました")
PY
```

### 4. 観測ツール用ユニット（任意）

`tools/observe_only.py` を使う場合のみ配置する。**`enable` しないこと**（Tesla APIの課金が継続するため。詳細は `docs/03_operation.md` の「読み取り専用の観測ツール」）。

配置は本章1の手順で行う。配置後は `sudo systemctl daemon-reload` のみ実行し、本章5の一括 `enable` の対象には含めない。`tools/observe_only.py` は同じディレクトリの `tesla_auth.py` を import する。`tesla_auth.py` が無いと観測ツールは起動しない。トークンファイルは充電制御と分けない。

ユニットファイルの `ExecStart` は変えていない。`tesla_auth.py` と `control_templates/` は、作業ディレクトリ（`/home/<username>/tesla-solar-charge`）からの import とファイル読み込みで使われる。ユニットのパスを書き換える必要はない。

### 5. サービスの有効化と即時起動コマンド

ファイルを配置したあと、システムに認識させて一気に起動する。

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

デプロイが正常に完了したか、以下のコマンドで最終確認する。

```bash
# 3つのサービスが揃って緑文字の「active (running)」になっているか確認
sudo systemctl status tesla-proxy.service tesla-charger.service tesla-override.service

# メモリ上でプロセスが物理的に3つ並んで実在しているか確認
ps aux | grep tesla

```

`tesla_auth.py` が無い状態で `tesla-charger.service` を起動すると、`import` に失敗して終了コード 1 になり、`Restart=always` が 30 秒ごとに再起動する。`control_templates/` が無い、またはディレクトリの走査権限が無い状態で `tesla-override.service` を起動すると、テンプレートを開けずに終了する。

---

## 8. 既に動いているラズパイへ、この版のファイルを足す

ユニット定義は変えない。次のファイルを、いまの配置へ上書きする。`venv/`、`tesla_tokens.json`、`tesla_config.json`、`*.pem`、`tesla-http-proxy` は、この版のためだけには転送しない。

| ファイル | 無いときに止まるサービス |
| --- | --- |
| `tesla_solar_charger.py` | `tesla-charger.service` |
| `tesla_auth.py` | `tesla-charger.service`。観測ツールを置くなら `tesla-observer.service` も |
| `override_state.py` | 充電制御と操作サーバーの両方 |
| `config_loader.py` | 充電制御と操作サーバーの両方 |
| `wall_connector.py` | `tesla-charger.service` |
| `vehicle_status.py` | 充電制御と操作サーバーの両方 |
| `control_server.py` | `tesla-override.service` |
| `control_templates/page.html` | `tesla-override.service` |
| `control_templates/manifest.webmanifest` | `tesla-override.service` |
| `control_templates/sw.js` | `tesla-override.service` |
| `tools/observe_only.py` | 観測ツールを使うときだけ |

開発機のリポジトリで、転送元は `git show origin/main:<パス>` のように、作業ツリーではなく `main` のファイルを使う。例は次のとおり。`<host>` はラズパイの SSH 先、`<username>` は実行ユーザーである。

```bash
cd /path/to/tesla-solar-charge
git fetch origin main
DEST=<username>@<host>:/home/<username>/tesla-solar-charge
for f in tesla_solar_charger.py tesla_auth.py override_state.py config_loader.py wall_connector.py vehicle_status.py control_server.py; do
  git show "origin/main:$f" > "/tmp/$f"
  scp "/tmp/$f" "$DEST/$f"
done
mkdir -p /tmp/control_templates
for f in page.html manifest.webmanifest sw.js; do
  git show "origin/main:control_templates/$f" > "/tmp/control_templates/$f"
done
scp -r /tmp/control_templates "$DEST/"
```

観測ツールも更新するときは、同じ要領で `tools/observe_only.py` を `.../tesla-solar-charge/tools/observe_only.py` へ置く。`rsync -a` でディレクトリごと置く場合も、上の一覧が揃っていることを確認する。`venv/` は送らない。

転送後、ラズパイで権限と構文を確認してから、プロキシ以外を再起動する。プロキシのバイナリを変えていなければ `tesla-proxy.service` は再起動しない。

```bash
cd /home/<username>/tesla-solar-charge
chmod 600 tesla_solar_charger.py tesla_auth.py override_state.py config_loader.py wall_connector.py vehicle_status.py control_server.py
find control_templates -type d -exec chmod 700 {} +
find control_templates -type f -exec chmod 600 {} +
./venv/bin/python -m py_compile tesla_solar_charger.py tesla_auth.py override_state.py config_loader.py wall_connector.py vehicle_status.py control_server.py
sudo systemctl restart tesla-charger.service tesla-override.service
sudo systemctl status tesla-charger.service tesla-override.service
```

`py_compile` が `ModuleNotFoundError: tesla_auth` になるときは、`tesla_auth.py` が作業ディレクトリに無い。操作サーバーが `FileNotFoundError` で `control_templates` を名指すときは、ディレクトリか中の3ファイルが無い。反映の記録は `docs/DEPLOY_LOG.md` に書く。
