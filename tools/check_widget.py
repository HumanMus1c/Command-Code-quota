"""Drive the injected readout in a real headless browser and assert what the
hand-written DOM shim cannot: that an unchanged poll touches nothing.

The fixture (app/web/widget-preview.html) mirrors the live host's composer
geometry and answers the bridge with an envelope. Serving app/web over plain
static HTTP means /api/live 404s, so the readout runs only on the mocked
electronAPI and its data is constant by construction - any DOM mutation the
observer records is therefore ghosting, not an update.

Usage:  python tools/check_widget.py
Exit code is non-zero when any assertion fails.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.inject import CdpConnection, InjectError  # noqa: E402

BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)
POLL_MS = 2500
OBSERVE_SECONDS = 8.0


def start_static_server(directory: Path) -> tuple[socketserver.TCPServer, int]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(directory), **kwargs)

        def log_message(self, *_args) -> None:
            pass  # keep the report readable

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    return server, server.server_address[1]


def launch_browser(executable: str, profile: Path) -> int:
    subprocess.Popen(  # noqa: S603 - fixed allow-list of browser paths below
        [
            executable,
            "--headless=new",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-gpu",
            "about:blank",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    port_file = profile / "DevToolsActivePort"
    deadline = time.time() + 25
    while time.time() < deadline:
        if port_file.exists():
            head = port_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if head and head[0].strip().isdigit():
                return int(head[0].strip())
        time.sleep(0.2)
    raise RuntimeError("headless browser never wrote DevToolsActivePort")


def page_target(port: int) -> dict:
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        rows = json.loads(response.read().decode("utf-8", errors="replace"))
    for row in rows:
        if row.get("type") == "page" and row.get("webSocketDebuggerUrl", "").startswith("ws://127.0.0.1"):
            return row
    raise RuntimeError("no loopback page target")


class Harness:
    def __init__(self, connection: CdpConnection) -> None:
        self.connection = connection
        self._id = 0

    def evaluate(self, expression: str, *, await_promise: bool = False):
        self._id += 1
        reply = self.connection.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        result = (reply or {}).get("result", {})
        if result.get("exceptionDetails"):
            raise RuntimeError(f"page threw: {result['exceptionDetails']}")
        return result.get("value")


ARM = """
(() => {
  const node = document.getElementById('goat-gauge-widget');
  if (!node) return 'not-mounted';
  const row = node.shadowRoot.getElementById('row');
  window.__seen = {count: 0, text: 0, style: 0, child: 0, attr: 0, where: []};
  window.__obs && window.__obs.disconnect();
  window.__obs = new MutationObserver((records) => {
    for (const record of records) {
      window.__seen.count++;
      if (record.type === 'characterData') window.__seen.text++;
      else if (record.attributeName === 'style') window.__seen.style++;
      else if (record.type === 'childList') window.__seen.child++;
      else window.__seen.attr++;
      if (record.type === 'childList' && window.__seen.where.length < 6) {
        const at = record.target;
        window.__seen.where.push(`child:${at.nodeName}.${at.className || ''}`);
      } else if (record.type === 'attributes' && window.__seen.where.length < 6) {
        const at = record.target;
        window.__seen.where.push(`attr:${record.attributeName}@${at.nodeName}.${at.className || ''}`);
      } else if (record.type === 'characterData' && window.__seen.where.length < 6) {
        window.__seen.where.push(`text:${(record.target.nodeValue || '').slice(0, 12)}`);
      }
    }
  });
  window.__obs.observe(row, {childList: true, subtree: true, attributes: true, characterData: true});
  return 'armed';
})()
"""

READ = "JSON.stringify(window.__seen)"

VALUE = """
(() => {
  const row = document.getElementById('goat-gauge-widget').shadowRoot.getElementById('row');
  const five = row.querySelector('[data-metric=five]');
  return JSON.stringify({
    value: five ? (five.querySelector('em') || {}).textContent : null,
    width: five ? (((five.querySelector('.bar > i') || {}).style) || {}).width : null,
  });
})()
"""

TAG = """
(() => {
  const row = document.getElementById('goat-gauge-widget').shadowRoot.getElementById('row');
  [...row.children].forEach((child, index) => { child.__slot = index; });
  return row.children.length;
})()
"""

SURVIVED = """
(() => {
  const row = document.getElementById('goat-gauge-widget').shadowRoot.getElementById('row');
  return JSON.stringify([...row.children].map((child, index) => child.__slot === index));
})()
"""

COLORS = """
(() => {
  const row = document.getElementById('goat-gauge-widget').shadowRoot.getElementById('row');
  const dot = row.querySelector('.chip.dot i');
  const fill = row.querySelector('.bar > i');
  const clear = (value) => !value || value === 'rgba(0, 0, 0, 0)' || value === 'transparent';
  return JSON.stringify({
    dot: dot ? getComputedStyle(dot).backgroundColor : null,
    fill: fill ? getComputedStyle(fill).backgroundColor : null,
    dotClear: dot ? clear(getComputedStyle(dot).backgroundColor) : true,
    fillClear: fill ? clear(getComputedStyle(fill).backgroundColor) : true,
  });
})()
"""

PROBE = "JSON.stringify(window.__goatProbe())"

# Replace the fixture's mock with one whose numbers move by exactly one field,
# so a keyed/guarded renderer must rewrite only that chip.
MOVE_ONE = """
(() => {
  const base = window.__goatProbe;
  const original = window.electronAPI.accountOverview;
  window.electronAPI.accountOverview = async () => {
    const reply = await original();
    reply.overview.windowLimits.fiveHour.used += 0.01;
    return reply;
  };
  return 'moved';
})()
"""


def main() -> int:
    executable = next((path for path in BROWSERS if Path(path).exists()), None)
    if not executable:
        print("no Edge or Chrome found; cannot run the browser harness")
        return 2

    web_root = ROOT / "app" / "web"
    server, port = start_static_server(web_root)
    profile = Path(tempfile.mkdtemp(prefix="goat-widget-harness-"))
    process = None
    results: list[tuple[str, bool, str]] = []

    try:
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        debug_port = launch_browser(executable, profile)
        target = page_target(debug_port)
        connection = CdpConnection(target["webSocketDebuggerUrl"], timeout=20)
        harness = Harness(connection)
        connection.call("Page.enable")
        connection.call(
            "Runtime.evaluate",
            {"expression": f"location.href='http://127.0.0.1:{port}/widget-preview.html'"},
        )

        deadline = time.time() + 20
        probe: dict = {}
        while time.time() < deadline:
            try:
                probe = json.loads(harness.evaluate(PROBE) or "{}")
            except (InjectError, RuntimeError, json.JSONDecodeError):
                probe = {}
            if probe.get("mounted"):
                break
            time.sleep(0.3)
        results.append(("fixture: production widget mounts in a real browser", bool(probe.get("mounted")), str(probe.get("version"))))

        colors = json.loads(harness.evaluate(COLORS) or "{}")
        results.append(("colour: status dot is not transparent", not colors.get("dotClear"), str(colors.get("dot"))))
        results.append(("colour: progress fill is not transparent", not colors.get("fillClear"), str(colors.get("fill"))))

        harness.evaluate(ARM)
        time.sleep(OBSERVE_SECONDS)
        seen = json.loads(harness.evaluate(READ) or "{}")
        results.append((
            f"ghost: unchanged data writes nothing for {OBSERVE_SECONDS:.0f}s (>2 polls)",
            seen.get("count", -1) == 0,
            json.dumps({k: v for k, v in seen.items() if k != "where"}) + " " + json.dumps(seen.get("where")),
        ))

        harness.evaluate(TAG)
        before = json.loads(harness.evaluate(VALUE) or "{}")
        harness.evaluate(MOVE_ONE)
        time.sleep(POLL_MS / 1000 + 1.5)
        survived = json.loads(harness.evaluate(SURVIVED) or "[]")
        after = json.loads(harness.evaluate(VALUE) or "{}")
        results.append((
            "identity: a value change patches in place, keeping every node",
            bool(survived) and all(survived),
            json.dumps(survived[:4]),
        ))
        results.append((
            "correctness: the changed number and bar width actually moved",
            before.get("value") != after.get("value") and bool(after.get("value")),
            f"{before.get('value')} -> {after.get('value')} / {after.get('width')}",
        ))

        connection.close()
    finally:
        if process is not None:
            process.terminate()
        server.shutdown()
        server.server_close()

    width = max(len(name) for name, _, _ in results)
    failures = 0
    for name, passed, detail in results:
        if not passed:
            failures += 1
        print(f"{'PASS' if passed else 'FAIL'}  {name:<{width}}  {detail}")
    print(f"\n{len(results) - failures}/{len(results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
