import os
import sys
import json
import html
import hmac
import logging
import threading
from logging.handlers import RotatingFileHandler
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from typing import Any, Dict

from config_loader import Settings
from override_state import (
    parse_charge_target,
    read_away_probe,
    read_charge_target,
    read_override,
    write_away_probe,
    write_charge_target,
    write_override,
)
from vehicle_status import load_vehicle_status

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

with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
    config: Dict[str, Any] = json.load(f)

# 数値の設定は充電制御側と同じ入口を通す。片方だけ素の config.get に戻ると、
# 「設定を読む場所は1か所」という前提がまた崩れる。
settings = Settings(config, logger.warning)

# 0 は「OSに空きポートを割り当てさせる」を意味してしまい、
# スマホのホーム画面から開けなくなる。下限を1にする。
CONTROL_PORT: int = settings.integer("CONTROL_PORT", 8090, minimum=1)
control_token_value: Any = config.get("CONTROL_TOKEN")

if (not isinstance(control_token_value, str) or len(control_token_value) < 32
        or control_token_value == "YOUR_RANDOM_CONTROL_TOKEN_HERE"):
    logger.critical("CONTROL_TOKEN が文字列でない、32文字未満、またはテンプレート値です。起動を中止します。")
    sys.exit(1)
CONTROL_TOKEN: str = control_token_value

MAX_POST_BYTES: int = 4096
CLIENT_TIMEOUT_SEC: int = 5
MAX_CLIENTS: int = 16

ICONS_DIR: str = os.path.join(BASE_DIR, "icons")

# トークン無しで配信するアイコンの一覧。パスをそのままファイル名にせず、ここに載せたものだけを返す。
# purpose "any" と "maskable" を1つの画像で兼ねると、any として表示するときは切り抜き用の余白が
# 余分になり、maskable として切り抜くときは余白が足りず図柄の端が欠ける。別ファイルにして
# manifest で使い分ける（tools/gen_icon.py）。
ICON_FILES: frozenset = frozenset({
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-192.png",
    "icon-maskable-512.png",
    "apple-touch-icon-180.png",
})

