"""目標充電率（docs/05_charge_target_design.md）の回帰テスト。

番号 T1〜T14 は設計書第10章の表と対応する。T15（目標未設定なら従来どおり）は
tests/test_charge_control.py の既存テストがそのまま担う。T16 は test_control_server.py にある。

既定の擬似世界では、ウォールコネクターの contactor_closed が True（給電中）になっている。
目標到達後の待機は給電を検知すると打ち切るため、止まった状態を再現するテストでは
wc_contactor_closed=False を渡す。
"""


def _home(**world):
    """自宅の充電器に接続中の日中の車両。呼び出し側の値で上書きする。"""
    base = {
        "vehicle_state": "online",
        "charging_state": "Stopped",
        "amps": 6,
        "battery_level": 20,
        "charge_limit_soc": 80,
        "wc_contactor_closed": False,
    }
    base.update(world)
    return base


# ---------------------------------------------------------------------------
# 停止・開始・再開（第4.1〜4.3節）
# ---------------------------------------------------------------------------

def test_T1_目標に達したら充電停止を1回だけ送る(run_loop):
    res = run_loop(
        world=_home(charging_state="Charging", battery_level=22, battery_rise_per_read=1, charge_target_soc=25),
        start="2026-09-28 10:00:00",
        budget_sec=2 * 3600,
        house_power=-3000,
    )
    assert res.count("charge_stop") == 1
    assert res.count("charge_start") == 0, "目標に達したあと余剰で再開している"
    assert res.has_log("目標充電率 25% に達したため、充電を停止しました", "ATTENTION")
    assert res.world["vehicle_status"].get("target_reached_at")


def test_T1_目標未満では止めない(run_loop):
    """R2：目標より下で止めると、スーパーチャージャーまで走れない。"""
    res = run_loop(
        world=_home(charging_state="Charging", battery_level=24, charge_target_soc=25),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=-3000,
    )
    assert res.count("charge_stop") == 0


def test_T2_目標以上なら余剰があっても充電を開始しない(run_loop):
    res = run_loop(
        world=_home(battery_level=30, charge_target_soc=25),
        start="2026-09-28 10:00:00",
        budget_sec=2 * 3600,
        house_power=-5000,
    )
    assert res.count("charge_start") == 0
    assert res.has_log("目標充電率 25% 以上のため、充電を開始しません")
    # 開始を抑止している間は10分ごとにしか読まない（3分ごとに読むと費用が約3倍になる）
    assert res.vehicle_data_calls <= 2 * 3600 // 600 + 1


def test_T3_目標を1パーセント下回り余剰があれば再開する(run_loop):
    def drop(elapsed, world):
        if elapsed >= 1800:
            world["battery_level"] = 24

    res = run_loop(
        world=_home(battery_level=25, charge_target_soc=25),
        start="2026-09-28 10:00:00",
        budget_sec=2 * 3600,
        house_power=-3000,
        on_poll=drop,
    )
    assert res.count("charge_start") >= 1


def test_T4_就寝中で最後の充電率が目標以上なら起こさない(run_loop):
    res = run_loop(
        world=_home(vehicle_state="asleep", charge_target_soc=25,
                    vehicle_status_initial={"battery_level": 30, "observed_at": 0}),
        start="2026-09-28 10:00:00",
        budget_sec=2 * 3600,
        house_power=-5000,
    )
    assert res.count("wake_up") == 0
    assert res.has_log("目標充電率 25% 以上のため、車両を起こしません")


def test_T4_目標を引き上げると次のサイクルで起こす(run_loop):
    def raise_target(elapsed, world):
        if elapsed >= 1800:
            world["charge_target_soc"] = 40

    res = run_loop(
        world=_home(vehicle_state="asleep", battery_level=30, charge_target_soc=25,
                    vehicle_status_initial={"battery_level": 30}),
        start="2026-09-28 10:00:00",
        budget_sec=2 * 3600,
        house_power=-5000,
        on_poll=raise_target,
    )
    assert res.count("wake_up") >= 1
    assert res.has_log("目標充電率が 25% → 40% に変更されました")


# ---------------------------------------------------------------------------
# フル充電モードとの関係（第4.4節）
# ---------------------------------------------------------------------------

