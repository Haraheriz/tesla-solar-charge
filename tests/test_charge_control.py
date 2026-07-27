"""充電制御ループの回帰テスト。

このファイルの多くは 2026-07-15 に起きた実際の障害に対応している。
当日の事象（tesla_solar_charger.log より）:

  01:58  PWAからフル充電モードON
  02:03  48A（約9.6kW）で深夜の系統充電を開始
  03:28  満充電（Complete）に到達
  03:28-07:39  満充電の車へ charge_start を3分おきに60回送信、7回叩き起こす
  19:04  ステータス Stopped（ユーザーが停止）→ 即座に再開命令
  19:10  充電が再開されてしまう
  19:40  ユーザーがオーバーライドを解除（ONのまま17時間41分）

「充電を停止したつもりが一昼夜継続していた」という報告の正体は19:04の再開である。
"""
import pytest


# ---------------------------------------------------------------------------
# 2026-07-15 の障害に対する回帰テスト
# ---------------------------------------------------------------------------

def test_満充電の車両に充電開始を送り続けない(run_loop):
    """03:28-07:39 の再現。当時は4時間で charge_start を60回送っていた。"""
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Complete", "amps": 48},
        start="2026-07-15 03:28:00",
        budget_sec=4 * 3600,
        override=True,
    )
    assert res.count("charge_start") == 0
    assert res.count("set_charging_amps") == 0
    assert res.has_log("満充電に到達済み")


def test_満充電のあいだ就寝中の車両を起こし続けない(run_loop):
    """当時は満充電でも offline を見るたびWake Upし、408を誘発していた（4時間で7回）。

    抑止期間が明けたときの再確認は必要なので0回にはならない。ただし
    抑止期間(1時間) > 待機間隔(10分) でなければ抑止は一度も効かず、
    毎サイクル叩き起こすことになる。この関係が崩れていないかを見張る。
    """
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Complete", "amps": 48},
        start="2026-07-15 03:28:00",
        budget_sec=4 * 3600,
        override=True,
        # 毎サイクル offline に戻す＝常にWake Upの判断を迫られる最悪ケース
        on_poll=lambda elapsed, world: world.update(vehicle_state="offline"),
    )
    # 抑止なしなら10分ごと（4時間で約24回）起こしてしまう
    assert res.count("wake_up") <= 5, "終端ステータス中のWake Up抑止が効いていない"
    assert res.has_log("車両を起こさずに待機します")


def test_wake抑止期間は待機間隔より長い(run_loop):
    """設定値の関係が崩れると抑止が無効化されるため、定数レベルで固定する。

    skip_wake_until = now + 抑止期間 の直後に 待機間隔 だけsleepするため、
    両者が同じ値だと次サイクル開始時点でちょうど期限切れになり抑止が効かない。
    """
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Complete", "amps": 48},
        start="2026-07-15 03:28:00",
        budget_sec=60,
        override=True,
    )
    assert res.module.TERMINAL_WAKE_SUPPRESS_SEC > res.module.TERMINAL_BACKOFF_SEC


def test_ユーザーの手動停止でオーバーライドが自動解除される(run_loop):
    """19:04 の再現。当時は Stopped を検知した3分後に再開していた。"""
    def stop_after_10min(elapsed, world):
        if elapsed >= 600:
            world["charging_state"] = "Stopped"

    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 48},
        start="2026-07-15 19:00:00",
        budget_sec=1800,
        override=True,
        on_poll=stop_after_10min,
    )
    assert res.override_writes == [False], "オーバーライドが自動解除されていない"
    assert res.count("charge_start") == 0, "手動停止後に充電を再開してしまっている"
    assert res.has_log("手動での充電停止を検知", level="ATTENTION")


def test_満充電への遷移は手動停止として扱わない(run_loop):
    """Charging→Complete は満充電であってユーザーの停止操作ではない。"""
    def complete_after_10min(elapsed, world):
        if elapsed >= 600:
            world["charging_state"] = "Complete"

    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 48},
        start="2026-07-15 02:00:00",
        budget_sec=1800,
        override=True,
        on_poll=complete_after_10min,
    )
    assert res.override_writes == [], "満充電でオーバーライドを解除してはいけない"


