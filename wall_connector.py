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
    for _ in range(max(attempts, 1)):
        data = _get_json_once(host, path, timeout)
        if data is not None:
            return data
    return None


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
