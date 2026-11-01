"""スマホ操作用コントロールサーバーのHTTP境界だけを検証する。

対象は「トークン検査を通ったあと、どの状態ファイルへ何を書くか」に絞る。画面の
見た目は対象にしない。実サーバーを一時ポートで立てるのは、ハンドラーが
BaseHTTPRequestHandler と密結合しており、切り離すほうが本物から遠くなるためである。

control_server.py は import 時に設定を読み、CONTROL_TOKEN が無ければ sys.exit(1) する。
そのため import より前に TESLA_CONFIG_PATH を一時ファイルへ向ける必要がある。
ログも相対パスで開くので、conftest.py の _load_module と同じく tmp へ chdir しておく。
"""
import html
import importlib.util
import itertools
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)

TOKEN = "0123456789abcdef" * 4

_module_counter = itertools.count()

# server フィクスチャが読み込んだ control_server モジュール。テストから定数や関数へ触るために置く。
# フィクスチャの戻り値は (ベースURL, 状態ファイル) のまま変えない。
_loaded: dict = {}


@pytest.fixture
def control_module(server):
    return _loaded["module"]


@pytest.fixture
def server(tmp_path):
    """一時ポートでコントロールサーバーを起動し、(ベースURL, 状態ファイルのパス) を返す。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"CONTROL_PORT": 0, "CONTROL_TOKEN": TOKEN}),
        encoding="utf-8",
    )
    state_file = tmp_path / "override_state.json"

    os.environ["TESLA_CONFIG_PATH"] = str(config_file)

    # override_state は TESLA_OVERRIDE_PATH を import 時に1回だけ読む。他のテストが
    # 先に import しているため、ここで環境変数を立てても遅い。属性を直接差し替える。
    # 差し替えないと、実行した開発機のリポジトリ直下へ override_state.json を書く。
    import override_state
    previous_state_path = override_state.OVERRIDE_FILE
    override_state.OVERRIDE_FILE = str(state_file)
    # vehicle_status.json も同じ理由で差し替える。差し替えないと、開発機にある本物の
    # 車両の状態を画面APIが返し、手元の状態でテスト結果が変わる。
    import vehicle_status
    previous_vehicle_path = vehicle_status.VEHICLE_STATUS_FILE
    vehicle_status.VEHICLE_STATUS_FILE = str(tmp_path / "vehicle_status.json")

    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"control_server_under_test_{next(_module_counter)}"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(PROJECT_ROOT, "control_server.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _loaded["module"] = module
    finally:
        os.chdir(previous_cwd)

    # ポート0でバインドし、OSが割り当てた番号を使う。固定ポートだと開発機で
    # 本物のコントロールサーバーが動いている場合に衝突する。
    httpd = module.ControlServer(("127.0.0.1", 0), module.ControlHandler)
    # 既定の確認間隔0.5秒では、各テストのshutdown()が最大0.5秒待つ。
    # テスト用サーバーだけ0.01秒にし、リクエスト処理は実装どおりに保つ。
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state_file
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        override_state.OVERRIDE_FILE = previous_state_path
        vehicle_status.VEHICLE_STATUS_FILE = previous_vehicle_path


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def _post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as res:
        return res.status, json.loads(res.read().decode("utf-8"))


def test_状態は各スイッチと目標充電率と車両の状態を返す(server):
    base, _ = server
    status, body = _get(f"{base}/api/status?token={TOKEN}")
    assert status == 200
    assert body["manual_override"] is False
    assert body["away_probe"] is False
    assert body["charge_target_soc"] is None
    # 制御ループがまだ車両を読んでいない状態。キーは揃っていて値が None になる。
    assert body["vehicle"]["battery_level"] is None


def test_外出先の充電記録を切替えられる(server):
    base, state_file = server
    _, body = _post(f"{base}/api/away_probe?token={TOKEN}", {"enabled": True})
    assert body["away_probe"] is True

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["away_probe"] is True
    assert saved["away_probe_updated_at"] > 0


def test_片方の切替でもう片方が消えない(server):
    """状態ファイルを全体上書きしていた頃の退行を検出する。"""
    base, state_file = server
    _post(f"{base}/api/override?token={TOKEN}", {"enabled": True})
    _, body = _post(f"{base}/api/away_probe?token={TOKEN}", {"enabled": True})
    assert (body["manual_override"], body["away_probe"]) == (True, True)

    _, body = _post(f"{base}/api/away_probe?token={TOKEN}", {"enabled": False})
    assert body["manual_override"] is True, "記録の切替でフル充電モードが消えた"

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["manual_override"] is True


@pytest.mark.parametrize("path", ["/api/override", "/api/away_probe", "/api/charge_target"])
def test_トークンが違えば書き込ませない(server, path):
    base, state_file = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}{path}?token=wrong-token", {"enabled": True})
    assert exc.value.code == 403
    assert not state_file.exists(), "検査に失敗したのに状態を書いている"


def test_知らないパスは404を返す(server):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/api/unknown?token={TOKEN}", {"enabled": True})
    assert exc.value.code == 404


def test_不正なバイト列をPOSTしても400を返す(server):
    base, _ = server
    request = urllib.request.Request(
        f"{base}/api/override?token={TOKEN}",
        data=b"\xff\xfe\xfd",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        with urllib.request.urlopen(request, timeout=5):
            pass
    assert exc.value.code == 400
    assert json.loads(exc.value.read().decode("utf-8")) == {"error": "invalid json"}


@pytest.mark.parametrize("payload", [[], {"enabled": "false"}, {"enabled": 1}, {}])
def test_切替APIは真偽値以外を保存しない(server, payload):
    base, state_file = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/api/override?token={TOKEN}", payload)
    assert exc.value.code == 400
    assert not state_file.exists()


def test_本文が4096バイトを超えたら413を返す(server):
    base, state_file = server
    request = urllib.request.Request(
        f"{base}/api/override?token={TOKEN}", data=b"x" * 4097, method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 413
    assert not state_file.exists()


def test_ヘッダーを途中まで送った接続が他の要求を止めない(server):
    base, _ = server
    with socket.create_connection(("127.0.0.1", int(base.rsplit(":", 1)[1])), timeout=5) as slow:
        slow.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
        status, _ = _get(f"{base}/api/status?token={TOKEN}")
        assert status == 200
        slow.sendall(b"\r\n")
        assert b"403" in slow.recv(1024)


def test_ヘッダーを小出しに送り続ける接続は締切で閉じられる(server, control_module):
    """受信ごとの5秒タイムアウトは、5秒未満の間隔で送り続ける接続を打ち切れない。
    処理全体の締切（REQUEST_DEADLINE_SEC）が無いと、16接続で枠を占有し続けられる。
    締切そのものの長さ（15秒）ではなく、締切を過ぎた接続が閉じられることを見る。"""
    base, _ = server
    port = int(base.rsplit(":", 1)[1])
    previous = control_module.REQUEST_DEADLINE_SEC
    control_module.REQUEST_DEADLINE_SEC = 0.5
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as slow:
            slow.sendall(b"GET / HTTP/1.1\r\n")
            time.sleep(0.3)
            slow.sendall(b"Host: localhost\r\n")   # 締切前の送信は受け付けられる
            time.sleep(0.5)                           # ここで締切（0.5秒）を過ぎる
            try:
                slow.sendall(b"\r\n")
                received = slow.recv(1024)
            except OSError:
                received = b""
            assert received == b"" or b"HTTP/1.0 403" not in received

        # 枠は解放され、次の正規の要求に応答する
        status, _ = _get(f"{base}/api/status?token={TOKEN}")
        assert status == 200
    finally:
        control_module.REQUEST_DEADLINE_SEC = previous


def test_締切で閉じた接続への応答の失敗はトレースバックを出さない(server, control_module, capfd, monkeypatch):
    """締切で閉じた接続にも、ハンドラーは受信済みの部分で応答を書こうとして BrokenPipeError になる。
    既定の handle_error() はこれをトレースバックとして標準エラー（journal）へ出していた（DEPLOY_LOG v0.8.0）。"""
    base, _ = server
    port = int(base.rsplit(":", 1)[1])
    logged = []
    monkeypatch.setattr(control_module.logger, "info", lambda message: logged.append(message))
    monkeypatch.setattr(control_module, "REQUEST_DEADLINE_SEC", 0.3)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as slow:
        slow.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
        time.sleep(0.6)                               # 締切（0.3秒）を過ぎる
    status, _ = _get(f"{base}/api/status?token={TOKEN}")
    assert status == 200

    assert "Traceback" not in capfd.readouterr().err
    assert any("BrokenPipeError" in message or "ConnectionResetError" in message for message in logged)


@pytest.mark.parametrize("error", [BrokenPipeError(32, "Broken pipe"), ConnectionResetError(104, "reset")])
def test_相手が閉じた接続の例外は1行で記録する(control_module, capfd, monkeypatch, error):
    logged = []
    monkeypatch.setattr(control_module.logger, "info", lambda message: logged.append(message))
    httpd = control_module.ControlServer(("127.0.0.1", 0), control_module.ControlHandler)
    try:
        try:
            raise error
        except OSError:
            httpd.handle_error(None, ("192.0.2.1", 50000))
    finally:
        httpd.server_close()
    assert "Traceback" not in capfd.readouterr().err
    assert logged == [f"192.0.2.1 との接続は、要求の処理中に閉じられていました（{type(error).__name__}）。"]


def test_それ以外の例外は従来どおりトレースバックを出す(control_module, capfd):
    httpd = control_module.ControlServer(("127.0.0.1", 0), control_module.ControlHandler)
    try:
        try:
            raise ValueError("unexpected")
        except ValueError:
            httpd.handle_error(None, ("192.0.2.1", 50000))
    finally:
        httpd.server_close()
    err = capfd.readouterr().err
    assert "Traceback" in err and "ValueError: unexpected" in err


class _Recorder:
    """logger の info・warning・debug への呼び出しを、水準ごとに集める。"""

    def __init__(self, monkeypatch, logger):
        self.lines = {"info": [], "warning": [], "debug": []}
        for level, lines in self.lines.items():
            monkeypatch.setattr(logger, level, lambda message, lines=lines: lines.append(message))

    def all(self):
        return [line for lines in self.lines.values() for line in lines]


def test_403の応答はINFOで記録しトークンを含めない(server, control_module, monkeypatch):
    """2026-10-26 19:06〜19:14 の操作は、認証失敗が DEBUG でしか記録されず、成否を確認できなかった。"""
    base, _ = server
    log = _Recorder(monkeypatch, control_module.logger)
    wrong = "x" * 40
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base}/api/status?token={wrong}")
    assert exc.value.code == 403

    assert log.lines["info"] == ["127.0.0.1 からの GET /api/status に 403 を返しました。"]
    assert not any(wrong in line or TOKEN in line or "token=" in line for line in log.all())


def test_画面の読み込みはINFOで記録しトークンを含めない(server, control_module, monkeypatch):
    base, _ = server
    log = _Recorder(monkeypatch, control_module.logger)
    with urllib.request.urlopen(f"{base}/?token={TOKEN}", timeout=5) as res:
        assert res.status == 200

    assert log.lines["info"] == ["127.0.0.1 からの GET / に 200 を返しました。"]
    assert not any(TOKEN in line for line in log.all())


def test_状態APIの成功はINFOに出さない(server, control_module, monkeypatch):
    """画面は5秒ごとに /api/status を取得する。INFO に出すと画面1つで1日に約17,000行になる。"""
    base, _ = server
    log = _Recorder(monkeypatch, control_module.logger)
    for _ in range(3):
        status, _ = _get(f"{base}/api/status?token={TOKEN}")
        assert status == 200

    assert log.lines["info"] == []
    assert log.lines["debug"] == ["127.0.0.1 からの GET /api/status に 200 を返しました。"] * 3
    assert not any(TOKEN in line for line in log.all())


def test_解釈できない要求行は異常として記録しトークンを伏せる(server, control_module, monkeypatch):
    """send_error() の記録には要求行がそのまま入る。要求行には ?token= が付くことがある。"""
    base, _ = server
    log = _Recorder(monkeypatch, control_module.logger)
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        conn.sendall(f"GET /api/status?token={TOKEN} extra HTTP/1.1\r\n\r\n".encode("ascii"))
        assert b" 400 " in conn.recv(1024)

    assert any("異常を検出しました" in line and "token=***" in line for line in log.lines["info"])
    assert any(line.endswith("に 400 を返しました。") for line in log.lines["info"])
    assert not any(TOKEN in line for line in log.all())


def test_受信の時間切れを記録する(server, control_module, monkeypatch):
    base, _ = server
    log = _Recorder(monkeypatch, control_module.logger)
    monkeypatch.setattr(control_module, "CLIENT_TIMEOUT_SEC", 0.2)
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
        assert conn.recv(1024) == b""                  # 何も送らず、サーバーが閉じるまで待つ

    assert any("異常を検出しました：Request timed out" in line for line in log.lines["info"])


def test_締切で閉じた接続を記録する(server, control_module, monkeypatch):
    base, _ = server
    log = _Recorder(monkeypatch, control_module.logger)
    monkeypatch.setattr(control_module, "REQUEST_DEADLINE_SEC", 0.3)
    port = int(base.rsplit(":", 1)[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as slow:
        slow.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
        time.sleep(0.6)                               # 締切（0.3秒）を過ぎる
    status, _ = _get(f"{base}/api/status?token={TOKEN}")
    assert status == 200

    assert "127.0.0.1 との接続を、処理の締切（0.3秒）を過ぎたため閉じました。" in log.lines["info"]


def test_同時接続の上限で閉じた接続をWARNINGで記録する(control_module, monkeypatch):
    """枠が埋まっている間の要求は応答なしで閉じられる。記録が無いと、操作が効かない原因として判別できない。"""
    log = _Recorder(monkeypatch, control_module.logger)
    httpd = control_module.ControlServer(("127.0.0.1", 0), control_module.ControlHandler)
    ours, theirs = socket.socketpair()
    try:
        for _ in range(control_module.MAX_CLIENTS):
            assert httpd._slots.acquire(blocking=False)
        httpd.process_request(ours, ("192.0.2.1", 50000))
        assert theirs.recv(1024) == b""                # 応答なしで閉じられている
    finally:
        theirs.close()
        httpd.server_close()

    assert log.lines["warning"] == [
        "同時接続が上限（16件）に達しているため、192.0.2.1 からの接続を応答せずに閉じました。"
    ]


@pytest.mark.parametrize("text, expected", [
    ("/api/status?token=abc&x=1", "/api/status?token=***&x=1"),
    ("'GET /?TOKEN=abc HTTP/1.1'", "'GET /?TOKEN=*** HTTP/1.1'"),
    ("a\r\nb\x00", "a\\x0d\\x0ab\\x00"),
])
def test_記録用の文字列はトークンと制御文字を伏せる(control_module, text, expected):
    assert control_module.text_for_log(text) == expected


def test_記録用の文字列はトークンの値そのものも伏せ長さを抑える(control_module):
    assert control_module.text_for_log(f"/x/{TOKEN}") == "/x/***"
    long_text = "a" * 500
    assert control_module.text_for_log(long_text) == "a" * control_module.LOG_TEXT_MAX_CHARS + "…"


def _refuse(httpd, address):
    """枠が埋まった状態で1接続を渡し、応答なしで閉じられたことを確かめる。"""
    ours, theirs = socket.socketpair()
    try:
        httpd.process_request(ours, (address, 50000))
        assert theirs.recv(1024) == b""
    finally:
        theirs.close()


def test_上限による拒否は間隔内は数えるだけにし枠が空いたらまとめて記録する(control_module, monkeypatch):
    """拒否1件ごとに1行を書くと、枠を埋め続ける接続があるとき control_server.log から
    調べたい時間帯の記録を押し出す（PR #55 の自動レビュー）。"""
    log = _Recorder(monkeypatch, control_module.logger)
    httpd = control_module.ControlServer(("127.0.0.1", 0), control_module.ControlHandler)
    try:
        for _ in range(control_module.MAX_CLIENTS):
            assert httpd._slots.acquire(blocking=False)
        for address in ["192.0.2.1", "192.0.2.2", "192.0.2.1", "192.0.2.3", "192.0.2.2"]:
            _refuse(httpd, address)
        assert log.lines["warning"] == [
            "同時接続が上限（16件）に達しているため、192.0.2.1 からの接続を応答せずに閉じました。"
        ]

        # 枠が1つ空き、次の接続を受け付けた時点で、数えた4件を1行で記録する
        httpd._slots.release()
        ours, theirs = socket.socketpair()
        theirs.close()                                  # 要求を送らずに閉じた接続として処理させる
        httpd.process_request(ours, ("192.0.2.9", 50000))
    finally:
        httpd.server_close()

    assert log.lines["warning"][1:] == [
        "同時接続の上限による拒否が終わりました。前回の記録の後に、同じ理由で 4 件を閉じていました"
        "（相手：192.0.2.2、192.0.2.1、192.0.2.3）。"
    ]


