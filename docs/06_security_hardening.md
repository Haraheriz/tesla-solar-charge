# 6. セキュリティ対策の記録

本書は、このリポジトリで実施したセキュリティ対策を、目的と根拠（PR・コミット）とともに一覧にしたものである。各項目の詳細は、リンク先の PR の説明文とコミットメッセージにある。

脆弱性の報告方法はリポジトリ直下の [SECURITY.md](../SECURITY.md) にある。

本書には、運用環境のネットワーク構成・ホスト名など、攻撃の手掛かりになる具体的な情報は書かない。

---

## 1. 守る対象と想定する脅威

| 守る対象 | 想定する脅威 |
| --- | --- |
| Tesla Fleet API のアクセストークン・リフレッシュトークン、アプリの Client Secret、車両コマンド署名用の秘密鍵 | リポジトリへの誤コミット、同じ端末の他ユーザーによる読み取り、ログ・中継機器への記録 |
| Nature Remo のアクセストークン | 同上 |
| 操作サーバー（スマートフォンから充電モードを切り替える HTTP サーバー）の `CONTROL_TOKEN` | URL・ログからの漏洩、総当たり、他サイトへの埋め込み |
| 操作サーバーの可用性 | 接続を長時間占有するクライアントによる、正規の操作の拒否 |
| Tesla・Nature Remo との通信 | TLS 検証の無効化による通信先のなりすまし |
| CI（GitHub Actions） | 参照先アクションの差し替え、secrets を持つジョブでの PR コードの実行 |

---

## 2. 対策の一覧

### 2.1 通信と TLS

