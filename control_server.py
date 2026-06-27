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

ICONS_DIR: str = os.path.join(BASE_DIR, "icons")

PAGE_TEMPLATE: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Tesla充電切替</title>
<link rel="manifest" href="/manifest.webmanifest?token=__TOKEN__">
<link rel="icon" href="/icons/icon-192.png">
<link rel="apple-touch-icon" href="/icons/icon-192.png">
<meta name="theme-color" content="#0b0f14">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Tesla充電切替">
<style>
  :root { color-scheme: dark; }
  * { -webkit-tap-highlight-color: transparent; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", Arial, sans-serif;
         background:#0b0f14; color:#e6edf3; margin:0;
         /* iOSのDynamic Island/ノッチ、Androidのジェスチャーナビゲーション・カットアウトに
            重ならないよう、固定24pxではなくセーフエリアの方を優先して確保する。 */
         padding-top: max(24px, env(safe-area-inset-top));
         padding-right: max(24px, env(safe-area-inset-right));
         padding-bottom: max(24px, env(safe-area-inset-bottom));
         padding-left: max(24px, env(safe-area-inset-left));
         display:flex; flex-direction:column; align-items:center; min-height:100vh; box-sizing:border-box; }
  /* iOS Human Interface Guidelines の本文基準（約17pt）、Android Material Design の
     body1（16sp）を下限の目安として、各要素のフォントサイズを引き上げている。 */
  h1 { font-size:22px; font-weight:600; margin-bottom:6px; text-align:center; }
  p.sub { color:#8b949e; font-size:16px; line-height:1.5; margin-top:0; margin-bottom:32px;
          text-align:center; max-width:280px; margin-left:auto; margin-right:auto; }
  /* 「現在のステータス」：常に事実（状態）のみを示す固定位置の表示。ボタンの外に置くことで
     「状態の提示」と「未来のアクションの提示」を位置的に分離する（位置とテキストの相補関係）。 */
  .status { font-size:17px; margin-bottom:24px; padding:10px 18px; border-radius:8px; background:#161b22; text-align:center; }
  .status.on { color:#3fb950; }
  .status.off { color:#8b949e; }
  .status .label { color:#6e7681; font-weight:500; }
  button#toggle { display:block; width:220px; height:220px; border-radius:50%; border:none; font-size:18px;
                  line-height:1.3; padding:0 24px;
                  font-weight:700; cursor:pointer; transition: background .2s; margin:0 auto; }
  button#toggle.off { background:#21262d; color:#e6edf3; }
  button#toggle.on { background:#238636; color:#ffffff; }
  button#toggle:disabled { opacity:0.5; }
  button#toggle:focus-visible { outline:3px solid #58a6ff; outline-offset:3px; }
  .updated { margin-top:24px; font-size:13px; color:#6e7681; text-align:center; }
</style>
</head>
<body>
  <main>
    <h1>Tesla充電切替</h1>
    <p class="sub">ONにすると太陽光の発電状況に関わらず、フル充電モードで動作します。</p>
    <!-- role="status" + aria-live="polite": 状態が変わったことを支援技術にも読み上げさせる -->
    <div class="status off" id="status" role="status" aria-live="polite" aria-atomic="true">
      <span class="label">現在のステータス：</span><span id="status-value">読み込み中...</span>
    </div>
    <!-- ボタン内テキストは常に「これを押すと何が起きるか（未来のアクション）」のみを示し、
         現在の状態は上の.statusだけが伝える。aria-pressedで状態自体も支援技術に伝える。 -->
    <button id="toggle" class="off" type="button" disabled aria-pressed="false" aria-describedby="status">...</button>
    <div class="updated" id="updated" aria-hidden="true"></div>
  </main>

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
  const statusValueEl = document.getElementById("status-value");
  const btn = document.getElementById("toggle");
  const updatedEl = document.getElementById("updated");
  btn.disabled = false;
  if (state.manual_override) {
    // .status側：現在の事実のみを示す（状態の提示）
    statusValueEl.textContent = "フル充電モード（太陽光発電状況を無視）";
    statusEl.className = "status on";
    // button側：押すと起きる未来のアクションのみを示す（アクションの提示）。状態の文言とは混在させない。
    btn.textContent = "太陽光追従モードに戻す";
    btn.className = "on";
    btn.setAttribute("aria-pressed", "true");
  } else {
    statusValueEl.textContent = "太陽光追従モード（通常稼働中）";
    statusEl.className = "status off";
    btn.textContent = "フル充電モードを開始する";
    btn.className = "off";
    btn.setAttribute("aria-pressed", "false");
  }
  updatedEl.textContent = "最終更新: " + new Date().toLocaleTimeString("ja-JP");
}

async function refresh() {
  try {
    const state = await fetchStatus();
    render(state);
  } catch (e) {
    document.getElementById("status-value").textContent = "通信エラー";
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

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}
</script>
</body>
</html>
"""

MANIFEST_TEMPLATE: str = """{
  "name": "Tesla充電切替",
  "short_name": "Tesla充電切替",
  "description": "太陽光発電の状況に関わらずフル充電モードを切替えるコントローラー",
  "start_url": "/?token=__TOKEN__",
  "scope": "/",
  "display": "standalone",
  "background_color": "#0b0f14",
  "theme_color": "#0b0f14",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable" }
  ]
}
"""

SERVICE_WORKER_SCRIPT: str = """const CACHE_NAME = "tesla-control-v1";

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", (event) => {
  // 充電状態は常に最新を取得する必要があるため、オフライン時のフォールバック以外はキャッシュしない
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request))
  );
});
"""


def render_page(token: str) -> str:
    return PAGE_TEMPLATE.replace("__TOKEN__", html.escape(token, quote=True))


def render_manifest(token: str) -> str:
    escaped_token = json.dumps(token)[1:-1]
    return MANIFEST_TEMPLATE.replace("__TOKEN__", escaped_token)


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

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
                self._send_bytes(403, "Forbidden: invalid or missing token".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send_bytes(200, render_page(CONTROL_TOKEN).encode("utf-8"), "text/html; charset=utf-8")
            return

        if parsed.path == "/manifest.webmanifest":
            if not self._check_token(query):
                self._send_json(403, {"error": "invalid token"})
                return
            self._send_bytes(200, render_manifest(CONTROL_TOKEN).encode("utf-8"), "application/manifest+json; charset=utf-8")
            return

        if parsed.path == "/sw.js":
            # PWAインストール判定に必要なService Workerはトークン不要の公開アセットとして配信する
            self._send_bytes(200, SERVICE_WORKER_SCRIPT.encode("utf-8"), "application/javascript; charset=utf-8")
            return

        if parsed.path in ("/icons/icon-192.png", "/icons/icon-512.png"):
            # アイコン画像自体は機密情報を含まないため、トークン無しで配信する
            icon_path = os.path.join(ICONS_DIR, os.path.basename(parsed.path))
            try:
                with open(icon_path, "rb") as f:
                    icon_bytes = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self._send_bytes(200, icon_bytes, "image/png")
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
