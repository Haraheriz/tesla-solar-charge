# 3. 運用・保守・トラブルシューティングマニュアル（日常管理の取扱説明書）

## 1. 日常の稼働監視とログの確認方法

本システムはバックグラウンドで完全自律稼働するため、通常はオペレーションを必要としない。システムの稼働状態や挙動をモニタリング・監査する場合は、以下の手順で行う。

### 1. サービスの生存確認（ヘルスチェック）

システムを構成する2つのサービスが正常に常駐しているかをワンラインで確認する。

```bash
sudo systemctl status tesla-proxy.service tesla-charger.service

```

* **正常時の状態：** 両方のサービスに緑色の文字で **`active (running)`** と表示されていること。
* **異常時の状態：** 赤文字で `failed` または `activating (auto-restart)` となっている場合は、何らかのエラーで即死ループが発生している（「3. トラブルシューティング」を参照）。

### 2. 充電制御スクリプトのリアルタイムログ確認

3分ごとに実行される太陽光余剰電力の演算、および車両への電流変更指令の履歴をリアルタイムに追跡（ストリーム表示）する。

```bash
tail -f /home/<username>/tesla-solar-charge/tesla_solar_charger.log

```

* ※ログ表示を終了するには `Ctrl + C` を押す。

#### ログ出力の解釈基準

本システムのログは以下の3つの時間帯・モードに分けて明瞭に出力される。生成AIにログをパースさせる際も、この3パターンのいずれかに分類される。

* **パターンA：昼間・充電追従稼働中（発電あり）**
```text
[2026-05-17 12:00:00] [INFO] --- 定期チェック開始 ---
[2026-05-17 12:00:01] [INFO] 車両状態: 『online』 (RemoE瞬時電力: -3500 W)
[2026-05-17 12:00:02] [INFO] 演算状況 → 目標: 16A (車両現在値: 5A / ステータス: Charging)
[2026-05-17 12:00:03] [INFO] 電流を変更調整: 5A → 16A

```


* **パターンB：昼間・車両スリープ保護中（Insomnia Defense作動）**
```text
[2026-05-17 13:15:00] [INFO] --- 定期チェック開始 ---
[2026-05-17 13:15:01] [INFO] 車両キャッシュ状態が『asleep』のため、車両を起こさず処理をスキップします。

```


* **パターンC：夜間・システム休止モード中（18:00〜翌朝07:00）**
```text
[2026-05-17 19:30:00] [INFO] --- 定期チェック開始 ---
[2026-05-17 19:30:00] [INFO] 夜間休止モード中（現在時刻 19:30:00）
[2026-05-17 19:30:00] [INFO] 次の稼働チェックまで10分間スリープします...

```



---

## 2. 夜間・任意タイミングでの動作確認（`--force-run`）

充電制御ロジック（余剰検知 → 車両起動 → 充電開始/停止 → 電流調整）は、太陽光が発電していない夜間や、Nature Remoの実測値が0の状態では検証できない。これを回避するため、`tesla_solar_charger.py` には動作確認専用のモードが用意されている。

```bash
python tesla_solar_charger.py --force-run
```

（`FORCE_RUN=1` 環境変数でも同様に有効化できる。）

* **夜間休止モード（7:00-18:00以外は停止）を無視**して常時稼働する。
* 各サイクルごとに、画面で仮想の家庭消費電力（W）を入力できる（**負の値＝売電中・余剰あり**、**正の値＝買電中**）。空Enterなら実測値を使う。

```text
[FORCE_RUNモード] 仮想の家庭消費電力(W)を入力（負の値＝売電中/余剰あり、空Enterで実測値を使用）: -3000
```

* これにより、深夜でも余剰電力を自由に変化させながら、車両の起動・充電開始・電流調整・充電停止までの一連の挙動を実機で確認できる。
* **動作確認専用のモードであり、本番運用（`tesla-charger.service`）では使用しない。** ラズパイのsystemdサービスは常に通常モード（`--force-run` なし）で起動する。

---

## 3. スマホからの「フル充電モード」切替（マニュアル・オーバーライド）

太陽光の発電状況に関わらず充電したい場合（来客時の急ぎ充電、出発前の追加充電など）に使う機能。`tesla-control.service` が宅内LAN上で軽量Webサーバーとして常駐しており、スマートフォンのブラウザからトークン付きURLにアクセスするだけでON/OFFを切替えられる。

### 使い方

1. **初回のみ：URLをブックマーク／ホーム画面に追加**
ラズパイのIPアドレスと、`tesla_config.json` に設定した `CONTROL_TOKEN` を使って以下の形式のURLを開く。

```text
http://<ラズパイのIPアドレス>:8090/?token=<CONTROL_TOKEN>

```

iPhoneのSafariなら共有メニューから「ホーム画面に追加」、AndroidのChromeなら「ホーム画面に追加」を選ぶと、アイコンタップだけでアプリのように開けるようになる。

2. **トグルボタンをタップ：**
画面中央の丸いボタンをタップすると「フル充電モード：ON」に切替わり、太陽光の余剰計算を無視して `MAX_AMPS` でのフル充電が始まる（車両が就寝中の場合は自動で起動する）。もう一度タップすると通常の太陽光追従モードに復帰する。

3. **状態確認：**
ページは5秒ごとに自動で状態を再取得するため、他の場所（PC等）から切替えた場合でも画面を開けば最新状態が反映される。

### 注意点

