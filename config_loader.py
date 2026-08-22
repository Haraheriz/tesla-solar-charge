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
#    流れるかを追う必要がある。結果として、数値の設定キー16個のうち保険があったのは
#    2個だけで、残りは無防備だった。`REMO_SAMPLES` に 0 を書けば1回も測定せず、制御が
#    無言で止まる。`COMMAND_RETRIES` に 0 を書けばコマンドを1回も送らずに
#    「0回連続で失敗しました」と記録する。`TERMINAL_BACKOFF_SEC` に 0 を書けば
#    課金対象の問い合わせを全速で繰り返す。
# 2. max() は黙って値を書き換える。0 と書いた人は、それが 1 として動いていることを
#    ログのどこからも知れない。
#
# ここを通した後の値は下限を満たしていることが保証される。使う側は再検査しない
#（契約による設計：事前条件を満たす責任は呼び出し側にある）。
#
# 不正な値でも起動は続ける。systemd が再起動を繰り返して充電制御そのものが死ぬより、
# 既定値で動くほうが被害が小さいためである。黙って丸める従来との違いは、
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
