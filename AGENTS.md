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

`tests/` は `main()` の常駐ループを**仮想時計と擬似Tesla API**で駆動する（実APIには接続しない）。数時間ぶんのサイクルが一瞬で回るため、「満充電の車にコマンドを送り続ける」「夜通し充電が止まらない」といった時間依存の不具合を検出できる。GitHub Actions（`.github/workflows/tests.yml`）が push / PR で自動実行する。

### 守ること

- **テストを gitignore 済みファイルに依存させない。** `tesla_config.json` / `tesla_tokens.json` / `*.pem` は開発機にしか無い。過去に、トークンファイルが在る開発機では通り、CIでのみ `main()` が初回OAuth認証フローに入りポート8000で無限ブロックした事例がある。設定は `TESLA_CONFIG_PATH` / `TESLA_TOKEN_PATH` でテスト用のダミーを指すこと。
- **`poc/test_*.py` を pytest で実行しない。** あれは実APIを直接叩く手動確認用スクリプトである。`pytest.ini` の `testpaths = tests` で収集対象を限定しているので、この設定を外さないこと。
- **Lint・型チェックのCIは意図的に導入していない。** 正しさが実機の挙動に依存するため。静的解析を増やす前に、まず回帰テストで再現できないかを検討する。
- **実機でしか確認できない部分は実機で確認する。** テストが通っても本番のラズパイに反映しなければ挙動は変わらない（デプロイ手順は `docs/02_deploy.md`、運用確認は `docs/03_operation.md`）。

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
