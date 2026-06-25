import os
import sys
import json
import html
import hmac
import logging
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict

from override_state import read_override, write_override

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

LOG_FILE: str = "control_server.log"
MAX_LOG_SIZE: int = 5 * 1024 * 1024
BACKUP_COUNT: int = 3

logger = logging.getLogger("ControlServer")
logger.setLevel(logging.INFO)
formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_SIZE, backupCount=BACKUP_COUNT, encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

BASE_DIR: str = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE: str = os.environ.get("TESLA_CONFIG_PATH", os.path.join(BASE_DIR, "tesla_config.json"))

if not os.path.exists(CONFIG_FILE):
    logger.critical(f"設定ファイル（{CONFIG_FILE}）が見つかりません。")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    config: Dict[str, Any] = json.load(f)

CONTROL_PORT: int = int(config.get("CONTROL_PORT", 8090))
CONTROL_TOKEN: str = str(config.get("CONTROL_TOKEN", ""))

if not CONTROL_TOKEN:
    logger.critical("CONTROL_TOKEN が tesla_config.json に設定されていません。第三者による無断操作を防ぐため起動を中止します。")
    sys.exit(1)

PAGE_TEMPLATE: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Tesla 充電コントロール</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
         background:#0b0f14; color:#e6edf3; margin:0; padding:24px;
         display:flex; flex-direction:column; align-items:center; min-height:100vh; box-sizing:border-box; }
  h1 { font-size:18px; font-weight:600; margin-bottom:4px; text-align:center; }
  p.sub { color:#8b949e; font-size:13px; margin-top:0; margin-bottom:32px; text-align:center; max-width:280px; }
  .status { font-size:15px; margin-bottom:24px; padding:8px 16px; border-radius:8px; background:#161b22; text-align:center; }
  .status.on { color:#3fb950; }
  .status.off { color:#8b949e; }
  button#toggle { width:220px; height:220px; border-radius:50%; border:none; font-size:16px;
                  font-weight:700; cursor:pointer; transition: background .2s; }
  button#toggle.off { background:#21262d; color:#e6edf3; }
  button#toggle.on { background:#238636; color:#ffffff; }
  button#toggle:disabled { opacity:0.5; }
  .updated { margin-top:24px; font-size:12px; color:#6e7681; text-align:center; }
</style>
</head>
<body>
  <h1>Tesla 充電コントロール</h1>
  <p class="sub">ONにすると太陽光の発電状況に関わらず、フル充電モードで動作します。</p>
  <div class="status off" id="status">読み込み中...</div>
  <button id="toggle" class="off" disabled>...</button>
  <div class="updated" id="updated"></div>

<script>
const TOKEN = "__TOKEN__";

async function fetchStatus() {
  const res = await fetch(`/api/status?token=${encodeURIComponent(TOKEN)}`);
  if (!res.ok) throw new Error("status fetch failed");
  return res.json();
}

async function setOverride(enabled) {
  const res = await fetch(`/api/override?token=${encodeURIComponent(TOKEN)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled })
  });
  if (!res.ok) throw new Error("override update failed");
  return res.json();
}

function render(state) {
  const statusEl = document.getElementById("status");
  const btn = document.getElementById("toggle");
  const updatedEl = document.getElementById("updated");
  btn.disabled = false;
  if (state.manual_override) {
    statusEl.textContent = "フル充電モード：ON";
    statusEl.className = "status on";
    btn.textContent = "OFFにする（太陽光優先に戻す）";
    btn.className = "on";
  } else {
    statusEl.textContent = "太陽光追従モード：通常稼働中";
    statusEl.className = "status off";
    btn.textContent = "ONにする（フル充電）";
    btn.className = "off";
  }
  updatedEl.textContent = "最終更新: " + new Date().toLocaleTimeString("ja-JP");
}

async function refresh() {
  try {
    const state = await fetchStatus();
    render(state);
  } catch (e) {
    document.getElementById("status").textContent = "通信エラー";
  }
}

document.getElementById("toggle").addEventListener("click", async () => {
  const btn = document.getElementById("toggle");
  const currentlyOn = btn.classList.contains("on");
  btn.disabled = true;
  try {
    const state = await setOverride(!currentlyOn);
    render(state);
  } catch (e) {
    alert("切替に失敗しました。通信状態を確認してください。");
    btn.disabled = false;
  }
});

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


def render_page(token: str) -> str:
    return PAGE_TEMPLATE.replace("__TOKEN__", html.escape(token, quote=True))


class ControlHandler(BaseHTTPRequestHandler):
    """太陽光追従ロジックのマニュアル・オーバーライドをスマホから切替えるためのHTTPハンドラー"""

    def _check_token(self, query: Dict[str, list]) -> bool:
        supplied = self.headers.get("X-Control-Token") or query.get("token", [None])[0]
        if not supplied:
            return False
        return hmac.compare_digest(supplied, CONTROL_TOKEN)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query: Dict[str, list] = parse_qs(parsed.query)

        if parsed.path == "/api/status":
            if not self._check_token(query):
                self._send_json(403, {"error": "invalid token"})
                return
            self._send_json(200, {"manual_override": read_override()})
            return

        if parsed.path == "/":
            if not self._check_token(query):
                body = "Forbidden: invalid or missing token".encode("utf-8")
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = render_page(CONTROL_TOKEN).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query: Dict[str, list] = parse_qs(parsed.query)

        if parsed.path != "/api/override":
            self.send_response(404)
            self.end_headers()
            return

        if not self._check_token(query):
            self._send_json(403, {"error": "invalid token"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw_body = self.rfile.read(length) if length else b""
        try:
            payload: Dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
            return

        enabled = bool(payload.get("enabled"))
        write_override(enabled)
        logger.info(f"マニュアル・オーバーライドを {'有効（フル充電）' if enabled else '無効（太陽光追従に復帰）'} に切替えました。")
        self._send_json(200, {"manual_override": enabled})

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format % args)


def main() -> None:
    server = HTTPServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    logger.info("=========================================================================")
    logger.info(f"スマホ操作用コントロールサーバーをポート {CONTROL_PORT} で起動しました。")
    logger.info("=========================================================================")
    server.serve_forever()


if __name__ == "__main__":
    main()