def test_T5_フル充電モード中に目標へ達したら停止してから解除しその夜は充電しない(run_loop):
    res = run_loop(
        world=_home(charging_state="Charging", amps=48, battery_level=47, battery_rise_per_read=1,
                    charge_target_soc=50),
        start="2026-09-28 17:00:00",
        budget_sec=4 * 3600,
        override=True,
    )
    assert res.count("charge_stop") == 1
    assert res.override_writes == [False]
    assert res.count("charge_start") == 0, "解除後に夜間の買電で補充している"
    assert res.has_log("フル充電モードを解除しました", "ATTENTION")


def test_T6_停止が失敗したらフル充電モードを解除しない(run_loop):
    res = run_loop(
        world=_home(charging_state="Charging", amps=48, battery_level=50, charge_target_soc=50,
                    command_results={"charge_stop": False}),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        override=True,
    )
    assert res.count("charge_stop") >= 1
    assert res.override_writes == []
    assert res.has_log("目標充電率での充電停止を確認できませんでした")


def test_T7_目標以上でフル充電モードをONにすると起こさずに解除する(run_loop):
    res = run_loop(
        world=_home(vehicle_state="asleep", charge_target_soc=50,
                    vehicle_status_initial={"battery_level": 60}),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        override=True,
    )
    assert res.count("wake_up") == 0
    assert res.override_writes == [False]


def test_T13b_車両自身が目標で止めたComplete状態でもフル充電モードを解除する(run_loop):
    res = run_loop(
        world=_home(charging_state="Complete", battery_level=60, charge_limit_soc=60, charge_target_soc=60),
        start="2026-09-28 10:00:00",
        budget_sec=1800,
        override=True,
    )
    assert res.count("charge_stop") == 0
    assert res.override_writes == [False]


# ---------------------------------------------------------------------------
# 外出先（R3）
# ---------------------------------------------------------------------------

def test_T8_外出先の充電は目標以上でも止めない(run_loop):
    res = run_loop(
        world=_home(charging_state="Charging", amps=32, battery_level=70, charge_target_soc=25,
                    wc_vehicle_connected=False),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=3000,
    )
    assert res.count("charge_stop") == 0
    assert res.count("set_charge_limit") == 0


# ---------------------------------------------------------------------------
# 車両側の充電上限（第4.5節）
# ---------------------------------------------------------------------------

def test_T9_目標が下限未満なら元の値を保存して下限を送る(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=30),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=3000,
    )
    assert res.world["set_charge_limit_sent"] == [50]
    status = res.world["vehicle_status"]
    assert status["charge_limit_restore_soc"] == 80
    assert status["charge_limit_applied_soc"] == 50
    assert res.has_log("車両側の下限が 50% のため、30% ではシステムが充電を停止します")


def test_T9b_目標が下限以上なら目標と同じ値を送り目標を変えると送り直す(run_loop):
    def change(elapsed, world):
        if elapsed >= 1200:
            world["charge_target_soc"] = 70

    res = run_loop(
        world=_home(battery_level=40, charge_target_soc=60),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=3000,
        on_poll=change,
    )
    assert res.world["set_charge_limit_sent"] == [60, 70]
    assert res.world["vehicle_status"]["charge_limit_restore_soc"] == 80, "元の値を上書きしている"


def test_T9c_下限未満の値を送らず切り上げ後の値が読めたら再送しない(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=10),
        start="2026-09-28 10:00:00",
        budget_sec=2 * 3600,
        house_power=3000,
    )
    assert res.world["set_charge_limit_sent"] == [50]


def test_T9d_応答が成功でも読んだ値が違えば再送する(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=30, set_charge_limit_ignored=True),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=3000,
    )
    assert len(res.world["set_charge_limit_sent"]) >= 2
    assert res.has_log("読み取った値は 80% でした", "ATTENTION")
    assert res.world["vehicle_status"]["charge_limit_restore_soc"] == 80


def test_T9e_下限が無い応答では50として扱う(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=30, omit_charge_limit_soc_min=True),
        start="2026-09-28 10:00:00",
        budget_sec=1800,
        house_power=3000,
    )
    assert res.world["set_charge_limit_sent"] == [50]


def test_T9e_車両が返す下限に従う(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=45, charge_limit_soc_min=40),
        start="2026-09-28 10:00:00",
        budget_sec=1800,
        house_power=3000,
    )
    assert res.world["set_charge_limit_sent"] == [45]