* 宅外からアクセスする場合は、ルーターのポート開放ではなく **Tailscale等のVPN経由でのアクセスを推奨する**（`CONTROL_TOKEN` をURLに含めて宅外公開すると、漏洩時に第三者から充電を操作されるリスクがあるため）。
* `CONTROL_TOKEN` は `openssl rand -hex 32` 等で生成した推測不可能な値を `tesla_config.json` に設定すること。
* フル充電モードをONにしたまま放置すると、太陽光発電量に関わらず充電が継続する。出発後やOFF忘れに注意し、必要に応じて手動でOFFに戻すこと。

---

## 4. トークン失効・更新時の「初回手動認証方式」復旧手順

テスラのAPI認可トークン（`tesla_tokens.json`）が完全に失効した場合、あるいは初回認証を行う場合、Linux（ヘッドレス環境）からではブラウザセキュリティ（Private Network Access: PNA制限）により直接の認証着地がブロックされる。

これを完全に迂回するため、以下の「Windows側でトークンを取得し、Linuxへ転送する」手順を厳格に執行する。

### トークン更新の3ステップフロー

```text
【手元のWindows PC】                     【Linuxサーバー】
 1. スクリプトを一時的に直接実行
    (ブラウザが開き認証成功)
 2. tesla_tokens.json が生成 →→(SCP転送)→→ 3. 指定ディレクトリへ上書き配置
                                           4. サービス再起動で完全復旧

```

### 具体的実行コマンド

1. **手元のWindows側での作業：**
Windows側の作業フォルダ（Python環境がある場所）で、一時的にメインスクリプトを直接実行する。
```powershell
python tesla_solar_charger.py

```


自動的にブラウザが立ち上がり、テスラの公式ログイン画面が表示される。ログイン完了後、画面が白くなりWindowsのコンソール側に `[INFO] トークンを新規保存しました。` と表示されれば成功。フォルダ内に最新の `tesla_tokens.json` が生成される。
2. **Linux側への転送：**
生成された `tesla_tokens.json` を、Linux側の対象ディレクトリへ上書き転送（SCP等）する。
* 転送先：`/home/<username>/tesla-solar-charge/tesla_tokens.json`


3. **権限修復とサービス再起動：**
Linuxのコンソールに入り、以下のコマンドでトークンファイルのパーミッションを厳格化し、システムを再バインドする。
```bash
# 所有者のみ読み書き可能に制限（セキュリティ担保）
chmod 600 /home/<username>/tesla-solar-charge/tesla_tokens.json

# サービスをリスタートして新しいトークンを読み込ませる
sudo systemctl restart tesla-proxy.service tesla-charger.service

```



---

## 5. 緊急時のトラブルシューティング（障害対応）

### 事象1：プロキシが `status=1/FAILURE` で即死を繰り返す

* **想定原因A：** 過去に手動テストした際のプロキシプロセスがゾンビ化して裏で生き残っており、ポート `4443` を既に占有している。
* **対応策A：** 以下のコマンドで幽霊プロセスを強制パージ（殺害）した後に再起動する。
```bash
# 4443番ポートを掴んでいるプロセスID(PID)を特定してキル
sudo fuser -k 4443/tcp

# サービスを再起動
sudo systemctl restart tesla-proxy.service

```


* **想定原因B：** Windowsから鍵ファイルを転送した際、改行コードが Windows形式（`CRLF`）になっており、Linuxの暗号ライブラリがパースに失敗している。
* **対応策B：** 改行コードをLinux標準の `LF` に一発置換する。
```bash
sudo apt-get install dos2unix -y  # 未導入の場合のみ
dos2unix /home/<username>/tesla-solar-charge/*.pem
sudo systemctl restart tesla-proxy.service

```



### 事象2：Python側が `status=1/FAILURE` で即死する

* **想定原因：** 大前提であるGoプロキシ（4443番ポート）が何らかの理由で落ちているため、Python側が通信を拒否され（Connection Refused）自死している。
* **対応策：** まず `sudo journalctl -u tesla-proxy.service -n 20 --no-pager` を叩き、プロキシが死んでいる根本原因（鍵の不整合など、仕様書第5章を参照）を排除してプロキシ側を先に緑色の `running` に戻すこと。

---

## 6. 運用管理コマンド クイックリファレンス（チートシート）

日常のメンテナンスで保守担当者が使用する、主要コマンドの一覧である。

| 操作目的 | 実行コマンド |
| --- | --- |
| **全システムの現在の状態を見る** | `sudo systemctl status tesla-proxy.service tesla-charger.service tesla-control.service` |
| **全システムをまとめて起動する** | `sudo systemctl start tesla-proxy.service tesla-charger.service tesla-control.service` |
| **全システムを安全に完全停止する** | `sudo systemctl stop tesla-charger.service tesla-proxy.service tesla-control.service` |
| **設定変更後に一括リスタートする** | `sudo systemctl restart tesla-proxy.service tesla-charger.service tesla-control.service` |
| **プロキシ側の「生のエラー」を追跡する** | `sudo journalctl -u tesla-proxy.service -f --no-pager` |
| **Python側の「システムエラー」を追跡する** | `sudo journalctl -u tesla-charger.service -f --no-pager` |
| **スマホ操作用サーバーのログを追跡する** | `sudo journalctl -u tesla-control.service -f --no-pager` |
| **資産フォルダ全体の権限状態を確認する** | `ls -la /home/<username>/tesla-solar-charge/` |