def test_フル充電モードの継続がATTENTIONで可視化される(run_loop):
    """自動解除はしない方針のため、切り忘れに気づけるログが唯一の防御線になる。"""
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 48},
        start="2026-07-15 02:00:00",
        budget_sec=900,
        override=True,
    )
    attention = res.messages(level="ATTENTION")
    assert attention, "ATTENTIONログが1件も出ていない"
    assert all("フル充電モード継続中" in m for m in attention)
    assert any("経過" in m for m in attention), "経過時間が表示されていない"
    assert any("夜間休止を迂回中" in m for m in attention), "夜間の迂回が明示されていない"


# ---------------------------------------------------------------------------
# 充電コマンドの成否検証
# ---------------------------------------------------------------------------

def test_停止コマンド失敗時はデバウンスを戻さず再試行する(run_loop):
    """当時は成否を見ずカウンタを0に戻していたため『停止したつもり』になっていた。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 10,
            "command_results": {"charge_stop": False},
        },
        start="2026-07-20 15:00:00",
        budget_sec=1800,
        house_power=2000,
    )
    assert res.count("charge_stop") > 1, "停止に失敗したのに再試行していない"
    assert res.world["charging_state"] == "Charging"
    assert res.has_log("充電停止を確認できませんでした", level="ERROR")


def test_開始コマンド失敗時は電流設定に進まない(run_loop):
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Stopped", "amps": 4,
            "command_results": {"charge_start": False},
        },
        start="2026-07-20 10:00:00",
        budget_sec=400,
        house_power=-3000,
    )
    assert res.count("charge_start") > 0
    assert res.count("set_charging_amps") == 0, "開始できていないのに電流を設定している"
    assert res.has_log("充電の開始を確認できませんでした", level="ERROR")


# ---------------------------------------------------------------------------
# 夜間休止入口の停止確認
# ---------------------------------------------------------------------------

def test_夜間休止入口の停止失敗は確認できるまで再試行される(run_loop):
    """当時は完了フラグを試行前に立てていたため、一晩に最大1回しか実行されなかった。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            "command_results": {"charge_stop": False},
        },
        start="2026-07-20 18:00:00",
        budget_sec=5400,
    )
    assert res.count("charge_stop") > 3, "夜間の停止が再試行されていない"
    assert res.has_log("確認できませんでした", level="CRITICAL")


def test_夜間休止入口の停止成功後は再問い合わせしない(run_loop):
    """就寝中の車両を無駄に起こさないため、確認できたらその夜はもう触らない。"""
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 20},
        start="2026-07-20 18:00:00",
        budget_sec=5400,
    )
    assert res.count("charge_stop") == 1
    assert res.world["charging_state"] == "Stopped"


