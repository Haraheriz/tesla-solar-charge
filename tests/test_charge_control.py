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
# 夜間休止中の充電監視
#
# 2026-07-31〜08-01 の障害に対応する。当時の実装は休止に入った最初の1回で確認を
# 打ち切っていたため、確認より後に始まった充電を朝まで検知できなかった。
#
#   07-31 18:07  夜間休止入り。充電中を検知して停止、停止を確認
#   （以降、制御側のコマンドは一切なし）
#   08-01 09:40  車両が満充電（Complete）で復帰
# ---------------------------------------------------------------------------

def _resume_charging_once(at_sec):
    """指定秒数が経過した最初の巡回で、車両側が勝手に充電を再開した状況を作る。"""
    fired = {"done": False}

    def _hook(elapsed, world):
        if not fired["done"] and elapsed >= at_sec:
            world["charging_state"] = "Charging"
            fired["done"] = True

    return _hook


def test_夜間の停止後に再開した充電を検知して止める(run_loop):
    """07-31 の再現。停止を確認した後も監視を続けなければ朝まで系統充電が続く。"""
    res = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 20},
        start="2026-07-31 18:07:00",
        budget_sec=6 * 3600,
        # 21:07 ごろ、車両側のスケジュール充電などで充電が再開したとする
        on_poll=_resume_charging_once(3 * 3600),
    )
    assert res.count("charge_stop") == 2, "再開された充電を止めていない"
    assert res.world["charging_state"] == "Stopped"
    assert res.has_log("夜間休止中に充電を検知しました")


def test_夜間に開始された充電も検知して止める(run_loop):
    """休止入りの時点で就寝中でも、その後の充電開始を見逃してはいけない。

    07-28・07-29・07-30・08-01 の夜はいずれも 18:00 台に offline で、
    当時はその1回の観測だけで『充電していない』と断定していた。
    """
    def plug_in_at_2200(elapsed, world):
        if elapsed >= 4 * 3600:
            world["vehicle_state"] = "online"
            world["charging_state"] = "Charging"

    res = run_loop(
        world={"vehicle_state": "offline", "charging_state": "Disconnected", "amps": 4},
        start="2026-07-20 18:00:00",
        budget_sec=6 * 3600,
        on_poll=plug_in_at_2200,
    )
    assert res.count("charge_stop") >= 1, "休止入り後に始まった充電を止めていない"
    assert res.count("wake_up") == 0, "夜間に車両を起こしてはいけない"
    assert res.world["charging_state"] == "Stopped"


def test_夜間の停止失敗は確認できるまで再試行される(run_loop):
    """停止できないまま黙って朝を迎えないこと。上限に達したらCRITICALで報告する。"""
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


def test_夜間の停止試行には上限がある(run_loop):
    """無制限に再試行するとレートリミット(429)を誘発するため、上限で打ち切る。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            "command_results": {"charge_stop": False},
        },
        start="2026-07-20 18:00:00",
        budget_sec=12 * 3600,
    )
    # 1回の停止試行のなかで send_charge_command がさらにリトライするため、
    # POST数ではなく「停止を試みた回数」で数える。
    attempts = [m for m in res.messages() if "夜間休止中に充電を検知しました" in m]
    assert len(attempts) == res.module.NIGHT_STOP_MAX_ATTEMPTS


@pytest.mark.parametrize(
    "transient_failure",
    [
        # 起動時が1回目。夜間1サイクル目の取得（2回目）だけ失敗させる
        {"vehicle_list_raise_on_calls": [2]},
        {"vehicle_list_http_by_call": {2: 504}},
    ],
    ids=["read_timeout", "504"],
)
def test_夜間の単発の取得失敗はその場で取り直す(run_loop, transient_failure):
    """巡回間隔は10分あるため、1回の失敗がそのまま10分の観測欠落になる。

    2026-08-06 19:59 に車両リスト取得がタイムアウトし、その10分のあいだに
    帰宅・接続・充電開始が起きて検知できなかった。

    budget_sec は1サイクルぶんに満たない値にしてある。次の巡回を待たず
    「同じサイクル内で」取り直して充電を検知できたことを、この予算で担保する。

    再試行は `night_proxy_get()` に集約されており、車両リスト取得と
    charge_state 取得の両方が同じ関数を経由する。
    """
    world = {"vehicle_state": "online", "charging_state": "Charging", "amps": 20}
    world.update(transient_failure)

    res = run_loop(world=world, start="2026-07-20 18:00:00", budget_sec=300)

    assert res.has_log("すぐに再試行します", level="WARNING")
    assert res.count("charge_stop") == 1, "同じサイクル内で取り直せていない"
    assert res.world["charging_state"] == "Stopped"


def test_夜間の再試行は即時1回までにとどめる(run_loop):
    """無制限に投げ直すとレートリミットを誘発する。上限を定数として固定する。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            # 起動時の1回だけ成功させ、以降は常に5xxで失敗させる
            "vehicle_list_ok_calls": 1,
            "vehicle_list_fail_http": 503,
        },
        start="2026-07-20 18:00:00",
        budget_sec=300,
    )
    assert res.module.NIGHT_GET_ATTEMPTS == 2
    retries = [m for m in res.messages() if "すぐに再試行します" in m]
    assert len(retries) == 1, f"1サイクルで再試行が {len(retries)} 回発生している"