| 対策 | 理由 | 根拠 |
| --- | --- | --- |
| クラウド API（Tesla・Nature Remo）の呼び出しで無効にしていた TLS 検証を有効にし、標準の CA バンドルで検証する | 検証を無効にすると、通信先のなりすましを検出できない | [PR #1](https://github.com/Haraheriz/tesla-solar-charge/pull/1) |
| ローカルの Tesla コマンドプロキシへの通信は、自己署名証明書 `cert.pem` をピン留めして検証する | 公的 CA の証明書を持たない通信先でも、検証を省かずに接続する | [PR #1](https://github.com/Haraheriz/tesla-solar-charge/pull/1) |
| 観測ツール `tools/observe_only.py` の TLS 検証を有効にし（2026-08-09）、ホスト名検証の除外も外す（2026-10-26） | 本体と同じベアラートークンを送る経路が、本体より弱い検証になっていた | [コミット 27f03e1](https://github.com/Haraheriz/tesla-solar-charge/commit/27f03e1)（[PR #15](https://github.com/Haraheriz/tesla-solar-charge/pull/15)）、[PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |

### 2.2 認証とトークン

| 対策 | 理由 | 根拠 |
| --- | --- | --- |
| OAuth の `state` を固定値 `12345` から実行ごとの乱数（`secrets.token_urlsafe(32)`）にし、コールバックで照合する | 固定値では CSRF・認可コードの差し込みを防げない | [PR #1](https://github.com/Haraheriz/tesla-solar-charge/pull/1) |
| `CONTROL_TOKEN` が文字列でない、32文字未満、テンプレートの値のまま、または表示可能な ASCII（0x21〜0x7E）以外を含む場合は、操作サーバーを起動しない | 推測しやすい値や、ヘッダーで送れない値での運用を防ぐ | [PR #46](https://github.com/Haraheriz/tesla-solar-charge/pull/46)、[PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| トークンの照合に `hmac.compare_digest` を使い、UTF-8 のバイト列で比較する | 照合時間からの推測を防ぐ。文字列のまま比較すると、ASCII 以外の文字を含む要求で例外になり、応答なしで切断されていた | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| 画面の JavaScript が API を呼ぶとき、トークンを URL のクエリではなく `X-Control-Token` ヘッダーで送る | URL は中継機器のアクセスログに記録される | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| トークンを画面に埋め込む箇所ごとに符号化を分ける（URL は percent-encoding、script 内は JSON と `</` の置換） | 文脈に合わない符号化は、値によって画面を壊す | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| `CONTROL_TOKEN` が漏洩したときの交換手順を文書にする | 他のトークンには交換手順があったが、このトークンには無かった | [PR #3](https://github.com/Haraheriz/tesla-solar-charge/pull/3)（`docs/03_operation.md`） |

### 2.3 操作サーバー

| 対策 | 理由 | 根拠 |
| --- | --- | --- |
| POST 本文を 4096 バイトまでに制限する | 大きな本文によるメモリ消費を防ぐ | [PR #46](https://github.com/Haraheriz/tesla-solar-charge/pull/46) |
| 1回の受信のタイムアウトを 5 秒、同時処理を 16 接続までにする | 接続を開いたまま送らないクライアントが、処理を占有し続けることを防ぐ | [PR #46](https://github.com/Haraheriz/tesla-solar-charge/pull/46) |
| 1リクエスト全体に 15 秒の期限を設け、超えた接続を閉じる | 5 秒は受信1回ごとの値なので、5 秒未満の間隔で1バイトずつ送る接続（slowloris 型）には効かない。16 接続でこれを行うと、正規の操作が拒否されていた | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| 切替の値に真偽値以外を受け付けない | 想定外の型の入力で状態を変えない | [PR #46](https://github.com/Haraheriz/tesla-solar-charge/pull/46) |
| すべての応答に `Cache-Control: no-store`、`Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff` を付ける | トークンを含むページのキャッシュ、Referer での送出、MIME の推測を防ぐ | [PR #46](https://github.com/Haraheriz/tesla-solar-charge/pull/46) |
| すべての応答に `X-Frame-Options: DENY` と `Content-Security-Policy: frame-ancestors 'none'` を付ける | 他サイトの iframe への埋め込み（クリックジャッキング）を防ぐ | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| 拒否（4xx）・失敗（5xx）・タイムアウト・同時接続数の上限による拒否をログに記録し、ログに書く文字列ではトークンを伏せ、制御文字をエスケープする | 不正なアクセスや接続の占有が起きたとき、記録から確認できるようにする。ログ自体からトークンが漏れないようにする | [PR #55](https://github.com/Haraheriz/tesla-solar-charge/pull/55)（主目的は障害調査） |

### 2.4 認証情報ファイル

| 対策 | 理由 | 根拠 |
| --- | --- | --- |
| 実証用スクリプトに直接書かれていた Tesla の Client ID・Client Secret と Nature Remo のアクセストークンを、環境変数から読むように変える | 認証情報をソースコードに置かない（第3章） | [コミット d37b575](https://github.com/Haraheriz/tesla-solar-charge/commit/d37b575) |
| トークンファイル `tesla_tokens.json` を権限 0600 で作成する | プロセスの umask に依存せず、所有者だけが読めるようにする。書き込みは `tesla_auth.py` の `save_token_file()` が行う | [PR #1](https://github.com/Haraheriz/tesla-solar-charge/pull/1) |
| トークン更新のあいだ `tesla_tokens.json.lock` を 0600 で取り、充電制御と観測ツールが同時にリフレッシュしない | リフレッシュトークンは使用のたびに置き換わる。同時に使うと片方のトークンが無効になる。ロックを取れない観測ツールはファイルを読み直す | [PR #73](https://github.com/Haraheriz/tesla-solar-charge/pull/73) |
| 操作状態の更新のあいだ `override_state.json.lock` を 0600 で取り、読み始めてから `os.replace` まで放さない | 2プロセスが同じ内容を書いて、フル充電モードの解除か目標充電率が消えることを防ぐ。Tesla API を待っているあいだはロックを持たない | [PR #69](https://github.com/Haraheriz/tesla-solar-charge/pull/69) |
| 実証用スクリプトが作る秘密鍵ファイルを所有者だけが読める権限で作成し、既存の鍵ファイルにグループ・他者の権限があれば処理を止める | 秘密鍵が同じ端末の他ユーザーから読める状態を防ぐ | [PR #46](https://github.com/Haraheriz/tesla-solar-charge/pull/46) |
| `tesla_config.json` と `tesla_tokens.json` が所有者以外から読める権限なら、起動時に `chmod` の手順を含む警告をログに出す | デプロイ手順で 0600 にする作業が漏れても、気付けるようにする。充電制御が止まる影響の方が大きいため、起動は続ける | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| AI コーディングアシスタントが認証情報ファイル（`tesla_tokens.json`、`tesla_config.json`、`*.pem`）を読み込まないよう、Read を拒否するフックを設定する | 作業中に認証情報がアシスタントへ送られることを防ぐ。フックの設定は端末ごとの設定として管理する | [コミット 494b865](https://github.com/Haraheriz/tesla-solar-charge/commit/494b865)、[コミット 2f77355](https://github.com/Haraheriz/tesla-solar-charge/commit/2f77355)、[PR #40](https://github.com/Haraheriz/tesla-solar-charge/pull/40) |

### 2.5 CI（GitHub Actions）

| 対策 | 理由 | 根拠 |
| --- | --- | --- |
| ワークフローの `uses:` をタグではなくコミット SHA で固定する | タグは作者が別のコミットへ付け替えられる | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| `requirements-dev.txt` の版を `==` で固定し、アクションと Python パッケージの更新 PR を Dependabot が毎月出すようにする | 固定した版を放置せず、更新を PR として確認してから取り込む | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |
| 自動レビュー（`claude-review.yml`）は `pull_request_target` で main のワークフローファイルを使い、同じリポジトリのブランチからの PR に限って動かす。PR の head は認証情報を残さずに別ディレクトリへ取り出し、読み取りだけを許す | secrets を持つジョブで、PR 側が変更したワークフローや PR のコードを実行しない | [PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47)、[PR #50](https://github.com/Haraheriz/tesla-solar-charge/pull/50) |
| 自動レビューにはテストを実行させない（テストは secrets を持たない `tests.yml` が実行する） | 公開リポジトリで、`pytest` は PR 内の任意のコードを実行する | [PR #26](https://github.com/Haraheriz/tesla-solar-charge/pull/26) |
| 自動レビューの観点に、認証情報・トークン・秘密鍵の漏洩を含める | このリポジトリは Tesla の認証情報と証明書を扱う | [PR #4](https://github.com/Haraheriz/tesla-solar-charge/pull/4) |

---

## 3. インシデント対応の記録

2026-06、開発初期の実証用スクリプトに Tesla の Client ID・Client Secret と Nature Remo のアクセストークンが直接書かれたまま、GitHub にコミットされていたことが判明した。リポジトリは当時 private だった。次の順で対応した。

1. 値を環境変数から読むように変える（[コミット d37b575](https://github.com/Haraheriz/tesla-solar-charge/commit/d37b575)、2026-06-20）。
2. `git-filter-repo` で Git の履歴から値を除去する。
3. Tesla のアプリを作り直し（Client Secret だけを再発行する手段が無いため）、Client ID・Client Secret・車両コマンド用の鍵ペアを新しくする（2026-06-20）。Nature Remo のアクセストークンを再発行する（2026-06-21）。経過は [DEPLOY_LOG.md](./DEPLOY_LOG.md) の 2026-06-20・2026-06-21 の項にある。
4. 対応の手順を、再発時に使える手順書として残す（[docs/04_security_incident_response.md](./04_security_incident_response.md)、[コミット ba8fd85](https://github.com/Haraheriz/tesla-solar-charge/commit/ba8fd85)）。

---

## 4. 意図的に行っていないこと

| 行っていないこと | 理由 | 根拠 |
| --- | --- | --- |
| 操作サーバーをインターネットへ公開すること（Tailscale Funnel を使わない） | 操作の対象は車両の充電で、防御は `CONTROL_TOKEN` の1つだけである。トークン漏洩・総当たりのリスクが公開の利点を上回る。宅外からは、スマートフォンを同じ tailnet に参加させて操作する | [PR #3](https://github.com/Haraheriz/tesla-solar-charge/pull/3)（`docs/01_architecture.md`） |
| secrets を持つ CI ジョブで PR のコードを実行すること | 2.5 のとおり | [PR #26](https://github.com/Haraheriz/tesla-solar-charge/pull/26)、[PR #47](https://github.com/Haraheriz/tesla-solar-charge/pull/47) |

---

## 5. 本書の更新

セキュリティに関わる変更をマージしたら、該当する章の表に1行追加する。行には「対策・理由・根拠（PR またはコミット）」を書く。