PAGE_TEMPLATE: str = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>Tesla充電切替</title>
<link rel="manifest" href="/manifest.webmanifest?token=__TOKEN__">
<link rel="icon" href="/icons/icon-192.png">
<link rel="apple-touch-icon" sizes="180x180" href="/icons/apple-touch-icon-180.png">
<meta name="theme-color" content="#F2F3F5" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E1113" media="(prefers-color-scheme: dark)">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Tesla充電切替">
<style>
  /* 画面の規則は docs/05_charge_target_design.md 第8章。
     タップ領域は Apple HIG（44pt）と Material Design 3（48dp）の厳しい方に合わせて 48px 以上。
     外観は端末の設定（prefers-color-scheme）に従い、ライト／ダークを切替える。 */
  :root {
    --bg:#F2F3F5; --surf:#FFFFFF; --surf2:#E6E9ED; --text:#14171A; --text2:#4F5863; --text3:#5E6771;
    --primary:#0B5CAD; --on-primary:#FFFFFF; --tonal-bg:#DCE8F7; --tonal-text:#0B3E73;
    --fill:#E3A008; --fill-off:#8C959F; --chip-on-bg:#FFF0CC; --chip-on-text:#6B4700;
    --chip-off-bg:#E6E9ED; --chip-off-text:#3F4750; --note-bg:#EEF1F4; --note-text:#3F4750;
    --banner-bg:#E3EEFB; --banner-text:#0B3E73; --seg-on:#FFFFFF; --seg-on-text:#14171A;
    --switch-off:#AEB6BF; --disabled-bg:#E6E9ED; --disabled-text:#5E6771; --ring:#FFFFFF;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0E1113; --surf:#1A1E22; --surf2:#2A3036; --text:#E9EDF0; --text2:#A7B1B9; --text3:#98A2AB;
      --primary:#8CC8FF; --on-primary:#002F57; --tonal-bg:#1E3247; --tonal-text:#CFE5FF;
      --fill:#F2B33D; --fill-off:#7C868F; --chip-on-bg:#3A2C0E; --chip-on-text:#F2B33D;
      --chip-off-bg:#2A3036; --chip-off-text:#A7B1B9; --note-bg:#22272C; --note-text:#A7B1B9;
      --banner-bg:#13263A; --banner-text:#CFE5FF; --seg-on:#454D55; --seg-on-text:#FFFFFF;
      --switch-off:#555E67; --disabled-bg:#2A3036; --disabled-text:#98A2AB; --ring:#1A1E22;
    }
  }
  /* iOS では -apple-system-body を起点にすると、端末の文字サイズ設定（Dynamic Type）に追従する。
     以降のサイズは rem で指定し、この基準に比例させる。 */
  html { font: -apple-system-body; }
  * { -webkit-tap-highlight-color: transparent; box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family: system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif;
         font-size:1.0625rem; line-height:1.35;
         padding-top: max(16px, env(safe-area-inset-top));
         padding-right: max(16px, env(safe-area-inset-right));
         padding-bottom: max(32px, env(safe-area-inset-bottom));
         padding-left: max(16px, env(safe-area-inset-left)); }
  main { max-width:480px; margin:0 auto; display:flex; flex-direction:column; gap:16px; }
  header { padding:8px 4px 4px; }
  h1 { margin:0; font-size:2.125rem; line-height:1.2; font-weight:700; }
  .sub { font-size:0.9375rem; color:var(--text2); }
  section { background:var(--surf); border-radius:28px; padding:20px; display:flex; flex-direction:column; gap:16px; }
  h2 { margin:0; font-size:1.25rem; font-weight:600; }
  .row { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }
  .row.base { align-items:baseline; }
  .label { margin:0; font-size:0.9375rem; font-weight:600; color:var(--text2); }
  .big { font-size:4.5rem; line-height:1; font-weight:700; letter-spacing:-0.02em; font-variant-numeric:tabular-nums; }
  .big small { font-size:2rem; font-weight:600; color:var(--text2); margin-left:2px; }
  .chip { padding:8px 14px; border-radius:16px; font-size:0.9375rem; font-weight:600; white-space:nowrap;
          background:var(--chip-off-bg); color:var(--chip-off-text); }
  .chip.on { background:var(--chip-on-bg); color:var(--chip-on-text); }
  .bar { position:relative; height:16px; border-radius:8px; background:var(--surf2); }
  .bar .fill { position:absolute; left:0; top:0; bottom:0; border-radius:8px; background:var(--fill-off); }
  .bar .fill.on { background:var(--fill); }
  .bar .limit { position:absolute; top:-4px; bottom:-4px; width:0; border-left:2px dashed var(--text2); }
  .bar .target { position:absolute; top:-6px; bottom:-6px; width:4px; border-radius:2px; background:var(--primary);
                 box-shadow:0 0 0 2px var(--ring); }
  .legend { display:flex; flex-wrap:wrap; gap:6px 16px; font-size:0.8125rem; color:var(--text2); }
  .legend span { display:flex; align-items:center; gap:6px; }
  .sw-target { width:4px; height:14px; border-radius:2px; background:var(--primary); }
  .sw-limit { width:0; height:14px; border-left:2px dashed var(--text2); }
  .note { margin:0; padding:12px 16px; border-radius:16px; font-size:0.9375rem; line-height:1.45;
          background:var(--note-bg); color:var(--note-text); }
  .banner { background:var(--banner-bg); color:var(--banner-text); }
  .fine { margin:0; font-size:0.8125rem; line-height:1.4; color:var(--text3); }
  .stepper { display:grid; grid-template-columns:56px minmax(0,1fr) 56px; align-items:center; gap:8px; }
  .icon-btn { width:56px; height:56px; border-radius:28px; border:none; background:var(--tonal-bg); color:var(--tonal-text);
              display:flex; align-items:center; justify-content:center; cursor:pointer; }
  output { text-align:center; font-size:4rem; line-height:1; font-weight:700; letter-spacing:-0.02em;
           font-variant-numeric:tabular-nums; color:var(--primary); }
  output small { font-size:1.75rem; font-weight:600; }
  input[type=range] { width:100%; height:48px; margin:0; accent-color:var(--primary); }
  .ticks { display:flex; justify-content:space-between; font-size:0.8125rem; color:var(--text3); font-variant-numeric:tabular-nums; }
  .btn { min-height:56px; border-radius:28px; border:none; font:inherit; font-size:1.0625rem; font-weight:600; cursor:pointer;
         background:var(--primary); color:var(--on-primary); }
  .btn:disabled { background:var(--disabled-bg); color:var(--disabled-text); cursor:default; }
  .btn.text { min-height:48px; background:transparent; color:var(--primary); }
  .seg { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:4px; padding:4px; border-radius:28px; background:var(--surf2); }
  .seg button { min-height:48px; border-radius:24px; border:none; font:inherit; font-size:1.0625rem; font-weight:500;
                background:transparent; color:var(--text2); cursor:pointer; }
  .seg button[aria-pressed=true] { background:var(--seg-on); color:var(--seg-on-text); font-weight:600; box-shadow:0 1px 3px rgba(0,0,0,.12); }
  .switch { flex-shrink:0; position:relative; width:52px; height:32px; border-radius:16px; border:none; padding:0;
            background:var(--switch-off); cursor:pointer; }
  .switch[aria-checked=true] { background:var(--primary); }
  .switch span { position:absolute; top:3px; left:3px; width:26px; height:26px; border-radius:13px; background:#FFFFFF;
                 box-shadow:0 1px 2px rgba(0,0,0,.25); transition:left .15s; }
  .switch[aria-checked=true] span { left:23px; }
  button:focus-visible, input:focus-visible { outline:3px solid var(--primary); outline-offset:3px; }
  @media (prefers-reduced-motion: reduce) { .switch span { transition:none; } }
  [hidden] { display:none !important; }