@pytest.mark.parametrize("status", [401, 408, 429])
def test_夜間の再試行対象は通信エラーと5xxに限る(run_loop, status):
    """401・408・429は即座に投げ直しても状況が変わらず、課金だけが増える。

    500未満はTesla Fleet APIの課金対象である（docs/01_architecture.md 第3章③）。
    """
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            "vehicle_list_ok_calls": 1,
            "vehicle_list_fail_http": status,
        },
        start="2026-07-20 18:00:00",
        budget_sec=300,
    )
    assert not res.has_log("すぐに再試行します"), f"HTTP {status} を再試行している"


@pytest.mark.parametrize(
    "failing_request",
    [
        # 起動時の1回だけ成功させ、以降のループ内取得を401で失敗させる
        {"vehicle_list_ok_calls": 1, "vehicle_list_fail_http": 401},
        {"charge_state_http": 401},
    ],
    ids=["vehicles", "charge_state"],
)
def test_夜間の401はトークン再取得を促す(run_loop, failing_request):
    """失効を単なる取得失敗として扱うと、同じトークンで上限まで失敗し監視が止まる。

    日中ループは401で `token_expires_at` を0に戻して次サイクルで取り直す。
    夜間だけこの分岐が無いと、トークン失効という頻度の高い原因で
    「一晩通した監視」が成立しなくなる。

    夜間ブロックは車両リストと充電状態の2箇所で外部へ問い合わせる。片方だけ
    直しても穴は塞がらないため、両方を同じ条件で検証する。
    """
    world = {"vehicle_state": "online", "charging_state": "Charging", "amps": 20}
    world.update(failing_request)

    res = run_loop(world=world, start="2026-07-20 18:00:00", budget_sec=3600)

    assert res.has_log("アクセストークンの失効を検知", level="WARNING")
    assert res.refresh_calls, "401を検知したのにトークンを取り直していない"


def test_車両リスト取得失敗を充電していないと断定しない(run_loop):
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
    warnings = [m for m in res.messages(level="WARNING") if "車両リスト取得に失敗" in m]
    assert len(warnings) > 1, "取得失敗後に再確認していない"
    assert not res.has_log("充電していないと判断"), "通信不良を充電なしと断定している"
    assert res.has_log("確認できませんでした", level="CRITICAL")


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
    assert res.has_log(expected_log)
    assert res.count("charge_stop") == 0
    assert res.count("wake_up") == 0


def test_夜間の観測ログは状況が変わったときだけ出す(run_loop):
    """10分毎に同じ行を出すと一晩で約80行になり、本当に見たい変化が埋もれる。"""
    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 10},
        start="2026-07-20 18:00:00",
        budget_sec=4 * 3600,
    )
    observations = [m for m in res.messages() if "充電していないと判断" in m]
    assert len(observations) == 1, f"同じ観測を繰り返し記録している（{len(observations)}件）"


# ---------------------------------------------------------------------------
# 外出先での充電を制御対象から除外する
#
# 2026-08-07 20:44、外出先のスーパーチャージャーでの充電を10分の間に2回停止させ、
# 21:05 に抜線されるまで、車両が再開するたびに停止させ続けた。
#
# 車両側のAPIには接続先充電器の識別子が存在しないため、自宅のウォールコネクターの
# ローカルAPI（vehicle_connected）を第一の根拠にする。読み取れなかった場合のみ
# charge_state の急速充電フィールドで補い、それでも断定できなければ従来動作に戻る。
# ---------------------------------------------------------------------------

