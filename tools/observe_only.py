#!/usr/bin/env python3
"""読み取り専用の観測ツール（車両へコマンドを一切送らない）。

2つのものを記録する。

1. **電力の生データ**（`observe_power.csv`）
   Nature Remo の瞬時電力を短い間隔でそのまま記録する。制御ループが記録するのは
   平滑化後の値をサイクル毎（180秒〜600秒）に1点だけで、その平均の元になった
   個々のサンプルは残らない。そのため「平滑化窓を延ばしたら結果がどう変わるか」を
   後から検証できない。生データがあれば任意の窓長を後からシミュレートでき、
   閾値超過がどれだけ継続したかの分布も出せる。
   充電中は車両自身の消費が電力値に乗るため、そのときの車両状態も併せて記録する。

2. **車両状態の変化**（`observe_only.log`）
   充電状態・SOC・スケジュールの推移。制御ログとは独立した記録として、
   「いつ充電が始まり、システムがいつ何を送ったか」を突き合わせるために使う。

車両へ送るPOSTは無い。charge_start / charge_stop / set_charging_amps / wake_up の
いずれも呼ばない。POSTするのは auth.tesla.com へのトークンリフレッシュだけで、
これは観測を継続するために必要なもの。

車両リスト（GET /api/1/vehicles）は課金対象外で車両も起こさない。vehicle_data は
車両が online のときだけ叩く（asleep/offline の車両は充電していないため、
起こしてまで確認する必要がない）。

Nature Remo APIにはレート制限がある（5分あたり30リクエスト程度）。制御ループ自身が
1サイクルにつき3回叩くため、ここの `SAMPLE_SEC` を短くしすぎると両方が429で失敗する。
既定の60秒で、両者あわせて5分あたり10回程度に収まる。
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# このファイルは tools/ に置かれるが、設定・トークン・証明書は本体と同じ場所
# （リポジトリ／デプロイ先のルート）にある。__file__ 基準にすると tools/ を指してしまう。
# 本体と同じく環境変数で上書きできる。**本体とトークンファイルを分けてはいけない。**
# リフレッシュのたびにトークンが更新されるため、別々のファイルを持つと片方が失効する。
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.environ.get("TESLA_CONFIG_PATH", os.path.join(BASE_DIR, "tesla_config.json"))
TOKEN_FILE = os.environ.get("TESLA_TOKEN_PATH", os.path.join(BASE_DIR, "tesla_tokens.json"))
PROXY_CERT_PATH = os.environ.get("TESLA_CERT_PATH", os.path.join(BASE_DIR, "cert.pem"))
LOG_FILE = os.path.join(BASE_DIR, "observe_only.log")
POWER_CSV = os.path.join(BASE_DIR, "observe_power.csv")
PROXY_HOST = "https://localhost:4443"
AUTH_URL = "https://auth.tesla.com/oauth2/v3/token"

SAMPLE_SEC = 60      # 電力の生データを取る間隔
VEHICLE_SEC = 600    # 車両状態を問い合わせる間隔

if not os.path.exists(CONFIG_FILE):
    sys.exit("設定ファイル（%s）が見つかりません。" % CONFIG_FILE)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config = json.load(f)

# 本体（proxy_session.verify = cert.pem）と同じく自己署名証明書をピン留めして検証する。
# 同じアクセストークンを載せる経路であり、検証を無効化する理由がない。
CTX = ssl.create_default_context(cafile=PROXY_CERT_PATH)
CTX.check_hostname = False  # 証明書のCNがlocalhostと一致しないため、ホスト名照合のみ外す


def log(message):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_sample(power, meter_updated_at, vehicle_state, charging_state):
    """電力の生データを1行追記する。ヘッダは初回のみ書く。"""
    new_file = not os.path.exists(POWER_CSV)
    with open(POWER_CSV, "a", encoding="utf-8") as f:
        if new_file:
            f.write("timestamp,power_w,meter_updated_at,vehicle_state,charging_state\n")
        f.write("%s,%s,%s,%s,%s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%S"),
            "" if power is None else power,
            meter_updated_at or "",
            vehicle_state or "",
            charging_state or "",
        ))


def load_tokens():
    with open(TOKEN_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_tokens(access, refresh, expires_in):
    """本体スクリプトと同じ形式・同じ余裕（1800秒）で原子的に書く。"""
    data = {
        "access_token": access,
        "refresh_token": refresh,
        "token_expires_at": time.time() + int(expires_in) - 1800,
    }
    tmp = TOKEN_FILE + ".observe.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, TOKEN_FILE)


def refresh_token():
    tok = load_tokens()
    payload = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": config.get("CLIENT_ID", ""),
        "client_secret": config.get("CLIENT_SECRET", ""),
        "refresh_token": tok.get("refresh_token", ""),
    }).encode()
    req = urllib.request.Request(AUTH_URL, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            body = json.load(res)
        save_tokens(body.get("access_token"), body.get("refresh_token"),
                    body.get("expires_in", 28800))
        log("トークンをリフレッシュしました。")
        return True
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            # 本体が過去に踏んだ穴と同じ。401のまま29日間・6,357回リトライし続け、
            # サービスは正常に見えていた。回復しない失敗は静かに繰り返さず止める。
            log("[CRITICAL] リフレッシュトークンが失効しています（HTTP %s）。"
                "観測を終了します。再認証は docs/03_operation.md 第4章。" % e.code)
            sys.exit(1)
        log("[ERROR] トークンのリフレッシュに失敗しました: HTTP %s" % e.code)
        return False
    except Exception as e:
        log("[ERROR] トークンのリフレッシュに失敗しました: %s" % e)
        return False


def get(path, access):
    req = urllib.request.Request(
        PROXY_HOST + path,
        headers={"Authorization": "Bearer " + access, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return None, str(e)


def remo_power():
    """瞬時電力(W)とスマートメーター側の更新時刻を返す。

    更新時刻も記録するのは、メーターが実際に何秒間隔で値を更新しているかを
    後から確認するため。同じ更新時刻の行が続いていれば、その間の値は
    メーターが更新していないだけで、電力が一定だったわけではない。
    """
    token = config.get("REMO_ACCESS_TOKEN", "")
    req = urllib.request.Request(
        "https://api.nature.global/1/appliances",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            appliances = json.load(res)
    except Exception:
        return None, None
    for a in appliances:
        sm = a.get("smart_meter")
        if not sm:
            continue
        for p in sm.get("echonetlite_properties", []):
            if p.get("epc") == 231:
                return int(p.get("val")), p.get("updated_at")
    return None, None


def poll_vehicle():
    """車両状態を1回問い合わせる。(vehicle_state, charging_state, 表示用の文字列) を返す。"""
    tok = load_tokens()
    if time.time() > tok.get("token_expires_at", 0):
        refresh_token()
        tok = load_tokens()
    access = tok.get("access_token", "")

    status, body = get("/api/1/vehicles", access)
    if status != 200 or not isinstance(body, dict):
        log("[WARN] 車両リスト取得に失敗 (status=%s)" % status)
        return None, None, None

    vehicles = body.get("response") or []
    vehicle = vehicles[0] if vehicles else {}
    state = str(vehicle.get("state", ""))
    if state != "online":
        return state, None, "車両=%s（充電していない状態。vehicle_dataは叩かない）" % state

    status, body = get(
        "/api/1/vehicles/%s/vehicle_data?endpoints=charge_state" % vehicle.get("vin"),
        access,
    )
    if status != 200 or not isinstance(body, dict):
        log("[WARN] charge_state 取得に失敗 (status=%s)" % status)
        return state, None, None

    cs = (body.get("response") or {}).get("charge_state") or {}
    summary = ("車両=%s 充電状態=%s SOC=%s%% 上限=%s%% 電流=%sA scheduled_mode=%s pending=%s"
               % (state, cs.get("charging_state"), cs.get("battery_level"),
                  cs.get("charge_limit_soc"), cs.get("charge_amps"),
                  cs.get("scheduled_charging_mode"), cs.get("scheduled_charging_pending")))
    return state, str(cs.get("charging_state") or ""), summary


def main():
    log("=" * 60)
    log("読み取り専用の観測を開始します（車両へのコマンドは送りません）。")
    log("電力の生データ: %s（%d秒間隔）" % (POWER_CSV, SAMPLE_SEC))
    log("=" * 60)

    previous_summary = None
    vehicle_state = None
    charging_state = None
    next_vehicle_poll = 0.0

    while True:
        try:
            if time.time() >= next_vehicle_poll:
                next_vehicle_poll = time.time() + VEHICLE_SEC
                state, charge, summary = poll_vehicle()
                if state is not None:
                    vehicle_state, charging_state = state, charge
                if summary and summary != previous_summary:
                    power_now, _ = remo_power()
                    log("%s / 系統電力=%s" % (
                        summary, "%s W" % power_now if power_now is not None else "取得失敗"))
                    previous_summary = summary

            power, meter_updated_at = remo_power()
            append_sample(power, meter_updated_at, vehicle_state, charging_state)
        except Exception as e:
            log("[ERROR] 観測ループで例外: %s" % e)
        time.sleep(SAMPLE_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("観測を終了します。")
        sys.exit(0)