def test_上限による拒否は間隔を過ぎたら数えた件数とともに記録する(control_module, monkeypatch):
    log = _Recorder(monkeypatch, control_module.logger)
    clock = [1000.0]
    monkeypatch.setattr(control_module, "time", type("Clock", (), {"monotonic": staticmethod(lambda: clock[0])}))
    monkeypatch.setattr(control_module, "REFUSAL_LOG_MAX_ADDRESSES", 2)
    httpd = control_module.ControlServer(("127.0.0.1", 0), control_module.ControlHandler)
    try:
        for _ in range(control_module.MAX_CLIENTS):
            assert httpd._slots.acquire(blocking=False)
        _refuse(httpd, "192.0.2.1")
        for address in ["192.0.2.2", "192.0.2.3", "192.0.2.4"]:
            clock[0] += 10.0
            _refuse(httpd, address)
        clock[0] += 31.0                                # 最初の記録から61秒
        _refuse(httpd, "192.0.2.5")
    finally:
        httpd.server_close()

    assert log.lines["warning"] == [
        "同時接続が上限（16件）に達しているため、192.0.2.1 からの接続を応答せずに閉じました。",
        "同時接続が上限（16件）に達しているため、192.0.2.5 からの接続を応答せずに閉じました。"
        "前回の記録の後に、同じ理由で 3 件を閉じていました（相手：192.0.2.2、192.0.2.3 ほか）。",
    ]


