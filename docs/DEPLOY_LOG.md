# デプロイ履歴

ラズパイへの本番デプロイを行った際の簡易記録。形式は自由、最新のものを上に追加する。

---

## 2026-06-20

**内容：** Tesla Client ID/Secret・車両コマンド用鍵ペアのローテーション後、ラズパイへ反映。

**理由：** 開発初期のPoCスクリプトに認証情報がハードコードされGitHub（Private）にコミットされていたため、念のため新しいTeslaアプリ（`Solar Charge Optimizer`）を作成し、Client ID/Secret・鍵ペアを再発行。ラズパイは旧アプリ（アーカイブ済み）の認証情報で稼働を続けていたため、新しい認証情報・鍵・`tesla_solar_charger.py`・`tesla-http-proxy`（ARM64再ビルド）に入れ替えた。

**作業内容：**
- 旧ファイル（`tesla_config.json` / `tesla_tokens.json` / `tesla_app_key.pem` / `tesla-http-proxy` / `tesla_solar_charger.py`）を `tesla-solar-charge-backup-20260620/` にバックアップ
- 新しい鍵ペア（Windows側 `private-key.pem` → ラズパイ側 `tesla_app_key.pem` にリネーム）を配置
- `tesla-proxy.service` / `tesla-charger.service` を再起動し、車両認識（VIN取得）まで確認

**結果：** 両サービス `active (running)`、正常稼働を確認。