def test_T10_自宅の充電器から外れたら元の値に戻す(run_loop):
    res = run_loop(
        world=_home(charging_state="Disconnected", charge_limit_soc=50, charge_target_soc=30,
                    wc_vehicle_connected=False,
                    vehicle_status_initial={"charge_limit_applied_soc": 50, "charge_limit_restore_soc": 80}),
        start="2026-09-28 10:00:00",
        budget_sec=2 * 3600,
        house_power=3000,
    )
    assert res.world["set_charge_limit_sent"] == [80], "二重に送っている、または戻していない"
    assert res.world["vehicle_status"]["charge_limit_restore_soc"] is None
    assert res.has_log("自宅の充電器から外れたため、車両側の充電上限を 80% に戻しました")


def test_T10_車両データを読まない経路でも元の値に戻す(run_loop):
    """1回目はウォールコネクターを読めず判定できない。2回目は読まない経路に入る。"""
    def recover(elapsed, world):
        if elapsed > 0:
            world["wc_raise"] = False

    res = run_loop(
        world=_home(charging_state="Disconnected", charge_limit_soc=50, charge_target_soc=30,
                    wc_vehicle_connected=False, wc_raise=True,
                    vehicle_status_initial={"charge_limit_applied_soc": 50, "charge_limit_restore_soc": 80}),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=3000,
        on_poll=recover,
    )
    assert res.world["set_charge_limit_sent"] == [80]
    assert res.vehicle_data_calls == 1


def test_T11_ウォールコネクターを読めないときは上限を変更しない(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=30, wc_raise=True),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=3000,
    )
    assert res.count("set_charge_limit") == 0


def test_T12_Teslaアプリで上限を変えられたらその値を保存して戻す(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=30, charge_limit_soc=70,
                    vehicle_status_initial={"charge_limit_applied_soc": 50, "charge_limit_restore_soc": 80}),
        start="2026-09-28 10:00:00",
        budget_sec=1800,
        house_power=3000,
    )
    assert res.world["set_charge_limit_sent"] == [50]
    assert res.world["vehicle_status"]["charge_limit_restore_soc"] == 70
    assert res.has_log("車両側の充電上限が 70% に変更されていました", "ATTENTION")


def test_T13_解除時に上限が変えられていたら戻さず記録だけ消す(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=None, charge_limit_soc=65,
                    vehicle_status_initial={"charge_limit_applied_soc": 50, "charge_limit_restore_soc": 80}),
        start="2026-09-28 10:00:00",
        budget_sec=1800,
        house_power=3000,
    )
    assert res.count("set_charge_limit") == 0
    assert res.world["vehicle_status"]["charge_limit_applied_soc"] is None
    assert res.has_log("元の値 80% には戻しません", "ATTENTION")


def test_T13_解除したら元の値に戻す(run_loop):
    res = run_loop(
        world=_home(charge_target_soc=None, charge_limit_soc=50,
                    vehicle_status_initial={"charge_limit_applied_soc": 50, "charge_limit_restore_soc": 80}),
        start="2026-09-28 10:00:00",
        budget_sec=1800,
        house_power=3000,
    )
    assert res.world["set_charge_limit_sent"] == [80]
    assert res.has_log("目標充電率が解除されたため、車両側の充電上限を 80% に戻しました")


# ---------------------------------------------------------------------------
# 目標到達後の待機（第4.6節）
# ---------------------------------------------------------------------------

def test_T14_待機中に給電が始まったら60秒で打ち切る(run_loop):
    res = run_loop(
        world=_home(battery_level=30, charge_target_soc=25, wc_contactor_closed=True),
        start="2026-09-28 10:00:00",
        budget_sec=1800,
        house_power=3000,
    )
    assert res.has_log("待機中に自宅の充電器が給電を始めました")
    # 600秒を待ち切らず、60秒で次のサイクルへ進んでいる
    assert 600 not in res.module.time.slept


def test_不正な目標充電率は未設定として扱い一度だけ知らせる(run_loop):
    res = run_loop(
        world=_home(battery_level=30, charge_target_invalid=True),
        start="2026-09-28 10:00:00",
        budget_sec=3600,
        house_power=-5000,
    )
    notices = [m for m in res.messages("ATTENTION") if "charge_target_soc" in m]
    assert len(notices) == 1
    assert res.count("charge_start") >= 1, "未設定として扱われていない"
