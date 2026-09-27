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


def test_BOM付き設定ファイルでも起動できる(tmp_path):
    config_file = tmp_path / "config_bom.json"
    content = b"\xef\xbb\xbf" + json.dumps({"CONTROL_PORT": 0, "CONTROL_TOKEN": "bom-token"}).encode("utf-8")
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
        assert module.CONTROL_TOKEN == "bom-token"
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
