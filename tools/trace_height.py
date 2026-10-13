"""Trace the board's height over an idle window in a real browser.

check_widget.py counts DOM writes; this counts *geometry* changes, because a row
that alternates between one line and two is a height flicker even when every write
is guarded. The composer column is narrowed to a borderline width where the row's
content is within a few pixels of the box, which is the state the live host was
measured in (clientWidth 847, scrollWidth 847).

Usage:  python tools/trace_height.py [--seconds 12] [--edit]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from app.inject import CdpConnection  # noqa: E402
from check_widget import (  # noqa: E402
    BROWSERS,
    launch_browser,
    page_target,
    prune_stale_profiles,
    shutdown_browser,
    start_static_server,
)

SET_WIDTH = """(() => {
  const column = document.querySelector('.cc-composer-host');
  column.style.maxWidth = %s ? (%s + 'px') : '';
  return column.style.maxWidth || 'auto';
})()"""

SAMPLE = """(() => {
  const node = document.getElementById('goat-gauge-widget');
  if (!node) return JSON.stringify({err: 'no-node'});
  const row = node.shadowRoot.getElementById('row');
  const probe = window.__goatProbe();
  const box = row.getBoundingClientRect();
  const tools = row.querySelector('.tools');
  const column = document.querySelector('.cc-composer-host');
  const lines = new Set([...row.children].map((c) => Math.round(c.getBoundingClientRect().top)));
  return JSON.stringify({
    t: Math.round(performance.now()),
    layout: node.dataset.layout,
    edit: node.dataset.edit,
    level: probe.shedLevel,
    moves: probe.levelChanges,
    shapes: (probe.shapeChanges || []).length,
    width: Math.round(row.clientWidth),
    natural: Math.round(row.scrollWidth),
    height: Math.round(box.height),
    scrollH: row.scrollHeight,
    clientH: row.clientHeight,
    lines: lines.size,
    shed: row.querySelectorAll('.cd.shed, .bar.shed').length,
    board: [...row.querySelectorAll('.chip[data-metric]')].map((c) => c.dataset.metric).join('+'),
    editor: probe.editor || null,
    log: (probe.shapeChanges || []).slice(-1)[0] || null,
    tools: tools ? Math.round(tools.getBoundingClientRect().bottom) : null,
    columnBottom: column ? Math.round(column.getBoundingClientRect().bottom) : null,
  });
})()"""

ARM = """(() => {
  const row = document.getElementById('goat-gauge-widget').shadowRoot.getElementById('row');
  window.__seen = {count: 0, child: 0, attr: 0, style: 0, text: 0, where: []};
  window.__obs && window.__obs.disconnect();
  window.__obs = new MutationObserver((rs) => { rs.forEach((r) => { window.__seen.count++;
    if (r.type === 'characterData') window.__seen.text++;
    else if (r.attributeName === 'style') window.__seen.style++;
    else if (r.type === 'childList') window.__seen.child++;
    else window.__seen.attr++;
    if (window.__seen.where.length < 8) {
      const t = r.target;
      window.__seen.where.push(r.type === 'attributes'
        ? `attr:${r.attributeName}@${t.nodeName}.${t.className || ''}`
        : r.type === 'childList' ? `child:${t.nodeName}.${t.className || ''}` : 'text');
    }}); });
  window.__obs.observe(row, {childList: true, subtree: true, attributes: true, characterData: true});
  return 'armed';
})()"""


def _runs(samples, key="board"):
    """Collapse a sample series into "value (count)" runs, so a flap is visible
    as an alternating pattern instead of a set that hides it."""
    read = key if callable(key) else (lambda item: item.get(key))
    out = []
    for sample in samples:
        value = read(sample)
        if out and out[-1][0] == value:
            out[-1][1] += 1
        else:
            out.append([value, 1])
    return [(value, count) for value, count in out]


FLAP = """(() => {
  window.FLAP_ON = !window.FLAP_ON;
  const ack = window.__goatUpdate({
    version: window.__goatWidgetVersion,
    server_time: Date.now(),
    rates: window.FLAP_ON ? {tokens_per_sec: 40, requests_per_min: 1.5} : {},
    projections: window.FLAP_ON ? {five_hour: {burn_per_hour: 2.5, verdict: "after_reset"}} : {},
  });
  const row = document.getElementById('goat-gauge-widget').shadowRoot.getElementById('row');
  return JSON.stringify([ack, [...row.querySelectorAll('.chip[data-metric]')].map((c) => c.dataset.metric).join('+')]);
})()"""

FLAP_SEED = "'seeded'"


def evaluate(connection, expression):
    reply = connection.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    result = (reply or {}).get("result", {})
    if result.get("exceptionDetails"):
        raise RuntimeError(f"page threw: {result['exceptionDetails']}")
    return result.get("value")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=12.0)
    parser.add_argument("--step", type=float, default=0.25)
    parser.add_argument("--width", type=float, default=0, help="composer column width; 0 keeps the default")
    parser.add_argument("--window", type=str, default="", help="browser window size, e.g. 1000,900")
    parser.add_argument("--edit", action="store_true", help="trace while the layout editor is open")
    parser.add_argument("--flap", action="store_true", help="toggle a data-derived metric in and out")
    parser.add_argument("--web", type=str, default="", help="serve this directory instead of app/web")
    args = parser.parse_args()

    executable = next((path for path in BROWSERS if Path(path).exists()), None)
    if not executable:
        print("no Edge or Chrome found")
        return 2

    pruned = prune_stale_profiles()
    if pruned:
        print(f"pruned {pruned} stale harness profile(s)")
    server, port = start_static_server(Path(args.web) if args.web else ROOT / "app" / "web")
    profile = Path(tempfile.mkdtemp(prefix="goat-height-trace-"))
    process: subprocess.Popen | None = None
    connection: CdpConnection | None = None
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        debug_port, process = launch_browser(executable, profile, args.window)
        target = page_target(debug_port)
        connection = CdpConnection(target["webSocketDebuggerUrl"], timeout=20)
        connection.call("Page.enable")
        evaluate(connection, f"location.href='http://127.0.0.1:{port}/widget-preview.html'")

        deadline = time.time() + 20
        while time.time() < deadline:
            raw = evaluate(connection, "JSON.stringify(window.__goatProbe && window.__goatProbe())")
            probe = json.loads(raw or "{}")
            if probe.get("mounted") and probe.get("host"):
                break
            time.sleep(0.3)
        else:
            print("the board never reported a host answer")
            return 2

        if args.width:
            print("column width set to", evaluate(connection, SET_WIDTH % (json.dumps(True), json.dumps(args.width))))
        if args.edit:
            evaluate(connection, "window.__goatLayout('edit')")
        time.sleep(1.5)

        print("arming", evaluate(connection, ARM))
        if args.flap:
            evaluate(connection, FLAP_SEED)
        started = time.time()
        samples = []
        flapped = []
        step = 0
        while time.time() - started < args.seconds:
            if args.flap and step % 4 == 0:
                # Toggle a metric's *data* in and out: membership is what moved the
                # reported height, so the trace has to make a chip lapse.
                flapped.append(evaluate(connection, FLAP))
            samples.append(json.loads(evaluate(connection, SAMPLE) or "{}"))
            step += 1
            time.sleep(args.step)
        seen = json.loads(evaluate(connection, "JSON.stringify(window.__seen)") or "{}")
        shapes = json.loads(evaluate(connection, "JSON.stringify(window.__goatProbe().shapeChanges)") or "[]")
        board_log = json.loads(evaluate(connection, "JSON.stringify(window.__goatProbe().boardLog)") or "[]")
        board_changes = evaluate(connection, "window.__goatProbe().boardChanges")
    finally:
        shutdown_browser(connection, process, profile)
        server.shutdown()
        server.server_close()

    path = Path(tempfile.gettempdir()) / "goat-height-trace.json"
    path.write_text(json.dumps({"samples": samples, "seen": seen}, ensure_ascii=False, indent=1), encoding="utf-8")

    heights = [s["height"] for s in samples]
    layouts = [s["layout"] for s in samples]
    lines = [s["lines"] for s in samples]
    widths = [s["width"] for s in samples]
    naturals = [s["natural"] for s in samples]
    print(f"samples={len(samples)} over {args.seconds}s  (trace: {path})")
    print("height    :", sorted(set(heights)), "changes:", sum(1 for a, b in zip(heights, heights[1:]) if a != b))
    print("lines     :", sorted(set(lines)), "changes:", sum(1 for a, b in zip(lines, lines[1:]) if a != b))
    print("layout    :", " ".join(str(x) for x in layouts))
    print("width     :", sorted(set(widths)), " natural:", sorted(set(naturals)))
    print("level     :", sorted(set(s['level'] for s in samples)), " moves:", samples[-1]["moves"] - samples[0]["moves"])
    print("shed      :", sorted(set(s["shed"] for s in samples)))
    print("board     :", " | ".join(f"{b} ({c})" for b, c in _runs(samples)))
    print("tools/column:", sorted({(s.get("tools"), s.get("columnBottom")) for s in samples}))
    editors = [s.get("editor") for s in samples if s.get("editor")]
    if editors:
        print("editor      :", " | ".join(f"{b} ({c})" for b, c in _runs(editors, key=json.dumps)))
    print("shape log :", json.dumps(shapes, ensure_ascii=False))
    print("mutations :", json.dumps({k: v for k, v in seen.items() if k != "where"}), json.dumps(seen.get("where")))
    if len(set(heights)) > 1:
        print("\nheight sequence:")
        print(" ".join(f"{h}" for h in heights))
    print("board moves :", board_changes, " logged:", len(board_log))
    if flapped:
        print("pushes      :", " | ".join(str(x) for x in flapped[:4]))
    for entry in board_log[-12:]:
        print("   ", json.dumps(entry, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
