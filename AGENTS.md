# tesla-solar-charge project rules

## Overview
Tesla solar charging automation project (Python).
GitHub: Haraheriz/tesla-solar-charge

## Code style
- Language: Python
- Follow existing file structure and naming conventions

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