class _FakeRequest:
    def __init__(self):
        self.shutdowns = 0

    def shutdown(self, how):
        self.shutdowns += 1


def test_処理が終わった後に発火した締切は記録も切断もしない(control_module, monkeypatch):
    """Timer.cancel() は発火済みのタイマーを止められない。終わった要求を「締切で閉じた」と記録しない。"""
    log = _Recorder(monkeypatch, control_module.logger)
    state = control_module._RequestState()
    state.finished = True
    request = _FakeRequest()
    control_module.ControlServer._abort_request(request, ("192.0.2.1", 50000), state)
    assert request.shutdowns == 0
    assert log.all() == []


def test_処理中に発火した締切は記録して切断する(control_module, monkeypatch):
    log = _Recorder(monkeypatch, control_module.logger)
    state = control_module._RequestState()
    request = _FakeRequest()
    control_module.ControlServer._abort_request(request, ("192.0.2.1", 50000), state)
    assert request.shutdowns == 1
    assert log.lines["info"] == [
        f"192.0.2.1 との接続を、処理の締切（{control_module.REQUEST_DEADLINE_SEC}秒）を過ぎたため閉じました。"
    ]


def test_締切の記録はロックを放してから書く(control_module, monkeypatch):
    """ログの書き込みが遅いと、ロックの中で書いた場合は同じ接続の後始末が待たされる。
    記録を書く時点で state.lock が空いていること、切断が済んでいることを確かめる。"""
    state = control_module._RequestState()
    request = _FakeRequest()
    observed = []
    monkeypatch.setattr(
        control_module.logger, "info",
        lambda message: observed.append((state.lock.locked(), request.shutdowns)),
    )
    control_module.ControlServer._abort_request(request, ("192.0.2.1", 50000), state)
    assert observed == [(False, 1)]