def test_夜間に自宅ウォールコネクター未接続なら停止しない(run_loop):
    """08-07 の再現。夜間の充電検知だけを根拠に止めると外出先の充電を妨害する。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            "wc_vehicle_connected": False,
        },
        start="2026-08-07 20:44:00",
        budget_sec=3600,
    )
    assert res.count("charge_stop") == 0, "外出先の充電を停止させている"
    assert res.world["charging_state"] == "Charging"
    assert res.has_log("外出先での充電と判断して停止しません", level="ATTENTION")


def test_夜間の外出先判定は再試行カウンタを消費しない(run_loop):
    """通信の失敗ではないため、その夜の監視を打ち切らせてはいけない。

    上限を消費すると、長時間の外出先充電のあいだに night_stop_exhausted が立ち、
    帰宅後に始まった自宅の系統充電を朝まで止められなくなる。
    """
    def come_home_late(elapsed, world):
        # 6サイクル（60分）を十分に超えた時点で帰宅し、自宅で充電を始める
        if elapsed >= 3 * 3600:
            world["wc_vehicle_connected"] = True

    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            "wc_vehicle_connected": False,
        },
        start="2026-08-07 20:00:00",
        budget_sec=5 * 3600,
        on_poll=come_home_late,
    )
    assert not res.has_log("連続で確認できませんでした", level="CRITICAL"), \
        "外出先判定でリトライ上限を消費している"
    assert res.count("charge_stop") >= 1, "帰宅後の自宅充電を停止できていない"
    assert res.world["charging_state"] == "Stopped"


@pytest.mark.parametrize(
    "charging_state, amps, house_power, forbidden",
    [
        # 充電中・余剰不足 → 本来ならデバウンス後に停止し、その手前で最小電流へ絞る
        ("Charging", 20, 3000, ("charge_stop", "set_charging_amps")),
        # 充電中・余剰あり → 本来なら目標値へ電流を変更する
        ("Charging", 6, -8000, ("set_charging_amps",)),
        # 停止中・余剰あり → 本来なら充電を開始する
        ("Stopped", 4, -5000, ("charge_start",)),
    ],
    ids=["余剰不足での停止", "電流変更", "充電開始"],
)
def test_昼間の外出先では電流も停止も開始も送らない(
    run_loop, charging_state, amps, house_power, forbidden
):
    """日中の3経路すべてを塞ぐ。急速充電中の車両への set_charging_amps は挙動が未確認である。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": charging_state, "amps": amps,
            "wc_vehicle_connected": False,
        },
        start="2026-08-07 13:00:00",
        budget_sec=1800,
        house_power=house_power,
    )
    for command in forbidden:
        assert res.count(command) == 0, f"外出先で {command} を送っている"
    assert res.has_log("外出先での充電と判断", level="ATTENTION")


def test_フル充電モード中でも外出先ではコマンドを送らない(run_loop):
    """オーバーライドの目的は自宅で太陽光を無視して充電することであり、外出先では意味を持たない。

    従来はオーバーライド中も set_charging_amps が送信されていた。
    """
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 6,
            "wc_vehicle_connected": False,
        },
        start="2026-08-07 21:00:00",
        budget_sec=1800,
        override=True,
    )
    assert res.count("set_charging_amps") == 0
    assert res.count("charge_stop") == 0


@pytest.mark.parametrize(
    "fallback_world",
    [
        {"fast_charger_present": True},
        {"fast_charger_type": "Combo"},
        {"charger_power": 120},
    ],
    ids=["fast_charger_present", "fast_charger_type", "charger_power"],
)
def test_ウォールコネクターが判定不能なら急速充電フィールドで判断する(run_loop, fallback_world):
    """LAN障害でウォールコネクターを読めなくても、DC急速充電は止めない。

    ここで読むフィールドは既に取得済みの charge_state に含まれるため、
    Tesla API の呼び出し数は増えない。
    """
    world = {
        "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
        "wc_raise": True,
    }
    world.update(fallback_world)
    res = run_loop(world=world, start="2026-08-07 20:44:00", budget_sec=3600)
    assert res.count("charge_stop") == 0
    assert res.has_log("外出先での充電と判断して停止しません", level="ATTENTION")


@pytest.mark.parametrize(
    "unreadable",
    [
        {"wc_raise": True},
        {"wc_http": 404},
        {"wc_json_error": True},
        {"wc_omit_field": True},
        {"wc_vehicle_connected": None},
    ],
    ids=["接続例外", "HTTP404", "JSON解釈失敗", "キー欠落", "真偽値でない"],
)
def test_ウォールコネクターも急速充電フィールドも判定不能なら従来どおり停止する(run_loop, unreadable):
    """判定できないことを理由に夜通しの系統充電を許してはいけない。

    急速充電フィールドは実機のスーパーチャージャー接続時の値が未検証である。
    期待どおりでなければここに落ちるため、未検証のまま実装してもデグレしない。
    """
    world = {"vehicle_state": "online", "charging_state": "Charging", "amps": 20}
    world.update(unreadable)
    res = run_loop(world=world, start="2026-08-07 20:44:00", budget_sec=1800)
    assert res.count("charge_stop") >= 1, "判定不能なのに従来動作へ落ちていない"
    assert res.world["charging_state"] == "Stopped"