</style>
</head>
<body>
<main>
  <header>
    <h1>Tesla充電</h1>
    <div class="sub" id="fetched">読み込み中...</div>
  </header>

  <section aria-labelledby="now-label">
    <div class="row">
      <div>
        <h2 class="label" id="now-label">現在の充電率</h2>
        <div class="big" id="level">--<small>%</small></div>
      </div>
      <div class="chip" id="chip" role="status" aria-live="polite">--</div>
    </div>
    <div>
      <div class="bar" aria-hidden="true">
        <div class="fill" id="bar-fill" style="width:0%"></div>
        <div class="limit" id="bar-limit" hidden></div>
        <div class="target" id="bar-target" hidden></div>
      </div>
      <div class="legend" style="margin-top:10px">
        <span id="legend-target" hidden><span class="sw-target"></span><span id="legend-target-text"></span></span>
        <span id="legend-limit" hidden><span class="sw-limit"></span><span id="legend-limit-text"></span></span>
      </div>
    </div>
    <p class="note banner" id="banner" hidden></p>
  </section>

  <section aria-labelledby="target-label">
    <div class="row base">
      <h2 id="target-label">目標充電率</h2>
      <span class="sub" id="saved-label">--</span>
    </div>
    <div class="stepper">
      <button type="button" class="icon-btn" id="dec" aria-label="1%下げる">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"></line></svg>
      </button>
      <output for="target-range" id="pending">--<small>%</small></output>
      <button type="button" class="icon-btn" id="inc" aria-label="1%上げる">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"></line><line x1="12" y1="5" x2="12" y2="19"></line></svg>
      </button>
    </div>
    <div>
      <label for="target-range" class="fine">スライダーで大まかに、±ボタンで1%ずつ合わせる</label>
      <input id="target-range" type="range" min="1" max="100" step="1" value="30">
      <div class="ticks"><span>1%</span><span>50%</span><span>100%</span></div>
    </div>
    <p class="note" id="limit-note"></p>
    <button type="button" class="btn" id="set-target" disabled>設定済み</button>
    <button type="button" class="btn text" id="clear-target" hidden>目標充電率を解除する</button>
    <p class="fine">目標以上になったら止め、目標より下では止めません。最大電流（48A）のときは1%程度超えて止まることがあります。</p>
  </section>

  <section aria-labelledby="mode-label">
    <h2 id="mode-label">充電モード</h2>
    <div class="seg" role="group" aria-labelledby="mode-label">
      <button type="button" id="mode-solar" aria-pressed="false">太陽光追従</button>
      <button type="button" id="mode-full" aria-pressed="false">フル充電</button>
    </div>
    <p class="note" id="mode-note" style="background:transparent;padding:0;color:var(--text2)"></p>
  </section>

  <section>
    <div class="row" style="align-items:center;min-height:48px">
      <span id="probe-label">外出先の充電記録</span>
      <button type="button" class="switch" id="probe" role="switch" aria-checked="false" aria-labelledby="probe-label"><span></span></button>
    </div>
    <p class="fine">自宅の充電器にケーブルが繋がっていない間も、10分ごとに車両データを読みます。外出1回（約4時間）でおよそ¥7かかります。</p>
  </section>
