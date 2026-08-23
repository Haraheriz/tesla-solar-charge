"""スマホ操作用コントロールサーバーのHTTP境界だけを検証する。

対象は「トークン検査を通ったあと、どの状態ファイルへ何を書くか」に絞る。画面の
見た目は対象にしない。実サーバーを一時ポートで立てるのは、ハンドラーが
BaseHTTPRequestHandler と密結合しており、切り離すほうが本物から遠くなるためである。

control_server.py は import 時に設定を読み、CONTROL_TOKEN が無ければ sys.exit(1) する。
そのため import より前に TESLA_CONFIG_PATH を一時ファイルへ向ける必要がある。
ログも相対パスで開くので、conftest.py の _load_module と同じく tmp へ chdir しておく。
"""
import importlib.util
import itertools
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)

TOKEN = "test-control-token"

_module_counter = itertools.count()


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

    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"control_server_under_test_{next(_module_counter)}"
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(PROJECT_ROOT, "control_server.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        os.chdir(previous_cwd)

    # ポート0でバインドし、OSが割り当てた番号を使う。固定ポートだと開発機で
    # 本物のコントロールサーバーが動いている場合に衝突する。
    httpd = module.HTTPServer(("127.0.0.1", 0), module.ControlHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state_file
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        override_state.OVERRIDE_FILE = previous_state_path


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


def test_状態は両方のフラグを返す(server):
    base, _ = server
    status, body = _get(f"{base}/api/status?token={TOKEN}")
    assert status == 200
    assert body == {"manual_override": False, "away_probe": False}


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
    assert body == {"manual_override": True, "away_probe": True}

    _, body = _post(f"{base}/api/away_probe?token={TOKEN}", {"enabled": False})
    assert body["manual_override"] is True, "記録の切替でフル充電モードが消えた"

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["manual_override"] is True


@pytest.mark.parametrize("path", ["/api/override", "/api/away_probe"])
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
