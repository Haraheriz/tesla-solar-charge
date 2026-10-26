import os
import stat
from typing import Any, Callable, Dict

# 設定ファイルの生の値を、使う前に1回だけ検証して定数へ確定させるための入口。
#
# 以前は、下限の検査が「その値を使う関数の中」に散っていた。
#
#   wall_connector.py       for attempt in range(1, max(attempts, 1) + 1):
#   tesla_solar_charger.py  attempts: int = max(1, NIGHT_GET_ATTEMPTS)
#
# この形には2つの問題があった。
#
# 1. 設定キーの定義を見ても、その値に下限があるのか分からない。知るには値がどの関数へ
#    流れるかを追う必要がある。結果として、数値の設定キー16個のうち下限の検査があったのは
#    2個だけで、残りは無防備だった。`REMO_SAMPLES` に 0 を書けば1回も測定せず、制御が
#    ログを出さずに止まる。`COMMAND_RETRIES` に 0 を書けばコマンドを1回も送らずに
#    「0回連続で失敗しました」と記録する。`TERMINAL_BACKOFF_SEC` に 0 を書けば
#    課金対象の問い合わせを全速で繰り返す。
# 2. max() はログを出さずに値を書き換える。0 と書いた人は、それが 1 として動いていることを
#    ログのどこからも知れない。
#
# ここを通した後の値は下限を満たしていることが保証される。使う側は再検査しない
#（契約による設計：事前条件を満たす責任は呼び出し側にある）。
#
# 不正な値でも起動は続ける。systemd が再起動を繰り返して充電制御そのものが停止するより、
# 既定値で動くほうが被害が小さいためである。ログを出さずに丸める従来との違いは、
# 丸めたことが必ずログに残る点にある。


class Settings:
    """設定辞書から数値を取り出し、下限を満たさなければ既定値へ戻す。

    on_invalid には、既定値へ戻したことを利用者へ知らせる関数を渡す
    （tesla_solar_charger.py なら log_attention）。異常ではないが人間の注意を
    向けたい事実であり、ATTENTION の定義に合致する。

    文字列の設定はここを通さない。空文字が「機能を使わない」を意味する規約が既にあり
    （WALL_CONNECTOR_HOST / WALL_CONNECTOR_SERIAL）、数値のような失敗の仕方をしない。
    """

    def __init__(self, config: Dict[str, Any], on_invalid: Callable[[str], None]) -> None:
        self._config = config
        self._on_invalid = on_invalid

    def integer(self, key: str, default: int, *, minimum: int) -> int:
        """整数の設定を返す。minimum はキーワード必須で、省略できない。

        省略できないようにしてあるのは、新しいキーを足す人に下限を必ず決めさせるため。
        散っていた頃に漏れたのは、書き忘れても動いてしまったからである。
        """
        return int(self._checked(key, default, minimum, int))

    def number(self, key: str, default: float, *, minimum: float) -> float:
        """小数を許す設定を返す（タイムアウト秒など）。"""
        return float(self._checked(key, default, minimum, float))

    def _checked(self, key: str, default: Any, minimum: Any, cast: Callable[[Any], Any]) -> Any:
        raw = self._config.get(key, default)
        try:
            value = cast(raw)
        except (TypeError, ValueError):
            self._on_invalid(
                f"設定 {key} の値 {raw!r} を数値として読めません。既定値 {default} で起動します。"
            )
            return default
        if value < minimum:
            self._on_invalid(
                f"設定 {key} の値 {value} は下限 {minimum} を下回ります。既定値 {default} で起動します。"
            )
            return default
        return value


def warn_if_accessible_by_others(path: str, on_invalid: Callable[[str], None]) -> bool:
    """所有者以外に読み書き実行のどれかを許す権限ビットが立っていれば on_invalid へ知らせる。

    対象は認証情報を含むファイル（tesla_config.json・tesla_tokens.json）である。
    docs/02_deploy.md は chmod 600 を手順にしているが、手順の実行漏れは起動しても
    どこにも現れなかった。ssh が秘密鍵の権限を起動時に確かめるのと同じ考え方で、
    ここで確かめてログに残す。起動は止めない（充電制御が止まるほうが被害が大きい）。

    POSIX 以外（Windows の開発機）では st_mode の権限ビットに意味がないため何もしない。
    ファイルが無い場合も何もしない（無いこと自体は呼び出し側が別に扱う）。
    戻り値は知らせたかどうかで、テストが見るためにある。
    """
    if os.name != "posix":
        return False
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    if mode & 0o077 == 0:
        return False
    on_invalid(
        f"{path} の権限が {stat.filemode(mode)} で、所有者以外からアクセスできます。"
        f"chmod 600 {path} を実行してください（docs/02_deploy.md 第4章）。"
    )
    return True
