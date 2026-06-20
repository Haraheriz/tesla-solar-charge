# デプロイ履歴

ラズパイへの本番デプロイを行った際の簡易記録。形式は自由、最新のものを上に追加する。

---

## 2026-06-21

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
