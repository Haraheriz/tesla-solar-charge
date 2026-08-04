# tesla-solar-charge project rules

## Overview
Tesla solar charging automation project (Python).
GitHub: Haraheriz/tesla-solar-charge

## Code style
- Language: Python
- Follow existing file structure and naming conventions

## テスト

充電制御ループを変更したら必ず実行する。

```bash
pip install -r requirements-dev.txt
python -m pytest
```

`tests/` は `main()` の常駐ループを**仮想時計と擬似Tesla API**で駆動する（実APIには接続しない）。数時間ぶんのサイクルが一瞬で回るため、「満充電の車にコマンドを送り続ける」「夜通し充電が止まらない」といった時間依存の不具合を検出できる。

GitHub Actions（`.github/workflows/tests.yml`）が **main への push と PR** で自動実行する。`claude-review.yml`（自動レビュー）は **PR のみ**。**フィーチャーブランチへ push しても何も走らない**ため、「PRを作る前にCIや自動レビューの結果を見る」という順序は取れない。ブランチ上での検証手段はローカルの `python -m pytest` だけである。

### 守ること

- **テストを gitignore 済みファイルに依存させない。** `tesla_config.json` / `tesla_tokens.json` / `*.pem` は開発機にしか無い。過去に、トークンファイルが在る開発機では通り、CIでのみ `main()` が初回OAuth認証フローに入りポート8000で無限ブロックした事例がある。設定は `TESLA_CONFIG_PATH` / `TESLA_TOKEN_PATH` でテスト用のダミーを指すこと。
- **`poc/test_*.py` を pytest で実行しない。** あれは実APIを直接叩く手動確認用スクリプトである。`pytest.ini` の `testpaths = tests` で収集対象を限定しているので、この設定を外さないこと。
- **Lint・型チェックのCIは意図的に導入していない。** 正しさが実機の挙動に依存するため。静的解析を増やす前に、まず回帰テストで再現できないかを検討する。
- **実機でしか確認できない部分は実機で確認する。** テストが通っても本番のラズパイに反映しなければ挙動は変わらない（デプロイ手順は `docs/02_deploy.md`、運用確認は `docs/03_operation.md`）。
- **外部通信を伴う関数を追加・変更したら `tests/conftest.py` の差し替えを見直す。** 2026-08-05まで `refresh_tesla_token()` の差し替えが漏れており、トークンリフレッシュを踏むテストがあれば `auth.tesla.com` へ実際にPOSTしていた。差し替え漏れは「テストが落ちる」形では現れず、その経路を通るテストを書いて初めて実APIを叩く。
- **バグ修正では、既存テストが旧挙動を仕様として固定していないか疑う。** 2026-08-05、夜間監視の欠陥（休止入り時の1回で確認を打ち切る）を修正した際、`test_夜間休止入口の停止成功後は再問い合わせしない` がその欠陥を要件として記述しており、正しい修正がテストを壊す形になっていた。修正でテストが落ちたら、実装だけでなくテストの前提も検討対象にすること。

## API利用料

Tesla Fleet API は従量課金である（カテゴリ別の単価と課金対象は `docs/01_architecture.md` 第3章③）。月額割引 ¥1,448 の枠内で運用しており、実績は約¥450/月。

**制御ループのAPI呼び出しの頻度・種類を変えたら、費用影響を見積もる。** 単価がカテゴリごとに大きく異なるため、リクエスト数の増減だけでは判断できない。

- `GET /api/1/vehicles`（車両リスト）は**課金されない**。夜間休止中の10分毎のポーリングはこの性質を前提にしている
- `vehicle_data` は ¥0.287/件、`wake_up` は ¥2.75/件（約10倍）
- `wake_up` はリトライ1回ごとに課金される。`wake_up_vehicle()` は最大5回POSTするため、ログ1行が課金1件とは限らない
- 実績は Tesla開発者ダッシュボードの「請求と使用状況」で確認する

## Commit messages
- Follow global conventions (see ~/.codex/AGENTS.md)

## この設定ファイル自体の管理（Git）

このファイル（AGENTS.md）はリポジトリに含まれ Git で管理されています。
CLAUDE.md は `@AGENTS.md` の1行のみで、このファイルが唯一の編集対象です。

### 編集手順
1. AGENTS.md を直接編集する（エディタ何でも可）
2. `git add AGENTS.md && git commit -m "..." && git push` で GitHub へ送信

### 別の端末で受け取る
`git pull` のみ。

### 注意
- CLAUDE.md は編集しない（@AGENTS.md の参照ポインタのため）
- グローバル設定（~/.codex/AGENTS.md）との使い分け:
  このファイル → プロジェクト固有のルール
  グローバル   → 全プロジェクト共通の個人設定
