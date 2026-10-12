"""Inject the GOAT Gauge readout into the Command Code desktop app.

Command Code ships a page CSP of ``script-src 'self' …``, so injected ``<script>``
elements are refused. Chrome DevTools Protocol evaluation is not subject to that
policy, which is why the widget arrives through ``Runtime.evaluate`` and derives
its data from the app's own ``window.electronAPI`` bridge plus what this module
pushes in from the local service.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

WIDGET_VERSION = "13"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
MAX_PORT_FILE_BYTES = 1024
MAX_TARGETS_BYTES = 1_000_000
DISCOVERY_TIMEOUT = 2.0
CALL_TIMEOUT = 5.0
ATTACH_INTERVAL = 3.0
SOURCE_PATH = Path(__file__).resolve().parent / "web" / "widget.js"

# Asked of the live renderer rather than parsed out of the host's leveldb, which
# is snappy-compressed and would break on any storage change. The host toggles
# the "dark" class for every non-light theme, so that plus data-theme covers it.
THEME_EXPRESSION = (
    "(function(){const r=document.documentElement;if(!r)return '';const t=(r.getAttribute('data-theme')"
    "||'').toLowerCase();if(t.indexOf('dark')>=0)return 'dark';"
    "return r.classList&&r.classList.contains('dark')?'dark':'light';})()"
)


class InjectError(RuntimeError):
    pass


def commandcode_profile_dir() -> Path:
    configured = os.environ.get("GOATGAUGE_CC_PROFILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt":
        root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(root) / "Command Code"
    return Path.home() / ".config" / "Command Code"


@dataclass
class CdpTarget:
    page_id: str
    ws_url: str
    url: str
    origin: str
    title: str


def origin_of(url: str) -> str:
    """Rebuild the origin a page would report, or '' for an opaque one.

    Chrome reports ``location.origin === "null"`` for file:// documents, so a
    file page can only be matched by protocol.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme in ("http", "https"):
        host = parsed.hostname or ""
        if not host:
            return ""
        suffix = f":{parsed.port}" if parsed.port not in (None, 80, 443) else ""
        return f"{parsed.scheme}://{host}{suffix}"
    return "" 


