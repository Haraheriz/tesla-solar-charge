import json
from typing import Any, Dict, Optional

import requests

# 自宅のTesla Wall Connector (Gen 3) のローカルAPIを読む。
#
# 車両側のAPIには「どの充電器に繋がっているか」を示す情報が一切ない。
# charge_state の全フィールドを確認したが、シリアル・DIN・サイトID・位置のいずれも
# 含まれず、conn_charge_cable はケーブル規格、fast_charger_* はDC充電器のみを表す。
# したがって車両側から自宅充電と外出先充電を判別することは原理的にできない。
#
# 一方このモジュールは宅内LANのウォールコネクターを直接読む。本質は個体シリアルの
# 照合ではなく、宅内LANから到達できること自体が「自宅の充電器である」ことの証明に
# なる点にある。車両が Charging を報告しているのに vehicle_connected が false なら、
# その充電は自宅ではない。DC・ACを問わず判別できる。
#
# Tesla Fleet API を一切使わないため、APIの呼び出し数も課金も増えず、
# OAuthスコープの追加も再認可も発生しない。
#
# 認証のない平文HTTPであり、宅内LANからのみ到達できる。エンドポイントは
# Teslaが公式に文書化したものではないため、いつ塞がれてもおかしくない。
# そのため本モジュールは「読めなかった」を異常として扱わず、UNKNOWN として返す。

# 判定結果。呼び出し側はこの3値だけを見る。
WC_CONNECTED: str = "connected"
WC_NOT_CONNECTED: str = "not_connected"
WC_UNKNOWN: str = "unknown"

# 給電しているかの判定結果。読めなければ WC_UNKNOWN を返す。
WC_DELIVERING: str = "delivering"
WC_NOT_DELIVERING: str = "not_delivering"

# 1回目の読み取りに失敗し、その場の取り直しで救われた回数。
#
# attempts=2 の再試行（PR #21）を入れて以降、1回目の失敗はどこにも現れなくなった。
# その再試行の根拠は「2026-08-11〜18 に読み取り失敗が週4回」という実測であり、
# 数えるのをやめると、失敗率の悪化に気づけるのは「2回とも失敗する」ようになってから
# になる。このモジュールはログを持たない方針なので、数だけ持って呼び出し側に渡す。
retry_saved_count: int = 0

# tesla_solar_charger.py の proxy_session / cloud_session とは用途が異なるため独立させる。
# proxy_session は自己署名証明書をピン留めしたTesla プロキシ専用であり、
# 宅内の平文HTTPとは無関係。テストではこの変数ごと差し替える。
wc_session = requests.Session()


def _get_json_once(host: str, path: str, timeout: float) -> Optional[Dict[str, Any]]:
    """ウォールコネクターのローカルAPIを1回読む。読めなければ None を返す。

    以下はすべて「読めなかった」として同じ None に畳む。呼び出し側が
    区別しても取れる対処が変わらないため。

      1. TCP接続が確立できない（電源断・Wi-Fi切断・IP変更・LAN障害）
      2. タイムアウト
      3. HTTPステータスが200以外
      4. レスポンスがJSONとして解釈できない
    """
    if not host:
        return None
    try:
        res = wc_session.get(f"http://{host}{path}", timeout=timeout)
    except Exception:
        return None
    if getattr(res, "status_code", None) != 200:
        return None
    try:
        data = res.json()
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _get_json(host: str, path: str, timeout: float, attempts: int = 2) -> Optional[Dict[str, Any]]:
    """読めなければその場で取り直す。夜間の night_proxy_get() と同じ考え方。

    2026-08-11〜18 の運用で、読み取り失敗が週4回発生した。定常時の応答は
    15〜42ms（ping平均13.6ms・ロス0%）であり、タイムアウトが短すぎるのではなく
    一過性の失敗である。制御サイクルは昼3分・夜10分あるため、次のサイクルまで
    持ち越すと、その間ずっと外出先の判定ができない。

    相手は宅内LANの機器で課金もないが、無制限には繰り返さない。応答しなくなった
    ウォールコネクターを叩き続けても回復しないため。
    """
    global retry_saved_count

    for attempt in range(1, max(attempts, 1) + 1):
        data = _get_json_once(host, path, timeout)
        if data is not None:
            if attempt > 1:
                retry_saved_count += 1
            return data
    return None


def take_retry_saved() -> int:
    """前回の呼び出し以降に「取り直しで救われた」回数を返し、カウンタを0に戻す。

    返した回数を呼び出し側がログへ出すことを前提にしている。読み捨てると、
    再試行が隠している失敗率がそのまま見えないままになる。
    """
    global retry_saved_count
    count = retry_saved_count
    retry_saved_count = 0
    return count


def read_vehicle_connected(host: str, timeout: float = 5.0, attempts: int = 2) -> str:
    """自宅のウォールコネクターに車両が接続されているかを3値で返す。

    例外は外に出さない。判定できない場合はすべて WC_UNKNOWN になる。
    """
    vitals = _get_json(host, "/api/1/vitals", timeout, attempts)
    if vitals is None:
        return WC_UNKNOWN

    connected = vitals.get("vehicle_connected")
    # キーが無い、または真偽値でない場合は判定に使えない。
    # ファームウェア更新でフィールドが消えたときに false と誤読しないため、
    # isinstance で明示的に確認する（0/1 や "false" を bool として扱わない）。
    if not isinstance(connected, bool):
        return WC_UNKNOWN

    return WC_CONNECTED if connected else WC_NOT_CONNECTED


def read_delivering(host: str, timeout: float = 5.0, attempts: int = 2) -> str:
    """自宅の充電器がいま給電しているかを3値で返す。

    vehicle_connected（ケーブルが挿さっているか）とは別の問いである。ケーブルが
    挿さったまま充電していない状態が通常であり、そのとき contactor_closed は false、
    vehicle_current_a は 0.0 になる（2026-08-23 に実機で確認）。

    用途は「車両データを読めない時間の代償がいま大きいか」の判断だけである。
    給電中なら、絞り込めない1分がそのまま買電になる。給電していない・読めない場合は
    何も主張せず、呼び出し側は従来の待機に落ちる。

    contactor_closed のみを見て vehicle_current_a は使わない。接点が閉じた直後など
    電流が0のまま給電中でありうるためで、この判定は「給電しているかもしれない」側へ
    倒しておくほうが安い（誤って倒れても vehicle_data 1回分の課金で済む）。
    """
    vitals = _get_json(host, "/api/1/vitals", timeout, attempts)
    if vitals is None:
        return WC_UNKNOWN

    closed = vitals.get("contactor_closed")
    # read_vehicle_connected と同じ理由で、真偽値でなければ判定に使わない。
    if not isinstance(closed, bool):
        return WC_UNKNOWN

    return WC_DELIVERING if closed else WC_NOT_DELIVERING


def read_serial(host: str, timeout: float = 5.0, attempts: int = 2) -> Optional[str]:
    """/api/1/version の serial_number を返す。読めなければ None。

    起動時に1回だけ呼び、どの物理個体に紐づいているかをログへ残すために使う。
    """
    version = _get_json(host, "/api/1/version", timeout, attempts)
    if version is None:
        return None
    serial = version.get("serial_number")
    if not isinstance(serial, str) or not serial:
        return None
    return serial