</main>

<script>
const TOKEN = "__TOKEN__";
const STATE_LABELS = { Charging: "充電中", Stopped: "停止中", Complete: "満充電", Disconnected: "ケーブル未接続",
                       NoPower: "給電なし", Starting: "開始処理中" };
let state = null;
let pending = null;   // 画面で選んでいる目標充電率（保存前）
let dirty = false;    // 保存前の変更があるか。あるうちは5秒ごとの再取得で上書きしない

function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  };
  return fetch(`${path}?token=${encodeURIComponent(TOKEN)}`, opts).then((res) => {
    if (!res.ok) throw new Error(path);
    return res.json();
  });
}

function hhmm(epoch) {
  return new Date(epoch * 1000).toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
}

function el(id) { return document.getElementById(id); }

function render() {
  if (!state) return;
  const v = state.vehicle || {};
  const target = state.charge_target_soc;
  const level = Number.isInteger(v.battery_level) ? v.battery_level : null;
  const limitMin = Number.isInteger(v.charge_limit_soc_min) ? v.charge_limit_soc_min : 50;
  const original = Number.isInteger(v.charge_limit_restore_soc) ? v.charge_limit_restore_soc
                 : (Number.isInteger(v.charge_limit_soc) ? v.charge_limit_soc : null);
  if (!dirty) pending = target === null ? 30 : target;

  el("fetched").textContent = v.observed_at ? `車両データ ${hhmm(v.observed_at)} 取得` : "車両データ未取得";
  el("level").firstChild.textContent = level === null ? "--" : String(level);
  const charging = v.charging_state === "Charging";
  el("chip").textContent = STATE_LABELS[v.charging_state] || v.charging_state || "--";
  el("chip").className = charging ? "chip on" : "chip";
  el("bar-fill").style.width = `${level === null ? 0 : level}%`;
  el("bar-fill").className = charging ? "fill on" : "fill";

  const showMarker = target !== null || dirty;
  el("bar-target").hidden = !showMarker;
  el("bar-target").style.left = `calc(${pending}% - 2px)`;
  el("legend-target").hidden = !showMarker;
  el("legend-target-text").textContent = `目標充電率 ${pending}%${dirty ? "（未保存）" : ""}`;
  const limit = Number.isInteger(v.charge_limit_soc) ? v.charge_limit_soc : null;
  el("bar-limit").hidden = limit === null;
  el("bar-limit").style.left = `calc(${limit}% - 1px)`;
  el("legend-limit").hidden = limit === null;
  el("legend-limit-text").textContent = `車両側の上限 ${limit}%${Number.isInteger(v.charge_limit_applied_soc) ? "（自動）" : ""}`;

  const reached = target !== null && level !== null && level >= target && v.target_reached_at;
  el("banner").hidden = !reached;
  if (reached) {
    el("banner").textContent = `目標充電率 ${target}% に達したため、${hhmm(v.target_reached_at)} に充電を停止しました。` +
      `以後は ${target}% を下回り、かつ余剰電力があるときだけ充電します。`;
  }

  el("saved-label").textContent = target === null ? "未設定" : `設定中：${target}%`;
  el("pending").firstChild.textContent = String(pending);
  el("target-range").value = String(pending);
  const nextLimit = Math.max(pending, limitMin);
  const stopper = pending < limitMin
    ? `車両側の下限が${limitMin}%のため、${pending}% ではシステムが充電を停止します。`
    : `車両自身が ${pending}% で充電を止めます。`;
  const back = original !== null
    ? `ケーブルを抜くと元の ${original}% に戻すため、外出先では ${original}% まで充電できます。` : "";
  el("limit-note").textContent =
    `自宅の充電器に繋がっている間、車両側の充電上限を ${nextLimit}% にします。${stopper}${back}`;
  const changed = pending !== target;
  el("set-target").disabled = !changed;
  el("set-target").textContent = changed ? `${pending}% に設定する` : "設定済み";
  el("clear-target").hidden = target === null;

  const full = state.manual_override;
  el("mode-solar").setAttribute("aria-pressed", String(!full));
  el("mode-full").setAttribute("aria-pressed", String(full));
  el("mode-note").textContent = !full
    ? "余剰電力の範囲で充電します。夜間（18:00〜7:00）は充電しません。"
    : (target !== null && level !== null && level >= target
      ? "現在の充電率が目標充電率以上のため、次の確認（最大3分後）でフル充電モードを解除し、太陽光追従に戻ります。"
      : "太陽光に関係なく最大48Aで充電します。夜間も充電します。" +
        (target !== null ? `目標充電率 ${target}% に達したら停止し、太陽光追従に戻ります。` : "車両側の上限まで充電します。"));
  el("probe").setAttribute("aria-checked", String(state.away_probe));
}