def _loopback_opener() -> urllib.request.OpenerDirector:
    """Never route the loopback DevTools endpoint through a proxy."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def devtools_port(profile: Path) -> int | None:
    path = profile / "DevToolsActivePort"
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:MAX_PORT_FILE_BYTES]
    except OSError:
        return None
    line = head.splitlines()[0].strip() if head.strip() else ""
    if not line.isdigit():
        return None
    port = int(line)
    return port if 1 <= port <= 65535 else None


def discover_targets(port: int) -> list[CdpTarget]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/json/list")
    try:
        with _loopback_opener().open(request, timeout=DISCOVERY_TIMEOUT) as response:
            raw = response.read(MAX_TARGETS_BYTES + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise InjectError(f"DevTools 发现失败: {exc}") from exc
    if len(raw) > MAX_TARGETS_BYTES:
        raise InjectError("DevTools 目标列表过大，已放弃")
    try:
        rows = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise InjectError("DevTools 返回了非 JSON 内容") from exc
    targets: list[CdpTarget] = []
    for row in rows if isinstance(rows, list) else []:
        if row.get("type") != "page":
            continue
        ws_url = str(row.get("webSocketDebuggerUrl") or "")
        parsed = urllib.parse.urlparse(ws_url)
        # A stale or hostile listing must never point us off-loopback.
        if parsed.scheme != "ws" or parsed.hostname not in LOOPBACK_HOSTS:
            continue
        if parsed.port != port:
            continue
        url = str(row.get("url") or "")
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme not in ("http", "https", "file"):
            continue
        targets.append(
            CdpTarget(
                page_id=str(row.get("id") or ""),
                ws_url=ws_url,
                url=url,
                origin=origin_of(url),
                title=str(row.get("title") or ""),
            )
        )
    return targets


class CdpConnection:
    """Synchronous JSON-RPC over the page WebSocket, matching ids to replies."""

    def __init__(self, ws_url: str, timeout: float = CALL_TIMEOUT) -> None:
        try:
            import websocket  # type: ignore
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise InjectError("缺少 websocket-client 依赖") from exc
        # Chromium rejects a DevTools handshake that carries a disallowed Origin,
        # and websocket-client adds one by default. Sending no Origin at all keeps
        # the port closed to web pages without widening it with
        # --remote-allow-origins, which any local process could then abuse.
        self._socket = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
        self._next_id = 1
        self._timeout = timeout

    def close(self) -> None:
        try:
            self._socket.close()
        except Exception:  # noqa: BLE001
            pass

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        message_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._socket.send(json.dumps(payload))
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            try:
                reply = json.loads(self._socket.recv())
            except OSError:
                raise InjectError(f"{method}: DevTools 连接中断")
            if not isinstance(reply, dict) or reply.get("id") != message_id:
                continue  # events and other replies are the caller's business
            if reply.get("error"):
                raise InjectError(f"{method}: {reply['error'].get('message', reply['error'])}")
            return reply.get("result")
        raise InjectError(f"{method}: DevTools 响应超时")


def widget_source() -> str:
    try:
        source = SOURCE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise InjectError(f"读数条源码不可读: {exc}") from exc
    return source


def page_guard(target: CdpTarget) -> str:
    if target.origin:
        safe = target.origin.replace("\\", "\\\\").replace('"', '\\"')
        return f'location.origin!=="{safe}"'
    scheme = urllib.parse.urlsplit(target.url).scheme or "file"
    return f'location.protocol!=="{scheme}:"'


def guarded_source(source: str, target: CdpTarget) -> str:
    """Only run in the page we registered for, and never twice in one page."""
    return (
        "(function(){if(" + page_guard(target) + "){return;}"
        + "if(window.__goatWidgetVersion=="
        + json.dumps(WIDGET_VERSION)
        + "){return;}"
        # A page still holding an older build has its own heartbeat and observer
        # running; leaving it up would render two boards into one slot.
        + "if(window.__goatWidgetVersion&&window.__goatDispose){try{window.__goatDispose();}"
        + "catch(error){}}"
        + source
        + "})()"
    )


def update_expression(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f"window.__goatUpdate && window.__goatUpdate({body})"


class WidgetInjector:
    def __init__(
        self,
        *,
        profile: Path | None = None,
        payload_builder: Callable[[], dict[str, Any]],
        log: Callable[[str], None] = lambda message: None,
    ) -> None:
        self.profile = profile or commandcode_profile_dir()
        self.payload_builder = payload_builder
        self.log = log
        self._sessions: dict[str, dict[str, Any]] = {}
        self._stop = threading.Event()
        # attach() holds this lock while it probes whether a page is still alive,
        # and that probe comes back through evaluate() on the same lock, so it has
        # to be reentrant or the first reconcile of a live page self-deadlocks.
        self._lock = threading.RLock()
        self.last_error = ""
        self.host_theme = ""

    def _open(self, target: CdpTarget) -> dict[str, Any]:
        connection = CdpConnection(target.ws_url)
        source = widget_source()
        script = guarded_source(source, target)
        identifier = ""
        try:
            connection.call("Runtime.enable")
            connection.call("Page.enable")
            registered = connection.call(
                "Page.addScriptToEvaluateOnNewDocument", {"source": script}
            )
            identifier = str((registered or {}).get("identifier") or "")
            connection.call("Runtime.evaluate", {"expression": script, "awaitPromise": False})
        except InjectError:
            # A registered preload outlives the socket, so it must be undone here.
            if identifier:
                try:
                    connection.call(
                        "Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier}
                    )
                except InjectError:
                    pass
            connection.close()
            raise
        return {"connection": connection, "identifier": identifier, "target": target}

    def attach(self) -> None:
        port = devtools_port(self.profile)
        if not port:
            self.last_error = "no-debug-port"
            return
        try:
            targets = discover_targets(port)
        except InjectError as exc:
            self.last_error = str(exc)
            self.log(f"discovery failed: {exc}")
            return
        wanted = {target.page_id: target for target in targets if target.page_id}
        with self._lock:
            for page_id in list(self._sessions):
                if page_id not in wanted:
                    self._close(page_id)
            for page_id, target in wanted.items():
                if page_id in self._sessions:
                    if not self._alive(page_id):
                        self._close(page_id)
                    else:
                        continue
                try:
                    self._sessions[page_id] = self._open(target)
                    self.log(f"attached {target.title or page_id}")
                    self.last_error = ""
                except InjectError as exc:
                    self.last_error = str(exc)
                    self.log(f"attach failed for {page_id}: {exc}")

    def attached(self) -> int:
        """How many pages hold a live session right now."""
        with self._lock:
            return len(self._sessions)

    def _alive(self, page_id: str) -> bool:
        probe = self.evaluate(
            page_id,
            "window.__goatProbe && window.__goatProbe()",
            return_value=True,
        )
        return isinstance(probe, dict) and bool(probe.get("mounted"))

    def _close(self, page_id: str) -> None:
        session = self._sessions.pop(page_id, None)
        if not session:
            return
        connection = session["connection"]
        try:
            connection.call(
                "Runtime.evaluate", {"expression": "window.__goatDispose && window.__goatDispose()"}
            )
            if session.get("identifier"):
                connection.call(
                    "Page.removeScriptToEvaluateOnNewDocument",
                    {"identifier": session["identifier"]},
                )
        except InjectError:
            pass
        connection.close()

    def evaluate(
        self,
        page_id: str,
        expression: str,
        *,
        return_value: bool = False,
    ) -> Any:
        with self._lock:
            session = self._sessions.get(page_id)
        if not session:
            return None
        try:
            reply = session["connection"].call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "returnByValue": return_value,
                    "awaitPromise": False,
                },
            )
        except InjectError as exc:
            self.last_error = str(exc)
            return None
        return (reply or {}).get("result", {}).get("value") if return_value else reply

    def read_theme(self) -> str:
        with self._lock:
            page_ids = list(self._sessions)
        for page_id in page_ids:
            value = self.evaluate(page_id, THEME_EXPRESSION, return_value=True)
            if isinstance(value, str) and value in ("light", "dark"):
                return value
        return ""

    def push(self, payload: dict[str, Any]) -> None:
        with self._lock:
            page_ids = list(self._sessions)
        if not page_ids:
            return
        expression = update_expression(payload)
        for page_id in page_ids:
            self.evaluate(page_id, expression)

    def snapshot(self) -> dict[str, Any]:
        try:
            payload = self.payload_builder()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"payload: {exc}"
            return {}
        return payload or {}

    def serve(self, stop_event: threading.Event, *, interval: float = 2.5) -> None:
        self._stop = stop_event
        next_attach = 0.0
        while not stop_event.is_set():
            now = time.time()
            if now >= next_attach:
                self.attach()
                next_attach = now + ATTACH_INTERVAL
                theme = self.read_theme()
                if theme:
                    self.host_theme = theme
            payload = self.snapshot()
            if payload:
                self.push(payload)
            stop_event.wait(interval)

    def detach_all(self) -> None:
        with self._lock:
            page_ids = list(self._sessions)
        for page_id in page_ids:
            with self._lock:
                has = page_id in self._sessions
            if has:
                self._close(page_id)


def detach(profile: Path | None = None) -> dict[str, Any]:
    """Remove the widget from every live Command Code page.

    The ``addScriptToEvaluateOnNewDocument`` registration belongs to the socket
    that made it, so once the injecting process has exited no later navigation
    brings the readout back.
    """
    profile = profile or commandcode_profile_dir()
    port = devtools_port(profile)
    if not port:
        return {"ok": False, "reason": "no-debug-port", "pages": 0}
    try:
        targets = discover_targets(port)
    except InjectError as exc:
        return {"ok": False, "reason": str(exc), "pages": 0}
    removed = 0
    errors: list[str] = []
    for target in targets:
        try:
            connection = CdpConnection(target.ws_url)
            try:
                connection.call(
                    "Runtime.evaluate",
                    {"expression": "window.__goatDispose && window.__goatDispose()"},
                )
                removed += 1
            finally:
                connection.close()
        except InjectError as exc:
            errors.append(f"{target.page_id}: {exc}")
    return {"ok": not errors, "pages": removed, "errors": errors}


def start_injection(
    service: Any,
    stop_event: threading.Event,
    *,
    profile: Path | None = None,
    interval: float = 2.5,
) -> WidgetInjector:
    def payload() -> dict[str, Any]:
        # The renderer's theme is the only trustworthy copy, so hand it back to
        # the service for the preview page to follow.
        service.host_theme = injector.host_theme or getattr(service, "host_theme", "")
        return build_payload(service)

    injector = WidgetInjector(profile=profile, payload_builder=payload)
    threading.Thread(
        target=injector.serve,
        args=(stop_event,),
        kwargs={"interval": interval},
        daemon=True,
        name="goat-gauge-inject",
    ).start()
    return injector


def build_payload(service: Any) -> dict[str, Any]:
    """Reuse the live endpoint's maths so the widget needs no computation."""
    live = service.live()
    fresh = live.get("fresh") or {}
    return {
        # The widget drops any payload whose version does not match, so the tag
        # belongs here rather than in whichever caller remembers to add it.
        "version": WIDGET_VERSION,
        # Synthetic and real numbers must never be indistinguishable on the host:
        # a demo payload that silently replaces a live one is how a 70-credit
        # synthetic pool ended up read as the account's real ceiling.
        "mode": live.get("mode") or ("demo" if getattr(service, "demo", False) else "live"),
        "server_time": live.get("server_time"),
        "host": live.get("host") or {},
        "rates": live.get("rates") or {},
        "projections": live.get("projections") or {},
        "monthly": live.get("monthly") or {},
        "fresh": {
            "state": fresh.get("state"),
            "age_ms": fresh.get("age_ms"),
            "last_error": fresh.get("last_error"),
        },
    }
