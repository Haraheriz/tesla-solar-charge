# デプロイ履歴

ラズパイへの本番デプロイを行った際の簡易記録。形式は自由、最新のものを上に追加する。

## バージョン管理の方針

軽量なSemVer（`vMAJOR.MINOR.PATCH`）のGitタグを使う。厳密な運用ルールは設けず、目安として以下の通りとする。

* 機能追加 → MINORを上げる（例: `v0.1.0` → `v0.2.0`）
* バグ修正・ドキュメント修正のみ → PATCHを上げる（例: `v0.1.0` → `v0.1.1`）
* **ラズパイへ実際にデプロイしたコミットにタグを打つ**ことで、「今ラズパイで動いているのはどのバージョンか」を `git tag` / `git describe` で確認できるようにする
* **タグを打ったら、必ず同じ内容で `gh release create` も行う。** `git tag` を push するだけではGitHubの「Releases」には反映されず（タグとReleaseは別物）、`v0.1.1`〜`v0.2.1`が一時的にタグのみでReleaseが無い状態になり、GitHub上の「Latest」表示が古いまま（`v0.1.0`）になっていたことがあった。コマンド例：
  ```bash
  gh release create vX.Y.Z --title "vX.Y.Z - 概要" --notes "英語の説明文

  ---

  日本語の説明文"
  ```
* 初回タグ：`v0.1.0`（認証情報のローテーション、`poc/`の再編成、ドキュメント整理を終えた時点のスナップショット）

---

## 2026-07-05（充電制御の安定化：ヒステリシス・デバウンス・平滑化ほか）

**タグ：** `v0.3.0`

**内容：** 「3A指令時にバッテリーが実質充電されない」問題の調査結果を受けた制御ロジック改善一式を `tesla_solar_charger.py` と `tesla_config.json` に反映。

**理由：** ログ調査により、開始・停止閾値が1本（`MIN_AMPS×200W`）しかないため、余剰ギリギリで充電開始→車両自身の消費で余剰が消えて3分後に即停止、という発振が発生（7/4は3Aセッションが3分/21分/3分の計約27分のみ＝正味SOC 0.1〜0.2%）。加えて、ケーブル未接続（`Disconnected`）でも充電コマンドを送り続ける問題、5A未満の`set_charging_amps`が1回では反映されないTesla APIの癖（7/4 11:32に3A指令成功ログ後も車両は5A）を確認。

**作業内容：**
- ヒステリシス導入：開始・車両起動は `START_AMPS`（デフォルト `MIN_AMPS+2`）以上の余剰を要求、停止判定は `MIN_AMPS` 未満
- 停止判定のデバウンス：余剰不足が2サイクル（`STOP_DEBOUNCE_CYCLES`）連続したときだけ停止。猶予サイクル中は電流を `MIN_AMPS` まで下げて買電を抑制
- Remo E瞬時電力の平滑化：10秒間隔×3回サンプリングの平均値で判定
- `Disconnected` 時は充電コマンド送信をスキップし10分待機
- 5A未満の `set_charging_amps` は2回送信（既知の癖対策）
- `MIN_AMPS` をコードデフォルト・ローカル/ラズパイ両方の `tesla_config.json` ともに 3→4 に変更（600Wでは車載オーバーヘッド比率が高く充電効率が悪いため）
- 夜間休止モードに入る最初のサイクルで、充電中なら `charge_stop` を送ってから休止するよう追加（FORCE_RUN検証時に「充電中のまま18:00を跨ぐと朝まで系統充電が続く」穴が判明したため。2サイクル目以降は就寝中の車両を起こさないよう車両へ問い合わせない）
- `tesla_solar_charger.py` を転送し、`tesla-charger.service` を再起動

**結果：** `tesla-charger.service` `active (running)`、トークン自動リフレッシュ・車両認識（VIN取得）・夜間休止モード移行まで正常動作を確認。

**検証（2026-07-05 17:27〜17:54、FORCE_RUNモード）：** 雨天のため実余剰の代わりに仮想電力値9シナリオを注入して実車で検証し、全項目合格。①開始閾値1200W（6A分）での起動・6A充電開始、②バランス維持、③6A→4A降圧で車両実測も4A（5A未満2回送信の効果を確認、以前の「5Aに化ける」問題解消）、④余剰不足1/2回目で充電継続（デバウンス猶予）、⑤余剰回復でカウンタリセット、⑥⑦2サイクル連続不足で『一時停止』、⑧余剰900W（4.5A分）では再開せず待機（ヒステリシス）、⑨1300Wで6A再開。テスト終了時はcharge_stopを手動送信して車両停止を確認後、サービス復帰（18:00の夜間休止直前のため本番デバウンスによる自然停止は待たず安全側で処理）。

