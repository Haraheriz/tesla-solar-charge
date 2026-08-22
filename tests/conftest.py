"""充電制御ループをネットワークなしで駆動するためのテスト基盤。

tesla_solar_charger.main() は本来「無限ループ・実API・実時間」で動く。ここでは

  * proxy_session  → 擬似Tesla APIプロキシ（FakeSession）
  * time           → 仮想時計（FakeTime）。sleepで時間が飛び、予算を超えたら停止
  * Remo / override_state / トークン → 差し替え
  * wall_connector.wc_session → 自宅ウォールコネクターの擬似ローカルAPI（FakeWCSession）

に置き換えることで、1回のテストで数時間ぶんのサイクルを一瞬で回す。

実機（Tesla車両・ラズパイ）でしか確認できない部分は当然カバーできないが、
「どのステータスのときにどのコマンドを送るか」という制御ロジックは全て検証できる。
2026-07-15 に発生した『停止したつもりが充電が継続する』事象はここに属する。
"""
import importlib.util
import itertools
import logging
import os
import time as real_time

import pytest

import wall_connector

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
CI_CONFIG = os.path.join(TESTS_DIR, "fixtures", "ci_config.json")

# モジュールを毎回別名で読み込むためのカウンタ（テスト間でグローバル状態を共有しない）
_module_counter = itertools.count()


class StopSim(BaseException):
    """main() の `except Exception` を突き抜けてループを終わらせるための例外。

    BaseException を継承しているのが重要。Exception だと main() 内の
    ループ内例外ハンドラに捕まってしまい、いつまでも止まらない。
    """


class FakeTime:
    """仮想時計。sleep()で時刻が進み、予算を超えた時点で StopSim を投げる。"""

    def __init__(self, start_epoch, budget_sec):
        self.now = start_epoch
        self.deadline = start_epoch + budget_sec
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, sec):
        self.slept.append(sec)
        self.now += sec
        if self.now > self.deadline:
            raise StopSim()

    def localtime(self, t=None):
        return real_time.localtime(self.now if t is None else t)

    def strftime(self, fmt, t=None):
        return real_time.strftime(fmt, t if t is not None else real_time.localtime(self.now))


class FakeReadTimeout(Exception):
    """プロキシへの読み取りタイムアウト（requests の ReadTimeout 相当）。"""


class FakeResponse:
    def __init__(self, status_code, payload, json_error=False):
        self.status_code = status_code
        self._payload = payload
        # 本文がJSONとして解釈できない状況（ウォールコネクターの判定不能パターンのひとつ）
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """車両リスト取得・charge_state取得・各種コマンドに応答する擬似Teslaプロキシ。

    world 辞書が「車両の実際の状態」を表し、コマンドを受けると変化する。
    world["command_results"] で個別コマンドをわざと失敗させられる。
    """

    def __init__(self, world):
        self.world = world
        self.commands = []
        self.verify = None
        self.vehicle_list_calls = 0

    def _fail(self, command):
        return self.world.get("command_results", {}).get(command, True) is False

    def get(self, url, headers=None, timeout=None):
        if url.endswith("/api/1/vehicles"):
            self.vehicle_list_calls += 1
            # 何回目の取得で失敗させるかを指定する。単発のタイムアウトや5xxからの
            # 即時再試行を検証するために、呼び出し回数で撃ち分ける必要がある。
            # 起動時の1回目もこの番号に含まれる。
            if self.vehicle_list_calls in (self.world.get("vehicle_list_raise_on_calls") or []):
                raise FakeReadTimeout(
                    "HTTPSConnectionPool(host='localhost', port=4443): Read timed out. (read timeout=10)"
                )
            by_call = self.world.get("vehicle_list_http_by_call") or {}
            if self.vehicle_list_calls in by_call:
                return FakeResponse(by_call[self.vehicle_list_calls], {})
            # main() は起動時にも車両リストを取得し、失敗すると sys.exit(1) する。
            # ループ内の通信障害だけを再現したい場合は vehicle_list_ok_calls で
            # 「最初のN回だけ成功させる」よう指定する。
            ok_calls = self.world.get("vehicle_list_ok_calls")
            if ok_calls is not None and self.vehicle_list_calls > ok_calls:
                # 失敗時のステータスは既定で500（通信障害）。401（トークン失効）など
                # 扱いが分岐するケースは vehicle_list_fail_http で指定する。
                return FakeResponse(self.world.get("vehicle_list_fail_http", 500), {})
            if self.world.get("vehicle_list_http", 200) != 200:
                return FakeResponse(self.world["vehicle_list_http"], {})
            fleet = [{"vin": "TESTVIN0000000000", "state": self.world["vehicle_state"]}]
            # 複数所有の再現。制御対象は先頭のみで、2台目以降は無視される。
            for i in range(self.world.get("extra_vehicles", 0)):
                fleet.append({"vin": f"TESTVIN{i + 1:011d}", "state": "asleep"})
            return FakeResponse(200, {"response": fleet})
        if "vehicle_data" in url:
            if self.world.get("charge_state_http", 200) != 200:
                return FakeResponse(self.world["charge_state_http"], {})
            # HTTP 200 でありながら本文に response が無い応答。Tesla側の仕様として
            # 起こりうるため、制御ループが周期を守れるかを確かめる必要がある。
            if self.world.get("charge_state_response_missing"):
                return FakeResponse(200, {})
            return FakeResponse(200, {"response": {"charge_state": {
                "charging_state": self.world["charging_state"],
                "charge_current_request": self.world["amps"],
                # 急速充電の判定に使うフィールド。既定は自宅のAC充電に相当する値で、
                # ウォールコネクターを読めなかったときのフォールバックにのみ影響する。
                "fast_charger_present": self.world.get("fast_charger_present", False),
                "fast_charger_type": self.world.get("fast_charger_type", "<invalid>"),
                "charger_power": self.world.get("charger_power", 0),
            }}})
        raise AssertionError(f"想定外のGET: {url}")

    def post(self, url, headers=None, json=None, timeout=None):
        command = url.rsplit("/", 1)[-1]
        self.commands.append(command)

        if command == "wake_up":
            self.world["vehicle_state"] = "online"
            return FakeResponse(200, {"response": {"state": "online"}})

        if self._fail(command):
            return FakeResponse(200, {"response": {"result": False, "reason": "vehicle unavailable"}})

        if command == "charge_start":
            self.world["charging_state"] = "Charging"
        elif command == "charge_stop":
            self.world["charging_state"] = "Stopped"
        elif command == "set_charging_amps":
            self.world["amps"] = json["charging_amps"]
        return FakeResponse(200, {"response": {"result": True}})


