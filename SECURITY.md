# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability reporting: open the [Security tab](https://github.com/Haraheriz/tesla-solar-charge/security) of this repository and select **Report a vulnerability**. Do not open a public issue or pull request for a vulnerability.

A useful report states the affected file or component, the steps to reproduce, and the impact you expect.

## Scope

- Only the latest commit on `main` is supported.
- This is a personal project that controls the charging of one vehicle at one home. Reports are read and answered, but no response or fix time is promised.
- Files under `poc/` are hand-run setup and verification scripts, not part of the running service. Reports about them are still welcome.

## Security measures

The measures taken so far, with the reason for each and the pull request or commit that made it, are listed in [docs/06_security_hardening.md](./docs/06_security_hardening.md) (Japanese).

---

# セキュリティポリシー

## 脆弱性の報告

脆弱性は、GitHub の非公開の脆弱性報告（Private vulnerability reporting）で報告してください。このリポジトリの [Security タブ](https://github.com/Haraheriz/tesla-solar-charge/security) を開き、**Report a vulnerability** を選びます。脆弱性を公開の Issue や Pull Request に書かないでください。

報告には、該当するファイルまたは機能、再現手順、想定される影響を書いてください。

## 対象

- 対象は `main` の最新のコミットだけです。
- 本プロジェクトは、1台の車両の自宅での充電を制御する個人のプロジェクトです。報告は読んで返信しますが、返信や修正の期限は約束しません。
- `poc/` 以下は手動で実行する設定・確認用のスクリプトで、常駐するサービスには含まれません。これらに関する報告も受け付けます。

## これまでの対策

これまでに行った対策を、理由と根拠の PR・コミットとともに [docs/06_security_hardening.md](./docs/06_security_hardening.md) に一覧にしています。