def test_車両リスト取得失敗時は夜間チェックを完了扱いにしない(run_loop):
    """通信不良を『充電していない』と誤断定すると朝まで系統充電が続きうる。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            # 起動時の1回だけ成功させ、以降のループ内取得を失敗させる
            "vehicle_list_ok_calls": 1,
        },
        start="2026-07-20 18:00:00",
        budget_sec=5400,
    )
    attempts = [m for m in res.messages() if "夜間休止前の充電状態を確認します" in m]
    assert len(attempts) > 1, "取得失敗後に再確認していない"
    assert res.has_log("車両リスト取得に失敗", level="WARNING")


@pytest.mark.parametrize(
    "vehicle_state, charging_state, expected_log",
    [
        ("asleep", "Stopped", "そのまま休止します"),
        ("offline", "Stopped", "そのまま休止します"),
        ("online", "Stopped", "停止操作は不要と判断しました"),
        ("online", "Complete", "停止操作は不要と判断しました"),
    ],
)
def test_夜間チェックは結果を必ずログに残す(run_loop, vehicle_state, charging_state, expected_log):
    """無言で終わる経路があると『チェックが走ったのか』を後から追えない。"""
    res = run_loop(
        world={"vehicle_state": vehicle_state, "charging_state": charging_state, "amps": 10},
        start="2026-07-20 18:00:00",
        budget_sec=2400,
    )
    assert res.has_log("夜間休止前の充電状態を確認します")
    assert res.has_log(expected_log)
    assert res.count("charge_stop") == 0
    assert res.count("wake_up") == 0


# ---------------------------------------------------------------------------
# 異常なステータスに対する安全側の挙動
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("charging_state", ["Disconnected", "NoPower"])
def test_終端ステータスではコマンドを送らない(run_loop, charging_state):
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": charging_state, "amps": 4},
        start="2026-07-20 10:00:00",
        budget_sec=1800,
        house_power=-5000,
    )
    assert res.count("charge_start") == 0
    assert res.count("set_charging_amps") == 0


@pytest.mark.parametrize("charging_state", [None, "", "SomethingNew"])
def test_未知のステータスでは安全側に倒してコマンドを送らない(run_loop, charging_state):
    """充電状態が判断できないまま開始命令を投げるのは危険。"""
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": charging_state, "amps": 4},
        start="2026-07-20 10:00:00",
        budget_sec=1800,
        house_power=-5000,
    )
    assert res.count("charge_start") == 0
    assert res.has_log("未知の充電ステータス", level="WARNING")


def test_開始処理中はコマンドを重ねない(run_loop):
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Starting", "amps": 16},
        start="2026-07-20 10:00:00",
        budget_sec=600,
        house_power=-5000,
    )
    assert res.count("charge_start") == 0
    assert res.has_log("充電の開始処理中")


# ---------------------------------------------------------------------------
# 通常の太陽光追従（デグレ検出用）
# ---------------------------------------------------------------------------

def test_余剰を検知したら充電を開始する(run_loop):
    # budget を1サイクルぶんに絞る（続けるとサイクルごとに余剰を足し込んで電流が上がっていく）
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Stopped", "amps": 4},
        start="2026-07-20 10:00:00",
        budget_sec=60,
        house_power=-3000,
    )
    assert res.count("charge_start") == 1
    assert res.world["charging_state"] == "Charging"
    assert res.world["amps"] == 15, "余剰3000Wなら15A（200W/A）で開始するはず"


def test_余剰不足はデバウンスしてから停止する(run_loop):
    """雲による一瞬の落ち込みで停止・再開を繰り返さないための仕組み。"""
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 10},
        start="2026-07-20 15:00:00",
        budget_sec=900,
        house_power=2000,
    )
    assert res.has_log("1/2回目")
    assert res.count("charge_stop") == 1
    assert res.world["charging_state"] == "Stopped"


def test_最大電流を超えない(run_loop):
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 40},
        start="2026-07-20 12:00:00",
        budget_sec=900,
        house_power=-9999,
    )
    assert res.world["amps"] <= res.module.MAX_AMPS


def test_余剰が閾値未満なら就寝中の車両を起こさない(run_loop):
    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 4},
        start="2026-07-20 10:00:00",
        budget_sec=900,
        house_power=-200,
    )
    assert res.count("wake_up") == 0


def test_夜間は太陽光追従モードで動かない(run_loop):
    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 4},
        start="2026-07-20 23:00:00",
        budget_sec=3600,
        house_power=-5000,
    )
    assert res.count("charge_start") == 0
    assert res.has_log("夜間休止モード中")


# ---------------------------------------------------------------------------
# 補助関数
# ---------------------------------------------------------------------------

def test_format_duration(run_loop, tmp_path):
    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 4},
        start="2026-07-20 23:00:00",
        budget_sec=60,
    )
    fmt = res.module.format_duration
    assert fmt(0) == "0分"
    assert fmt(600) == "10分"
    assert fmt(3600) == "1時間0分"
    # 2026-07-15 にオーバーライドがONだった実際の長さ
    assert fmt(17 * 3600 + 41 * 60) == "17時間41分"


def test_attentionレベルが登録されている(run_loop):
    import logging

    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 4},
        start="2026-07-20 23:00:00",
        budget_sec=60,
    )
    assert res.module.ATTENTION == 25
    assert logging.INFO < res.module.ATTENTION < logging.WARNING
    assert logging.getLevelName(res.module.ATTENTION) == "ATTENTION"