class FakeWCSession:
    """自宅ウォールコネクターのローカルAPIに応答する擬似サーバー。

    差し替えるのは wall_connector.wc_session そのものなので、判定を行う
    read_vehicle_connected() / read_serial() の実装は本物が動く。JSON解釈失敗や
    キー欠落といった「判定不能」の各パターンも、実際のコードで検証できる。

    world のキー（すべて省略可。既定は「自宅のウォールコネクターに接続中」）::

        wc_vehicle_connected  vitals の vehicle_connected（Noneで真偽値以外を再現）
        wc_http               HTTPステータス（200以外なら判定不能）
        wc_raise              True で接続例外（電源断・LAN障害を再現）
        wc_json_error         True で本文がJSONとして解釈できない
        wc_omit_field         True で vehicle_connected キー自体が無い
        wc_serial             /api/1/version が返す serial_number
        wc_fail_first         最初のN回の呼び出しだけ失敗させる（再試行の検証用）
    """

    def __init__(self, world):
        self.world = world
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)

        # 最初のN回だけ失敗させる。その場での取り直しが効いているかを見るため。
        fail_first = self.world.get("wc_fail_first", 0)
        if fail_first and len(self.calls) <= fail_first:
            raise FakeReadTimeout("transient")

        if self.world.get("wc_raise"):
            raise FakeReadTimeout(
                "HTTPConnectionPool(host='192.0.2.1', port=80): Read timed out. (read timeout=5)"
            )

        status = self.world.get("wc_http", 200)
        if status != 200:
            return FakeResponse(status, {})
        if self.world.get("wc_json_error"):
            return FakeResponse(200, None, json_error=True)

        if url.endswith("/api/1/version"):
            return FakeResponse(200, {
                "firmware_version": "26.18.0+gtest",
                "part_number": "1457768-02-H",
                "serial_number": self.world.get("wc_serial", "TESTWC0000000000"),
            })

        if url.endswith("/api/1/vitals"):
            vitals = {
                "contactor_closed": True,
                "vehicle_current_a": 21.3,
                "session_energy_wh": 9738.1,
            }
            if not self.world.get("wc_omit_field"):
                vitals["vehicle_connected"] = self.world.get("wc_vehicle_connected", True)
            return FakeResponse(200, vitals)

        raise AssertionError(f"想定外のウォールコネクターGET: {url}")


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


class Result:
    """1回のシミュレーション結果。テストはこれに対してアサーションする。"""

    def __init__(self, commands, logs, override_writes, world, module, refresh_calls=None,
                 wc_calls=None):
        self.commands = commands
        self.logs = logs
        self.override_writes = override_writes
        self.world = world
        self.module = module
        # トークンリフレッシュが呼ばれた時刻（仮想時計）の一覧
        self.refresh_calls = refresh_calls if refresh_calls is not None else []
        # 自宅ウォールコネクターへ投げたURLの一覧
        self.wc_calls = wc_calls if wc_calls is not None else []

    def count(self, command):
        return self.commands.count(command)

    def messages(self, level=None):
        return [m for lv, m in self.logs if level is None or lv == level]

    def has_log(self, substring, level=None):
        return any(substring in m for m in self.messages(level))

    def commands_after(self, command):
        """指定コマンドが最後に送られた以降に送られたコマンドの一覧。"""
        if command not in self.commands:
            return list(self.commands)
        return self.commands[len(self.commands) - self.commands[::-1].index(command):]


