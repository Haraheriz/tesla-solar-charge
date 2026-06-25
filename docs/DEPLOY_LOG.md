# デプロイ履歴

ラズパイへの本番デプロイを行った際の簡易記録。形式は自由、最新のものを上に追加する。

## バージョン管理の方針

軽量なSemVer（`vMAJOR.MINOR.PATCH`）のGitタグを使う。厳密な運用ルールは設けず、目安として以下の通りとする。

* 機能追加 → MINORを上げる（例: `v0.1.0` → `v0.2.0`）
* バグ修正・ドキュメント修正のみ → PATCHを上げる（例: `v0.1.0` → `v0.1.1`）
* **ラズパイへ実際にデプロイしたコミットにタグを打つ**ことで、「今ラズパイで動いているのはどのバージョンか」を `git tag` / `git describe` で確認できるようにする
* 初回タグ：`v0.1.0`（認証情報のローテーション、`poc/`の再編成、ドキュメント整理を終えた時点のスナップショット）

---

## 2026-06-25（セキュリティ強化: TLS検証・CSRF対策・パス解決・ファイル権限）

**タグ：** `v0.1.1`

**内容：** セキュリティレビューで見つかった4点を修正し、ラズパイへ反映（[PR #1](https://github.com/Haraheriz/tesla-solar-charge/pull/1)）。

**理由：**
- `requests`の`verify='cert.pem'`が、ローカルTeslaプロキシ宛・外部クラウドAPI宛の区別なく使われており、本来TLS検証すべき箇所の意図が不明確だった
- OAuthの`state`パラメータが固定値`"12345"`で、CSRF/認可コード差し替え対策として機能していなかった
- `cert.pem` / `tesla_config.json` / `tesla_tokens.json`のパスが相対パス指定で、実行時のカレントディレクトリに依存していた
- トークンファイルの書き込み権限がプロセスのumask依存で、明示的に制限されていなかった

**作業内容：**
- HTTPセッションを`proxy_session`（ローカルプロキシ用、`cert.pem`をピン留め検証）と`cloud_session`（外部API用、標準CA検証）に分離
- OAuth `state`を`secrets.token_urlsafe(32)`でランダム化し、コールバック時に検証
- `cert.pem` / `tesla_config.json` / `tesla_tokens.json`のパスを`__file__`基準の絶対パスに変更（`TESLA_CERT_PATH` / `TESLA_CONFIG_PATH` / `TESLA_TOKEN_PATH`で上書き可能）
- `tesla_tokens.json`を`0o600`権限で書き込むように変更
- `tesla_solar_charger.py`をラズパイへ転送し、`tesla-charger.service`を再起動。`--force-run`で`wake_up` / `charge_start` / `set_charging_amps` / `charge_stop`を実機確認

**結果：** `tesla-charger.service` `active (running)`。ローカルとラズパイのファイルがsha256で完全一致することを確認。

---

## 2026-06-21（Nature Remoトークン再発行）

**内容：** Nature Remo Cloud APIのアクセストークンを再発行し、ラズパイへ反映。

**理由：** 開発初期のPoCスクリプトにハードコードされ漏洩していたトークンが、`tesla_config.json` 上でそのまま使われ続けていたため。Tesla認証情報のローテーション時に対応していなかった残作業。

**作業内容：**
- [home.nature.global](https://home.nature.global) で新しいアクセストークンを発行（スコープは `basic` のみ。`sendir` / `echonetlite.*.read` / `nature_evcc` は本プロジェクトで未使用のため選択せず）
- ローカル（Windows側）の `tesla_config.json` の `REMO_ACCESS_TOKEN` を更新
- `scp` でラズパイへ転送、`tesla-charger.service` を再起動

**結果：** `tesla-charger.service` `active (running)`。夜間のため、Nature Remoへの実通信確認は日中の稼働確認時に持ち越し。

---

## 2026-06-21（表現クリーンアップ）

**内容：** ログ・コメント・docs内の表現クリーンアップ（絵文字除去、冗長表現・曖昧語句・誇張表現の修正）を `tesla_solar_charger.py` のみラズパイへ反映。

**理由：** 開発初期にGoogle Geminiと作業した際の癖（絵文字、「密輸型」という不適切な命名、「車両を叩き起こす」等の乱暴な表現、冗長・曖昧な言い回し）を一掃するスタイル修正。機能変更はなし。

**作業内容：**
- `tesla_solar_charger.py` を転送し、`tesla-charger.service` を再起動
- ログ出力が新しい表現（「初回手動認証方式」「保存済みトークンで再ログインします」等）になっていることを確認

**結果：** `tesla-charger.service` `active (running)`、車両認識まで正常稼働を確認。

---

## 2026-06-20

**内容：** Tesla Client ID/Secret・車両コマンド用鍵ペアのローテーション後、ラズパイへ反映。

**理由：** 開発初期のPoCスクリプトに認証情報がハードコードされGitHub（Private）にコミットされていたため、念のため新しいTeslaアプリ（`Solar Charge Optimizer`）を作成し、Client ID/Secret・鍵ペアを再発行。ラズパイは旧アプリ（アーカイブ済み）の認証情報で稼働を続けていたため、新しい認証情報・鍵・`tesla_solar_charger.py`・`tesla-http-proxy`（ARM64再ビルド）に入れ替えた。

**作業内容：**
- 旧ファイル（`tesla_config.json` / `tesla_tokens.json` / `tesla_app_key.pem` / `tesla-http-proxy` / `tesla_solar_charger.py`）を `tesla-solar-charge-backup-20260620/` にバックアップ
- 新しい鍵ペア（Windows側 `private-key.pem` → ラズパイ側 `tesla_app_key.pem` にリネーム）を配置
- `tesla-proxy.service` / `tesla-charger.service` を再起動し、車両認識（VIN取得）まで確認

**結果：** 両サービス `active (running)`、正常稼働を確認。