def test_ウォールコネクター未設定なら判定は行われない(run_loop):
    """設定していない環境では全経路が従来どおり動くこと。"""
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            # 設定が空なら、この値は参照されない
            "wc_vehicle_connected": False,
        },
        start="2026-08-07 20:44:00",
        budget_sec=1800,
        wall_connector_host="",
    )
    assert res.wc_calls == [], "未設定なのにウォールコネクターへ問い合わせている"
    assert res.count("charge_stop") >= 1
    assert res.has_log("外出先の充電判定は行いません")


def test_外出先判定のログに急速充電フィールドの実値を残す(run_loop):
    """フォールバックが正しく効くかを後から検証するための実機データを取りこぼさない。

    2026-08-10、テンフィールズファクトリーのFLASHで充電した際、これらの値が
    ログに残っておらず検証の機会を逃した。判定が働いた場面では必ず書き出す。
    """
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 21,
            "wc_vehicle_connected": False,
            "fast_charger_present": True,
            "fast_charger_type": "Combo",
            "charger_power": 90,
        },
        start="2026-08-10 20:00:00",
        budget_sec=1800,
    )
    assert res.has_log("fast_charger_present=True", level="ATTENTION")
    assert res.has_log("fast_charger_type='Combo'", level="ATTENTION")
    assert res.has_log("charger_power=90", level="ATTENTION")


def test_シリアルが設定と一致しなければ判定を無効化する(run_loop):
    """設定したIPが別の機器を指している。誤った機器の応答で在宅を判断させない。

    このとき判定は行われず、全経路が従来どおりの動作に戻る。無効化したことは
    ATTENTION で必ず可視化する（黙って従来動作に戻ると原因を追えない）。
    """
    res = run_loop(
        world={
            "vehicle_state": "online", "charging_state": "Charging", "amps": 20,
            "wc_serial": "OTHERDEVICE000000",
            # 別機器が「接続なし」を返しても、それを根拠に停止を見送ってはいけない
            "wc_vehicle_connected": False,
        },
        start="2026-08-07 20:44:00",
        budget_sec=1800,
        wall_connector_serial="HOMEWC0000000000",
    )
    assert res.has_log("シリアルが設定と一致しません", level="ATTENTION")
    assert res.count("charge_stop") >= 1, "判定を無効化したのに従来動作へ戻っていない"
    # 無効化後は毎サイクルの問い合わせも行わない（起動時の /api/1/version のみ）
    assert all("vitals" not in url for url in res.wc_calls)


def test_自宅ウォールコネクター接続中は従来どおり制御する(run_loop):
    """自宅での挙動を変えていないこと（デグレ検出）。"""
    night = run_loop(
        world={"vehicle_state": "online", "charging_state": "Charging", "amps": 20},
        start="2026-08-07 20:44:00",
        budget_sec=1800,
    )
    assert night.count("charge_stop") >= 1
    assert night.has_log("充電の停止を確認しました")

    day = run_loop(
        world={"vehicle_state": "online", "charging_state": "Stopped", "amps": 4},
        start="2026-08-07 10:00:00",
        budget_sec=60,
        house_power=-3000,
    )
    assert day.count("charge_start") == 1


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


def test_起動後に余剰を測り直してから開始を判断する(run_loop):
    """起動には70秒以上かかる。その間に余剰が消えていれば充電を始めてはいけない。

    2026-08-09 11:46 の再現。余剰1202Wで車両を起こし（ウェイクは¥2.75）、11:48に
    6Aで充電を開始したが、11:51には1090Wの買電に転じており、11:55に自分で停止させた。
    起動を決めた時点の値のまま開始を判断していたことが原因である。
    """
    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 4},
        start="2026-08-09 11:46:00",
        budget_sec=300,
        # 1回目（起動の判断）は余剰1202W、2回目（起動後の判断）は買電500W
        house_power=[-1202, 500],
    )
    assert res.count("wake_up") == 1, "開始閾値を超えているので起動自体は行う"
    assert res.count("charge_start") == 0, "余剰が消えているのに充電を開始している"
    assert res.has_log("起動後に余剰を測り直しました")


def test_起動後の再測定に失敗したら充電を開始しない(run_loop):
    """測れないまま古い値で開始するのは、測り直しを入れた意味が無い。"""
    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 4},
        start="2026-08-09 11:46:00",
        budget_sec=300,
        house_power=[-1202, None],
    )
    assert res.count("charge_start") == 0
    assert res.has_log("起動後の余剰再測定に失敗しました", level="WARNING")


def test_余剰が続いていれば起動後も充電を開始する(run_loop):
    """測り直しは開始を妨げるためのものではない。余剰が続いていれば従来どおり開始する。"""
    res = run_loop(
        world={"vehicle_state": "asleep", "charging_state": "Stopped", "amps": 4},
        start="2026-08-09 11:46:00",
        budget_sec=120,
        house_power=[-1202, -1300],
    )
    assert res.count("wake_up") == 1
    assert res.count("charge_start") == 1
    assert res.world["charging_state"] == "Charging"


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