def _load_module(tmp_path):
    """tesla_solar_charger を、設定・ログ・トークンをテスト用に向けて読み込む。

    ログはRotatingFileHandlerが相対パスで開くため、import前にtmpへchdirしておく。

    トークンファイルの用意が重要。main() は

        if not os.path.exists(TOKEN_FILE) or refresh_token is None:
            ... HTTPServer(('127.0.0.1', 8000)) を立てて認証コールバックを待つ ...

    という初回認証フローを持っており、ファイルが無いと **永久にブロックする**。
    tesla_tokens.json は .gitignore 済みなので開発機には在ってもCIには無い。
    ここで tmp 上のダミーを指しておかないと、ローカルだけ通ってCIでハングする。
    """
    os.environ["TESLA_CONFIG_PATH"] = CI_CONFIG

    token_file = tmp_path / "tesla_tokens.json"
    token_file.write_text(
        '{"access_token": "dummy", "refresh_token": "dummy", "token_expires_at": 0}',
        encoding="utf-8",
    )
    os.environ["TESLA_TOKEN_PATH"] = str(token_file)

    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"tsc_under_test_{next(_module_counter)}"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(PROJECT_ROOT, "tesla_solar_charger.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


@pytest.fixture
def run_loop(tmp_path):
    """充電制御ループを仮想時間で回すランナーを返す。

    使い方::

        res = run_loop(
            world={"vehicle_state": "online", "charging_state": "Complete", "amps": 48},
            start="2026-07-15 03:28:00",
            budget_sec=4 * 3600,
            override=True,
        )
        assert res.count("charge_start") == 0
    """

    def _run(world, start, budget_sec, override=False, house_power=-3000, on_poll=None,
             wall_connector_host=None, wall_connector_serial=None):
        module = _load_module(tmp_path)

        # ci_config.json の値を上書きする。空文字を渡すと外出先判定そのものが無効になる。
        if wall_connector_host is not None:
            module.WALL_CONNECTOR_HOST = wall_connector_host
        if wall_connector_serial is not None:
            module.WALL_CONNECTOR_SERIAL = wall_connector_serial

        for handler in list(module.logger.handlers):
            module.logger.removeHandler(handler)
        capture = CapturingHandler()
        module.logger.addHandler(capture)
        module.logger.setLevel(logging.INFO)

        session = FakeSession(world)
        module.proxy_session = session

        # 自宅ウォールコネクターへの実HTTPを止める。差し替えを忘れると
        # ci_config.json の 192.0.2.1（RFC 5737 のドキュメント用アドレス）へ
        # 毎サイクル接続を試み、テストが黙って遅くなる。
        # tesla_solar_charger は read_vehicle_connected / read_serial を名前で
        # import しているが、どちらも呼び出し時に wall_connector.wc_session を見るため、
        # セッションを差し替えれば判定ロジック本体は本物が動く。
        wc_session = FakeWCSession(world)
        wall_connector.wc_session = wc_session

        start_epoch = real_time.mktime(real_time.strptime(start, "%Y-%m-%d %H:%M:%S"))
        module.time = FakeTime(start_epoch, budget_sec)

        override_state = {
            "enabled": override,
            "updated_at": start_epoch - 3600,
            "writes": [],
        }
        module.read_override_state = lambda: (override_state["enabled"], override_state["updated_at"])

        def _write_override(enabled):
            override_state["enabled"] = enabled
            override_state["writes"].append(enabled)

        module.write_override = _write_override

        # house_power はスカラーのほか、リストでも渡せる。1回の測定ごとに次の値へ進み、
        # 使い切ったら最後の値を返し続ける。起動前後で余剰が変わる状況を再現するために要る。
        power_sequence = list(house_power) if isinstance(house_power, (list, tuple)) else None
        power_reads = []

        def _read_power():
            if power_sequence is None:
                power_reads.append(house_power)
                return house_power
            index = min(len(power_reads), len(power_sequence) - 1)
            power_reads.append(power_sequence[index])
            return power_sequence[index]

        module.get_remo_power_smoothed = _read_power
        module.load_tokens = lambda: True
        module.access_token = "dummy-access-token"
        module.refresh_token = "dummy-refresh-token"
        module.token_expires_at = start_epoch + 10 ** 6

        # 実物の refresh_tesla_token() は auth.tesla.com へPOSTする。401検出の経路など
        # リフレッシュを踏むテストで実APIを叩かないよう、必ず差し替えておく。
        refresh_calls = []

        def _fake_refresh():
            refresh_calls.append(module.time.now)
            module.access_token = "refreshed-access-token"
            module.token_expires_at = module.time.now + 28800
            return True

        module.refresh_tesla_token = _fake_refresh

        # サイクルごとに world を変化させたい場合のフック（車両リスト取得時に呼ばれる）
        if on_poll:
            original_get = session.get

            def _hooked_get(url, **kwargs):
                if url.endswith("/api/1/vehicles"):
                    on_poll(module.time.now - start_epoch, world)
                return original_get(url, **kwargs)

            session.get = _hooked_get

        try:
            module.main()
        except StopSim:
            pass

        return Result(
            session.commands, capture.records, override_state["writes"], world, module,
            refresh_calls, wc_session.calls
        )

    return _run