function update(promise) {
  return promise.then((s) => { state = s; render(); })
    .catch(() => { alert("切替に失敗しました。通信状態を確認してください。"); });
}

function setPending(value) {
  pending = Math.max(1, Math.min(100, value));
  dirty = !state || pending !== state.charge_target_soc;
  render();
}

el("dec").addEventListener("click", () => setPending(pending - 1));
el("inc").addEventListener("click", () => setPending(pending + 1));
el("target-range").addEventListener("input", (e) => setPending(parseInt(e.target.value, 10)));
el("set-target").addEventListener("click", () => {
  dirty = false;
  update(api("/api/charge_target", { soc: pending }));
});
el("clear-target").addEventListener("click", () => {
  dirty = false;
  update(api("/api/charge_target", { soc: null }));
});
el("mode-solar").addEventListener("click", () => update(api("/api/override", { enabled: false })));
el("mode-full").addEventListener("click", () => update(api("/api/override", { enabled: true })));
el("probe").addEventListener("click", () => update(api("/api/away_probe", { enabled: !state.away_probe })));

function refresh() {
  api("/api/status").then((s) => { state = s; render(); })
    .catch(() => { el("fetched").textContent = "通信エラー"; });
}
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
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any" },
    { "src": "/icons/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable" },
    { "src": "/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
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


# 画面に返す車両の状態。vehicle_status.json のうち、表示に使うキーだけを渡す。
VEHICLE_STATUS_KEYS = (
    "battery_level",
    "charging_state",
    "charge_limit_soc",
    "charge_limit_soc_min",
    "charge_limit_applied_soc",
    "charge_limit_restore_soc",
    "observed_at",
    "target_reached_at",
)


def status_payload() -> Dict[str, Any]:
    """/api/status と各POSTの応答。画面は1回の応答で全体を描き直す。"""
    vehicle = load_vehicle_status()
    return {
        "manual_override": read_override(),
        "away_probe": read_away_probe(),
        "charge_target_soc": read_charge_target()[0],
        "vehicle": {key: vehicle.get(key) for key in VEHICLE_STATUS_KEYS},
    }


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
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query: Dict[str, list] = parse_qs(parsed.query)

        if parsed.path == "/api/status":
            if not self._check_token(query):
                self._send_json(403, {"error": "invalid token"})
                return
            self._send_json(200, status_payload())
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

        if parsed.path.startswith("/icons/") and parsed.path[len("/icons/"):] in ICON_FILES:
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

        if parsed.path not in ("/api/override", "/api/away_probe", "/api/charge_target"):
            self.send_response(404)
            self.end_headers()
            return

        if not self._check_token(query):
            self._send_json(403, {"error": "invalid token"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length < 0:
            self._send_json(400, {"error": "invalid content length"})
            return
        if length > MAX_POST_BYTES:
            self._send_json(413, {"error": "request body too large"})
            return
        raw_body = self.rfile.read(length) if length else b""
        try:
            payload: Dict[str, Any] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return

        if parsed.path == "/api/charge_target":
            # 値の検査はここで1回だけ行い、通った値だけを保存する（parse_charge_target）。
            # 制御ループは保存された値を信用するが、手で書き換えられた場合に備えて読む側でも同じ関数を通す。
            if not isinstance(payload, dict) or "soc" not in payload:
                self._send_json(400, {"error": "soc is required"})
                return
            try:
                target = parse_charge_target(payload["soc"])
            except ValueError:
                self._send_json(400, {"error": "soc must be an integer from 1 to 100, or null"})
                return
            write_charge_target(target)
            logger.info(f"目標充電率を {'未設定' if target is None else f'{target}%'} に変更しました。")
            self._send_json(200, status_payload())
            return

        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            self._send_json(400, {"error": "enabled must be a boolean"})
            return
        enabled = payload["enabled"]

        if parsed.path == "/api/override":
            write_override(enabled)
            logger.info(f"マニュアル・オーバーライドを {'有効（フル充電）' if enabled else '無効（太陽光追従に復帰）'} に切替えました。")
        else:
            write_away_probe(enabled)
            logger.info(f"外出先の充電記録を {'有効（課金対象の問い合わせを再開）' if enabled else '無効'} に切替えました。")

        # 画面は1回の応答で全体を描き直す。一部だけ返すと、残りの表示が
        # 次のポーリング（5秒後）まで古いままになる。
        self._send_json(200, status_payload())

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format % args)


class ControlServer(ThreadingHTTPServer):
    """同時接続を16件に制限し、各接続の読み取りを5秒で打ち切る。"""

    request_queue_size = MAX_CLIENTS

    def __init__(self, server_address: tuple, handler_class: type) -> None:
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        super().__init__(server_address, handler_class)

    def get_request(self) -> tuple:
        request, client_address = super().get_request()
        request.settimeout(CLIENT_TIMEOUT_SEC)
        return request, client_address

    def process_request(self, request: Any, client_address: tuple) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: tuple) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main() -> None:
    server = ControlServer(("0.0.0.0", CONTROL_PORT), ControlHandler)
    logger.info("=========================================================================")
    logger.info(f"スマホ操作用コントロールサーバーをポート {CONTROL_PORT} で起動しました。")
    logger.info("=========================================================================")
    server.serve_forever()


if __name__ == "__main__":
    main()
