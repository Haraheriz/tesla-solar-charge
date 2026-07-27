"""充電制御ループをネットワークなしで駆動するためのテスト基盤。

tesla_solar_charger.main() は本来「無限ループ・実API・実時間」で動く。ここでは

  * proxy_session  → 擬似Tesla APIプロキシ（FakeSession）
  * time           → 仮想時計（FakeTime）。sleepで時間が飛び、予算を超えたら停止
  * Remo / override_state / トークン → 差し替え

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


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
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
            # main() は起動時にも車両リストを取得し、失敗すると sys.exit(1) する。
            # ループ内の通信障害だけを再現したい場合は vehicle_list_ok_calls で
            # 「最初のN回だけ成功させる」よう指定する。
            ok_calls = self.world.get("vehicle_list_ok_calls")
            if ok_calls is not None and self.vehicle_list_calls > ok_calls:
                return FakeResponse(500, {})
            if self.world.get("vehicle_list_http", 200) != 200:
                return FakeResponse(self.world["vehicle_list_http"], {})
            return FakeResponse(200, {"response": [
                {"vin": "TESTVIN0000000000", "state": self.world["vehicle_state"]}
            ]})
        if "vehicle_data" in url:
            if self.world.get("charge_state_http", 200) != 200:
                return FakeResponse(self.world["charge_state_http"], {})
            return FakeResponse(200, {"response": {"charge_state": {
                "charging_state": self.world["charging_state"],
                "charge_current_request": self.world["amps"],
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


class CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


class Result:
    """1回のシミュレーション結果。テストはこれに対してアサーションする。"""

    def __init__(self, commands, logs, override_writes, world, module):
        self.commands = commands
        self.logs = logs
        self.override_writes = override_writes
        self.world = world
        self.module = module

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

    def _run(world, start, budget_sec, override=False, house_power=-3000, on_poll=None):
        module = _load_module(tmp_path)

        for handler in list(module.logger.handlers):
            module.logger.removeHandler(handler)
        capture = CapturingHandler()
        module.logger.addHandler(capture)
        module.logger.setLevel(logging.INFO)

        session = FakeSession(world)
        module.proxy_session = session

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

        module.get_remo_power_smoothed = lambda: house_power
        module.load_tokens = lambda: True
        module.access_token = "dummy-access-token"
        module.refresh_token = "dummy-refresh-token"
        module.token_expires_at = start_epoch + 10 ** 6

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

        return Result(session.commands, capture.records, override_state["writes"], world, module)

    return _run