class _Unprintable:
    def __str__(self):
        raise RuntimeError("str")

    def __repr__(self):
        raise RuntimeError("repr")


@pytest.mark.parametrize("format_, args, expected", [
    ("100% の要求", (), "100% の要求"),                                    # 引数が無ければ展開しない
    ("%s と %s", ("a",), "書式を展開できませんでした：'%s と %s' ('a')"),        # 引数の数が合わない
    ("%d 件", ("a",), "書式を展開できませんでした：'%d 件' ('a')"),              # 型が合わない
    ("%(x)s", {"y": 1}, "書式を展開できませんでした：'%(x)s' ('y')"),            # KeyError
    ("%s", (_Unprintable(),), "書式を展開できませんでした：'%s' (<表示できない値>)"),  # str() も repr() も失敗する
])
def test_記録の組み立てはどう呼ばれても例外を出さない(control_module, format_, args, expected):
    assert control_module.format_for_log(format_, args) == expected


def test_記録の組み立てでも要求行のトークンを伏せる(control_module):
    assert control_module.format_for_log("code %d, message %s", (400, f"Bad request syntax ('GET /?token={TOKEN} x')")) \
        == "code 400, message Bad request syntax ('GET /?token=*** x')"


QUOTED_TOKEN = "a'b\"c\\d" + "z" * 30


