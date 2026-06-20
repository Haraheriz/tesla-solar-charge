# 4. 認証情報漏洩時の対応手順（セキュリティインシデント・レスポンス）

開発初期の実証用スクリプト（`poc/`）にTesla Client Secret・Nature Remo Access Tokenが直接ハードコードされたままGitにコミットされていたことが判明した際の、実際の対応記録に基づく再利用可能な手順書である。同様の事態（認証情報の誤コミット、第三者への意図しない開示等）が再発した場合は、本手順に従って対応する。

---

## 1. 検知時に最初に確認すること

* 漏洩した値（Client Secret、Access Token、秘密鍵等）が**どのコミットから**含まれているか（`git log --all -p -- <file>` 等）
* リポジトリが publicか privateか、誰がアクセスできる状態だったか
* その値が**現在も有効か**（無効化済みのものなら履歴除去のみで良いが、有効なら即時ローテーションが優先）

---

## 2. コード側の応急処置

1. ハードコードされた値を環境変数（または `tesla_config.json` 等のgitignore対象ファイル）から読み込む形に書き換える（`poc/_env.py` の `load_env()` パターンを参照）。
2. 修正をコミットする。**この時点ではまだ履歴に古い値が残っているため、リポジトリは安全になっていない**。

---

## 3. Gitの履歴からの除去

```bash
pip install git-filter-repo
```

```bash
cat > /tmp/replacements.txt <<'EOF'
古い値1==>***REMOVED***
古い値2==>***REMOVED***
EOF

python -m git_filter_repo --replace-text /tmp/replacements.txt --force
```

* `git-filter-repo` は安全装置として `origin` リモートを自動的に削除する。実行後は再度 `git remote add origin <URL>` してから `git push --force origin main` で反映する。
* **force push はリモートの履歴を書き換える破壊的操作である。** 他の人がこのリポジトリをcloneしている場合は事前に連絡すること（個人リポジトリであれば問題ないことが多い）。
* 実行後、`git log --all -p | grep <漏洩した値>` で完全に消えたことを確認する。

> ⚠️ 履歴から消しても、**漏洩した値自体が無効化されたわけではない**。次の手順で必ずローテーションする。

---

## 4. Tesla認証情報のローテーション

Tesla Developer Portalには「Client Secretのみ再生成」する機能がない（2026年時点）。そのため、**新しいアプリを作り直す**形でローテーションする。

1. **新しいアプリを作成**：旧アプリと同じ設定（アプリ名・説明・OAuth付与タイプ・許可された送信元・リダイレクトURI・スコープ）で新規作成し、新しいClient ID/Secretを取得する。
2. **新しい鍵ペアを生成**：`openssl ecparam -name prime256v1 -genkey -noout -out private-key.pem` 等（`docs/00_onboarding.md` Phase 3参照）。
3. **公開鍵をホスティング先（GitHub Pages等）で更新**：`.well-known/appspecific/com.tesla.3p.public-key.pem` の内容を新しい公開鍵に置き換える。
4. **`tesla_config.json` のCLIENT_ID/CLIENT_SECRETを新しい値に書き換え、`tesla_tokens.json` を削除**して再認証フローを通す（`tesla_solar_charger.py` が自動的に認可URLを発行する）。
5. **パートナーアカウント登録**：`poc/activate_tesla_app.py` を新しいClient ID/Secretとドメインで実行する。
   * このとき公開鍵のホスティング更新が反映される前に実行すると `Public key hash has already been taken` で失敗する。旧アプリのアーカイブと公開鍵の差し替えの両方が必要。
6. **旧アプリをアーカイブ申請**：新アプリでの動作確認が完全に取れてから行う（先にアーカイブすると、新アプリの設定が終わるまで車両制御が完全に止まる）。
7. **車両への鍵承認（Virtual Key Pairing）**：`tesla-control add-key-request` はBLE必須でWindows非対応のため使えない。スマートフォンのTeslaアプリで `https://www.tesla.com/_ak/<ドメイン>` を開いて承認する（詳細：`docs/00_onboarding.md` Phase 4）。

## 5. Nature Remoアクセストークンのローテーション

[home.nature.global](https://home.nature.global) でアクセストークンを再発行し、`tesla_config.json` の `REMO_ACCESS_TOKEN` を書き換える。Tesla側と異なり、トークンの再生成自体は即時可能。

---

## 6. ラズパイ（本番環境）への反映

新しい認証情報・鍵・スクリプトをWindows側で検証できたら、本番環境にも反映する。

1. ラズパイ側の旧ファイル（`tesla_config.json` / `tesla_tokens.json` / 鍵ファイル）をバックアップ
2. 新しい `tesla_config.json` / `tesla_tokens.json` / 鍵ファイル（`tesla_app_key.pem` にリネーム、`docs/02_deploy.md` 参照）を転送
3. パーミッションを修復（`chmod 600` 等）
4. `tesla-proxy.service` / `tesla-charger.service` を再起動し、ログで車両認識（VIN取得）まで確認
5. `docs/DEPLOY_LOG.md` に対応内容を記録

---

## 7. 再発防止チェックリスト

* [ ] 新しい実証用スクリプトを書くときは、認証情報を最初から環境変数（`poc/.env`）またはgitignore対象ファイルから読み込む
* [ ] コミット前に `git diff --cached` で機密情報が混入していないか目視確認する
* [ ] 公開リポジトリ化する前に、全履歴に対して既知の漏洩パターンで再検索する（`git log --all -p | grep -iE "secret|token|password"`）