---

## 2026-06-27（ドキュメント整備・アイコン同期・設定共有）

**タグ：** `v0.2.1`

**内容：** `v0.2.0`以降に積まれた、本番の動作には影響しないPATCHレベルの変更4件をまとめてタグ付け（[PR #3](https://github.com/Haraheriz/tesla-solar-charge/pull/3)、および直接mainへの3コミット）。

**理由：** 機能追加を伴わない、ドキュメント・リポジトリ整合性の修正が`v0.2.0`タグ以降に複数積まれており、バージョン管理方針（バグ修正・ドキュメント修正のみ→PATCHを上げる）に従ってタグを打ち忘れていた。

**作業内容：**
- `.claude/settings.json`（秘密ファイルRead拒否フック）をコミットし、別端末でも共有されるようにした
- アイコン生成スクリプト（`tools/gen_icon.py`）を新規コミットし、リポジトリ内の`icons/*.png`が以前のセッションでラズパイへscp直接反映した再デザイン版と不一致だったのを同期・修正
- `docs/01_architecture.md`にTailscale Serveの構成とFunnel不採用の設計判断を追記
- `docs/03_operation.md`に`show_control_url.sh`の使い方と`CONTROL_TOKEN`漏洩時のローテーション手順を追記

**結果：** ラズパイ側の本番サービスへの変更は無し（ドキュメント・リポジトリ整合性のみ）。

---

## 2026-06-27（マニュアル・オーバーライド機能 + PWA対応 + UI改善）

**タグ：** `v0.2.0`

**内容：** スマホから切替え可能なマニュアル・オーバーライド（フル充電モード）とPWA対応をラズパイへ初回フルセットアップし、デプロイ時に見つかった不具合・UI課題を修正してmainへ統合（[PR #2](https://github.com/Haraheriz/tesla-solar-charge/pull/2)）。

**理由：**
- `claude/solar-charging-control-app-2vjyvj`ブランチで開発済みだったマニュアル・オーバーライド機能（`control_server.py` / `override_state.py`）とPWA対応（マニフェスト・Service Worker・アイコン）が、ラズパイには一度も配置されていなかった（`tesla-override.service`未登録、`tesla_config.json`にCONTROL_PORT/CONTROL_TOKEN未設定）
- 初回フルセットアップ後の動作確認で、オーバーライドON中に手動でTeslaアプリから充電電流を下げても、スクリプトが毎サイクルMAX_AMPSへ強制的に戻してしまう不具合を発見
- 操作画面で、iPhoneのDynamic Islandに文字が隠れる・ボタンが左にずれる・ON/OFF表記が状態と結果のどちらを示すか曖昧、といったUI課題が見つかった

**作業内容：**
- `control_server.py` / `override_state.py` / `tesla_solar_charger.py`（オーバーライド対応版）/ `icons/icon-192.png` / `icons/icon-512.png` をラズパイへ転送
- `tesla_config.json`に`CONTROL_PORT`（8090）と新規生成した`CONTROL_TOKEN`を追加
- `/etc/systemd/system/tesla-override.service`を新規作成し`enable`・`start`、`tesla-charger.service`も再起動
- Tailscaleをラズパイに導入し、`tailscale serve`でコントロールサーバーをtailnet内HTTPS公開（Funnelは意図的に無効のまま）。AndroidでのPWAインストールにHTTPSが必要なための対応
- `tesla_solar_charger.py`のオーバーライド分岐を修正：充電停止中からの再開時のみMAX_AMPSを設定し、充電中は車両側の現在値（手動変更分）を維持するように変更
- `control_server.py`の操作画面を改善：`env(safe-area-inset-*)`によるセーフエリア対応、iOS HIG/Material Design基準のフォントサイズ拡大、状態表示とボタンの役割分離（解決策A：状態は常に事実、ボタンは常に未来のアクションのみ）、WAI-ARIA対応（`role="status"` `aria-live` `aria-pressed`等）、中央揃えのレイアウト修正、ページタイトル・見出し・PWAマニフェストの表示名を「Tesla充電切替」に統一
- `show_control_url.sh`を追加（`qrencode`導入）し、操作用URLとQRコードを端末に表示できるようにした
- `claude/solar-charging-control-app-2vjyvj`をmainへマージし、上記の修正一式をコミットしてPR化

**結果：** `tesla-proxy.service` / `tesla-charger.service` / `tesla-override.service`の3つすべて`active (running)`。HTTPS経由（`https://raspi4-12.taila049aa.ts.net/`）で200 OKを確認。手動変更した充電電流が次サイクルで上書きされないことを確認。

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