@pytest.mark.parametrize("text", [
    repr(f"GET /x/{QUOTED_TOKEN} HTTP/1.1"),                               # repr() でエスケープされた形
    repr(f"GET /?token={QUOTED_TOKEN}"),                                    # 値の途中と直後に引用符がある
    f"/x/{urllib.parse.quote(QUOTED_TOKEN, safe='')}",                      # URL 符号化した形
    f"/x/{QUOTED_TOKEN}",                                                   # 生の形
])
def test_引用符やバックスラッシュを含むトークンもどの形でも伏せる(control_module, monkeypatch, text):
    monkeypatch.setattr(control_module, "_TOKEN_FORMS", control_module._token_forms(QUOTED_TOKEN))
    assert "z" * 30 not in control_module.text_for_log(text)


def test_改行として扱われうる文字をエスケープする(control_module):
    assert control_module.text_for_log("a\x85b\u2028c\u2029d\x9be\x7f") == "a\\x85b\\u2028c\\u2029d\\x9be\\x7f"


def test_画面はトークンをキャッシュと参照元へ渡さない(server):
    base, _ = server
    with urllib.request.urlopen(f"{base}/?token={TOKEN}", timeout=5) as res:
        assert res.headers["Cache-Control"] == "no-store"
        assert res.headers["Referrer-Policy"] == "no-referrer"


