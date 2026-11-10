# Tesla Solar Automated Charging System

本プロジェクトは太陽光発電の余剰電力に合わせてテスラ車両の充電電流を動的に全自動制御するシステムである。

## ドキュメント体系
`docs/` にあるドキュメントは、以下の9つである。

1. [Nature Remo API 初期セットアップ](./docs/00_nature_remo_onboarding.md)
   - Nature Remo E からスマートメーターの瞬時電力（W）を読むための、API の有効化とアクセストークン発行の手順
2. [テスラ Fleet API 初期セットアップ](./docs/00_onboarding.md)
   - 開発者登録、車両コマンド用の鍵ペアの生成、実車への公開鍵の承認までの手順
3. [仕様書・アーキテクチャ](./docs/01_architecture.md)
   - システムの全体像、使用API、AI向け特殊制約の定義
4. [再構築・デプロイ手順書](./docs/02_deploy.md)
   - クロスコンパイル、仮想環境構築、パーミッション一括修復コマンド集
5. [運用・トラブルシューティングマニュアル](./docs/03_operation.md)
   - 日常のログ確認方法、トークン失効時の「初回手動認証方式」更新手順、緊急対応
6. [認証情報漏洩時の対応手順](./docs/04_security_incident_response.md)
   - 履歴からの機密情報除去、Tesla/Nature Remo認証情報のローテーション手順
7. [目標充電率の設計](./docs/05_charge_target_design.md)
   - 目標充電率で充電を止める仕様。車両側の充電上限は50%未満に設定できない
8. [セキュリティ対策の記録](./docs/06_security_hardening.md)
   - これまでの対策の一覧と根拠の PR。脆弱性の報告方法は [SECURITY.md](./SECURITY.md)
9. [デプロイ履歴](./docs/DEPLOY_LOG.md)
   - ラズパイへ本番デプロイした際の簡易記録

## 設定

`tesla_config.json` に設定を書く。このファイルは認証情報を含むため `.gitignore` 対象であり、リポジトリを取得しただけでは存在しない。`tesla_config.json.template` をコピーして作成する。

```bash
cp tesla_config.json.template tesla_config.json
chmod 600 tesla_config.json
```

キーの一覧と既定値は [デプロイ手順書の「`tesla_config.json` の設定」](./docs/02_deploy.md) にある。必須のキーはTesla・Nature Remoの認証情報のほか、次の1つがある。

* `WALL_CONNECTOR_HOST` — 自宅のTesla Wall Connector（Gen 3）のIPアドレス。未設定だとシステムは充電している場所を区別せず、スーパーチャージャーや目的地充電器での充電も停止・電流変更の対象になる。車両側のAPIには接続先の充電器を識別する情報が存在しないため、宅内LANのウォールコネクターを読むことでのみ「自宅で充電しているか」を判定できる。Gen 2 やWi-Fi未接続の個体では利用できないため、その場合は空のままにする（従来どおりの動作になる）

未設定のまま起動した場合は、起動時に `ATTENTION` ログで通知される。

## テスト

充電制御ループの回帰テストを `tests/` に置いている。次のコマンドで実行する。

```bash
pip install -r requirements-dev.txt
python -m pytest
```

`main()` の常駐ループを仮想時間で駆動し、Tesla API・Nature Remo API・オーバーライド状態を差し替えて動かす（実APIには一切接続しない。`tests/fixtures/ci_config.json` はダミー値）。数時間ぶんのサイクルが一瞬で回るため、「満充電の車に4時間コマンドを送り続ける」「夜通し充電が止まらない」といった時間に依存する不具合を検証できる。

テストの多くは 2026-07-15 に実際に起きた障害（フル充電モードが17時間41分継続し、ユーザーの停止操作が3分後に上書きされた事象）に対応する回帰テストである。

GitHub Actions（[tests.yml](./.github/workflows/tests.yml)）が main への push と PR で自動実行する。なおLint・型チェックのCIは導入していない。このシステムの正しさは実機（テスラ車両・ラズパイ）での動作に依存しており、静的解析で捕捉できる範囲が本質的に狭いため。**実機でしか確認できない部分は必ず実機で確認すること**（[運用マニュアル](./docs/03_operation.md)参照）。

> `poc/` 配下にも `test_*.py` があるが、あれは実APIを直接呼び出す手動確認用スクリプトであり自動テストではない。`pytest.ini` で収集対象を `tests/` に限定している。

## プロジェクト資産とマルチ環境マッピング

本プロジェクトのディレクトリ構造、および各環境（Windows開発環境 / ラズパイ本番環境）におけるファイルの配置とデータの動線は以下の通りである。

```text
tesla-solar-charge/
├── docs/                             # [共用] ドキュメント 9ファイル。各ファイルの内容は上記「ドキュメント体系」
│   ├── 00_nature_remo_onboarding.md  # Nature Remo API の有効化とアクセストークン発行
│   ├── 00_onboarding.md              # Tesla Fleet API と車両コマンド鍵の初期設定
│   ├── 01_architecture.md            # 仕様、使用 API、起動から昼夜の制御まで
│   ├── 02_deploy.md                  # 再構築、転送、権限、既存環境への更新
│   ├── 03_operation.md               # ログ確認、トークン失効、緊急時の確認
│   ├── 04_security_incident_response.md # 認証情報漏洩時の履歴除去と再発行
│   ├── 05_charge_target_design.md    # 目標充電率で充電を止める仕様
│   ├── 06_security_hardening.md      # 実施したセキュリティ対策と根拠の PR
│   └── DEPLOY_LOG.md                 # ラズパイへ本番反映した記録
│
├── tesla_solar_charger.py            # [共用] 充電制御メインスクリプト（両環境で直接実行可能）
├── tesla_auth.py                     # [共用] トークンファイルの読み書きと更新ロック。充電制御と観測ツールが使う
├── control_server.py                 # [共用] スマホ操作用コントロールサーバー（フル充電モードの切替UI）
├── control_templates/                # [共用] 操作画面の page.html、manifest.webmanifest、sw.js
├── override_state.py                 # [共用] フル充電モード・外出先の充電記録・目標充電率。更新中は flock
├── vehicle_status.py                 # [共用] 最後に読んだ車両の状態と、車両側の充電上限の元の値の保存モジュール
├── config_loader.py                  # [共用] 数値設定の下限検査。日中は 7:00 以上 18:00 未満
├── wall_connector.py                 # [共用] 自宅ウォールコネクターのローカル API
├── tools/observe_only.py             # [任意] 読み取り専用の観測。トークンは tesla_auth.py 経由で本体と共有
├── icons/                             # [共用] PWA用アプリアイコン（icon-*.png, icon-maskable-*.png, apple-touch-icon-180.png）
├── tesla_config.json                 # [共用] システム設定ファイル（各環境のトークン/パスを記載）
├── override_state.json               # 【動線: スマホ → RPi】マニュアル・オーバーライドの現在状態（自動生成）
│
├── poc/                               # [開発用] 本実装に至るまでの実証・検証スクリプト一式
│                                     # (Tesla/Nature Remo APIの認証フローを個別に確認するための使い捨てコード。本番では使用しない)
│                                     # 詳細・セットアップ方法は poc/README.md を参照
│
├── tesla_tokens.json                 # 【動線: Win → RPi】テスラAPI認可トークン
│                                     # (Windows側で一時実行して生成し、ラズパイへ転送・上書き配置)
│
├── tesla_app_key.pem                 # 【動線: Win → RPi】車両コマンド用秘密鍵（暗号資産）
├── cert.pem                          # 【動線: Win → RPi】ローカルHTTPS通信用 公開鍵証明書
├── key.pem                           # 【動線: Win → RPi】ローカルHTTPS通信用 秘密鍵（RSA）
│
├── tesla-http-proxy                  # [ラズパイ専用] Go言語ネイティブバイナリ（拡張子なし）
│                                     # (Windows側で GOOS=linux としてクロスコンパイルし転送)
│
├── venv/                             # [ラズパイ専用] Python3 仮想環境（隔離ディレクトリ）
│                                     # (ラズパイ側でローカル生成するため、Windows側からは転送厳禁)
│
└── tesla_solar_charger.log           # [ラズパイ専用] 自動充電制御システムのローテーションログ
                                      # (ラズパイ常駐稼働時に動的に自動生成・自動圧縮される)

```

## ライセンス

[MIT License](./LICENSE)