def test_画面と状態APIは他サイトの枠に埋め込ませない(server):
    base, _ = server
    for url in (f"{base}/?token={TOKEN}", f"{base}/api/status?token={TOKEN}"):
        with urllib.request.urlopen(url, timeout=5) as res:
            assert res.headers["X-Frame-Options"] == "DENY"
            assert res.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


@pytest.mark.parametrize("token", ["トークン", "0123456789abcdef" * 3 + "0123456789abcdé"])
def test_非ASCIIのトークンは403を返し切断しない(server, token):
    """str 同士の compare_digest は非ASCIIで TypeError を投げ、応答を返さずに切断していた。"""
    base, state_file = server
    quoted = urllib.parse.quote(token)
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(f"{base}/api/status?token={quoted}", timeout=5)
    assert exc.value.code == 403

    request = urllib.request.Request(
        f"{base}/api/override",
        data=json.dumps({"enabled": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Control-Token": token.encode("utf-8")},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 403
    assert not state_file.exists()

    # 同じサーバーが続けて正規の要求に応答できる
    status, _ = _get(f"{base}/api/status?token={TOKEN}")
    assert status == 200


def test_ヘッダーだけでトークンを渡せる(server):
    """画面のJavaScriptはURLではなく X-Control-Token ヘッダーでトークンを送る。"""
    base, state_file = server
    request = urllib.request.Request(f"{base}/api/status", headers={"X-Control-Token": TOKEN})
    with urllib.request.urlopen(request, timeout=5) as res:
        assert res.status == 200

    request = urllib.request.Request(
        f"{base}/api/override",
        data=json.dumps({"enabled": True}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Control-Token": TOKEN},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as res:
        assert json.loads(res.read().decode("utf-8"))["manual_override"] is True
    assert json.loads(state_file.read_text(encoding="utf-8"))["manual_override"] is True


@pytest.mark.parametrize("token", [
    TOKEN,
    'ab"c\\d</script>&e' + "'" + "f?g=h&i=j#k é" + "x" * 16,
])
def test_トークンは埋め込む文脈ごとに符号化される(control_module, token):
    """引用符・バックスラッシュ・"</script>"・URL区切り・非ASCIIを含む値でも、
    script の文字列リテラルは値そのまま、manifest の href と start_url は同じクエリ値になる。"""
    page = control_module.render_page(token)
    script = page[page.index("<script>"):page.index("</script>")]
    literal = script[script.index("const TOKEN = ") + len("const TOKEN = "):]
    literal = literal[:literal.index(";\n")]
    assert json.loads(literal.replace("<\\/", "</")) == token
    assert page.count("</script>") == 1

    quoted = urllib.parse.quote(token, safe="")
    href = page[page.index('href="/manifest.webmanifest?token=') + len('href="/manifest.webmanifest?token='):]
    href = href[:href.index('"')]
    assert urllib.parse.unquote(html.unescape(href)) == token

    manifest = json.loads(control_module.render_manifest(token))
    assert manifest["start_url"] == f"/?token={quoted}"
    assert urllib.parse.parse_qs(urllib.parse.urlparse(manifest["start_url"]).query)["token"] == [token]


def test_画面のスクリプトはAPI呼び出しのトークンをURLに載せない(server):
    base, _ = server
    _, _, raw = _get_bytes(f"{base}/?token={TOKEN}")
    page = raw.decode("utf-8")
    script = page[page.index("<script>"):page.index("</script>")]
    assert '"X-Control-Token": TOKEN' in script
    assert "?token=" not in script


def test_BOM付き設定ファイルでも起動できる(tmp_path):
    config_file = tmp_path / "config_bom.json"
    content = b"\xef\xbb\xbf" + json.dumps({"CONTROL_PORT": 0, "CONTROL_TOKEN": TOKEN}).encode("utf-8")
    config_file.write_bytes(content)
    previous_config = os.environ.get("TESLA_CONFIG_PATH")
    os.environ["TESLA_CONFIG_PATH"] = str(config_file)
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"control_server_bom_test_{next(_module_counter)}"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(PROJECT_ROOT, "control_server.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.CONTROL_TOKEN == TOKEN
    finally:
        os.chdir(previous_cwd)
        if previous_config is not None:
            os.environ["TESLA_CONFIG_PATH"] = previous_config
        else:
            os.environ.pop("TESLA_CONFIG_PATH", None)


@pytest.mark.parametrize("token", [
    "YOUR_RANDOM_CONTROL_TOKEN_HERE",
    "short",
    123,
    "トークン" * 8,                       # 非ASCII：fetch() がヘッダー値として送れない
    "0123456789abcdef 0123456789abcdef",  # 空白：ヘッダー値の先頭と末尾で削られる
])
def test_不正なトークンでは起動しない(tmp_path, token):
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"CONTROL_TOKEN": token}), encoding="utf-8",
    )
    previous_config = os.environ.get("TESLA_CONFIG_PATH")
    os.environ["TESLA_CONFIG_PATH"] = str(config_file)
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"control_server_placeholder_test_{next(_module_counter)}"
        spec = importlib.util.spec_from_file_location(name, os.path.join(PROJECT_ROOT, "control_server.py"))
        module = importlib.util.module_from_spec(spec)
        with pytest.raises(SystemExit) as exc:
            spec.loader.exec_module(module)
        assert exc.value.code == 1
    finally:
        os.chdir(previous_cwd)
        if previous_config is not None:
            os.environ["TESLA_CONFIG_PATH"] = previous_config
        else:
            os.environ.pop("TESLA_CONFIG_PATH", None)


def test_BOM付き状態ファイルを読める(tmp_path):
    import override_state
    state_file = tmp_path / "override_state_bom.json"
    content = b"\xef\xbb\xbf" + json.dumps({"manual_override": True, "updated_at": 1234.5}).encode("utf-8")
    state_file.write_bytes(content)
    previous_state = override_state.OVERRIDE_FILE
    override_state.OVERRIDE_FILE = str(state_file)
    try:
        assert override_state.read_override() is True
    finally:
        override_state.OVERRIDE_FILE = previous_state




def _get_bytes(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, res.headers.get("Content-Type"), res.read()


def test_manifestのアイコンはすべて配信され宣言どおりの大きさである(server):
    """any と maskable を別ファイルにしたとき、manifest と配信の許可リストがずれていないかを見る。"""
    base, _ = server
    _, _, raw = _get_bytes(f"{base}/manifest.webmanifest?token={TOKEN}")
    icons = json.loads(raw.decode("utf-8"))["icons"]

    # 集合ではなく (大きさ, purpose) の組で比べる。purpose の種類だけを見ると、
    # エントリが1件消えても残りが any と maskable を含む限り検出できない。
    declared = sorted((icon["sizes"], icon["purpose"]) for icon in icons)
    assert declared == [
        ("192x192", "any"),
        ("192x192", "maskable"),
        ("512x512", "any"),
        ("512x512", "maskable"),
    ], "any と maskable を1つの画像で兼ねているか、エントリが欠けている"

    for icon in icons:
        status, content_type, body = _get_bytes(f"{base}{icon['src']}")
        assert status == 200
        assert content_type == "image/png"
        # PNG の IHDR から幅と高さを読む
        width = int.from_bytes(body[16:20], "big")
        height = int.from_bytes(body[20:24], "big")
        assert f"{width}x{height}" == icon["sizes"], icon["src"]


def test_apple_touch_iconを配信する(server):
    base, _ = server
    status, content_type, body = _get_bytes(f"{base}/icons/apple-touch-icon-180.png")
    assert status == 200 and content_type == "image/png"
    assert int.from_bytes(body[16:20], "big") == 180


@pytest.mark.parametrize("path", ["/icons/unknown.png", "/icons/../control_server.py", "/icons/"])
def test_許可リストにないアイコンのパスは404を返す(server, path):
    base, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get_bytes(f"{base}{path}")
    assert exc.value.code == 404


# ---------------------------------------------------------------------------
# 目標充電率（docs/05_charge_target_design.md 第5.1節・第8.3節、テスト T16）
# ---------------------------------------------------------------------------

def test_目標充電率を設定し解除できる(server):
    base, state_file = server
    _, body = _post(f"{base}/api/charge_target?token={TOKEN}", {"soc": 25})
    assert body["charge_target_soc"] == 25
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["charge_target_soc"] == 25
    assert saved["charge_target_updated_at"] > 0

    _, body = _post(f"{base}/api/charge_target?token={TOKEN}", {"soc": None})
    assert body["charge_target_soc"] is None


def test_目標充電率の設定でほかのスイッチが消えない(server):
    base, _ = server
    _post(f"{base}/api/override?token={TOKEN}", {"enabled": True})
    _, body = _post(f"{base}/api/charge_target?token={TOKEN}", {"soc": 40})
    assert body["manual_override"] is True


@pytest.mark.parametrize("payload", [
    {"soc": 0},
    {"soc": 101},
    {"soc": 25.5},
    {"soc": "25"},
    {"soc": True},
    {},
])
def test_T16_目標充電率として受け付けない値は400を返す(server, payload):
    base, state_file = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(f"{base}/api/charge_target?token={TOKEN}", payload)
    assert exc.value.code == 400
    assert not state_file.exists(), "不正な値を保存している"


def test_状態APIは制御ループが書いた車両の状態を返す(server, tmp_path):
    base, _ = server
    (tmp_path / "vehicle_status.json").write_text(
        json.dumps({"battery_level": 42, "charge_limit_soc": 50, "charge_limit_restore_soc": 80,
                    "charge_limit_applied_soc": 50, "observed_at": 1790000000, "unrelated": "x"}),
        encoding="utf-8",
    )
    _, body = _get(f"{base}/api/status?token={TOKEN}")
    assert body["vehicle"]["battery_level"] == 42
    assert body["vehicle"]["charge_limit_restore_soc"] == 80
    assert "unrelated" not in body["vehicle"], "表示に使わない値まで返している"


def test_状態ファイルの不正な目標充電率は未設定として返す(server):
    base, state_file = server
    state_file.write_text(json.dumps({"charge_target_soc": "abc"}), encoding="utf-8")
    _, body = _get(f"{base}/api/status?token={TOKEN}")
    assert body["charge_target_soc"] is None
