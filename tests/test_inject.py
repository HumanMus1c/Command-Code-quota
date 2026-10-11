"""Tests for the Command Code injection layer: discovery, CDP client, lifecycle."""

from __future__ import annotations

import io
import json
import sys
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app import inject
from app.inject import (
    CdpConnection,
    CdpTarget,
    InjectError,
    WidgetInjector,
    devtools_port,
    discover_targets,
    guarded_source,
    update_expression,
    widget_source,
)


class FakeResponse(io.BytesIO):
    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class PortFileTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, text: str) -> None:
        (self.dir / "DevToolsActivePort").write_text(text, encoding="utf-8")

    def test_reads_first_line(self) -> None:
        self.write("9333\n/devtools/browser/abc-123\n")
        self.assertEqual(devtools_port(self.dir), 9333)

    def test_missing_file_is_not_an_error(self) -> None:
        self.assertIsNone(devtools_port(self.dir))

    def test_garbage_and_out_of_range_are_rejected(self) -> None:
        for text in ("", "   ", "not-a-port\n", "0\n", "70000\n", "1\n"):
            self.write(text)
            expected = 1 if text == "1\n" else None
            self.assertEqual(devtools_port(self.dir), expected, text)

    def test_oversized_file_is_capped(self) -> None:
        self.write("9333\n" + "x" * 100_000)
        self.assertEqual(devtools_port(self.dir), 9333)


class DiscoveryTestCase(unittest.TestCase):
    def patch_listing(self, payload: object, size: int | None = None) -> None:
        body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        if size is not None:
            body = body + b"x" * size
        opener = mock.Mock()
        opener.open.return_value = FakeResponse(body)
        patcher = mock.patch.object(inject, "_loopback_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keeps_only_loopback_pages_on_the_same_port(self) -> None:
        self.patch_listing(
            [
                {
                    "type": "page",
                    "id": "good",
                    "url": "https://app.commandcode.ai/c/1",
                    "title": "Command Code",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/good",
                },
                {
                    "type": "iframe",
                    "id": "frame",
                    "url": "https://x/",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/frame",
                },
                {
                    "type": "page",
                    "id": "remote",
                    "url": "https://x/",
                    "webSocketDebuggerUrl": "ws://evil.example.com:9333/devtools/page/remote",
                },
                {
                    "type": "page",
                    "id": "otherport",
                    "url": "https://x/",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:1234/devtools/page/otherport",
                },
                {
                    "type": "page",
                    "id": "chrome",
                    "url": "chrome://newtab",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/chrome",
                },
            ]
        )
        targets = discover_targets(9333)
        self.assertEqual([item.page_id for item in targets], ["good"])
        self.assertEqual(targets[0].origin, "https://app.commandcode.ai")

    def test_file_origin_pages_are_accepted(self) -> None:
        self.patch_listing(
            [
                {
                    "type": "page",
                    "id": "local",
                    "url": "file:///app/index.html",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/local",
                }
            ]
        )
        self.assertEqual(len(discover_targets(9333)), 1)

    def test_oversized_listing_raises(self) -> None:
        self.patch_listing([{"type": "page", "id": "x"}], size=inject.MAX_TARGETS_BYTES + 10)
        with self.assertRaises(InjectError):
            discover_targets(9333)

    def test_non_json_listing_raises(self) -> None:
        self.patch_listing(b"<html>nope</html>")
        with self.assertRaises(InjectError):
            discover_targets(9333)

    def test_unreachable_endpoint_raises_inject_error(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("refused")
        patcher = mock.patch.object(inject, "_loopback_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(InjectError):
            discover_targets(9333)


class PayloadModeTestCase(unittest.TestCase):
    def test_build_payload_carries_the_service_mode(self) -> None:
        class Service:
            def live(self):
                return {"mode": "demo", "rates": {}, "projections": {}, "monthly": {}, "host": {}, "fresh": {}}

        self.assertEqual(inject.build_payload(Service())["mode"], "demo")

    def test_unlabelled_payload_is_not_assumed_live(self) -> None:
        class Service:
            def live(self):
                return {"rates": {}, "projections": {}, "monthly": {}, "host": {}, "fresh": {}}

        self.assertEqual(inject.build_payload(Service())["mode"], "live")


class SourceWrappingTestCase(unittest.TestCase):
    def test_guard_checks_origin_and_version(self) -> None:
        wrapped = guarded_source("void 0;", make_target())
        self.assertIn('location.origin!=="https://app.commandcode.ai"', wrapped)
        self.assertIn("window.__goatWidgetVersion", wrapped)

    def test_a_stale_build_is_disposed_before_the_new_one_runs(self) -> None:
        # Two copies of the script in one page means two heartbeats rendering into
        # the same slot, so the older build has to be torn down first.
        wrapped = guarded_source("void 0;", make_target())
        self.assertIn("window.__goatDispose()", wrapped)
        self.assertLess(wrapped.index("window.__goatDispose()"), wrapped.index("void 0;"))

    def test_file_pages_guard_on_protocol_because_origin_is_null(self) -> None:
        # Chrome reports location.origin === "null" for file:// documents; an
        # origin equality guard there would silently never mount.
        target = make_target(url="file:///app/out/renderer/index.html")
        self.assertEqual(target.origin, "")
        wrapped = guarded_source("void 0;", target)
        self.assertIn('location.protocol!=="file:"', wrapped)
        self.assertNotIn("location.origin", wrapped)

    def test_origin_of_omits_default_ports_and_keeps_explicit_ones(self) -> None:
        self.assertEqual(inject.origin_of("https://a.b/c"), "https://a.b")
        self.assertEqual(inject.origin_of("https://a.b:8443/c"), "https://a.b:8443")
        self.assertEqual(inject.origin_of("http://127.0.0.1:3000/"), "http://127.0.0.1:3000")
        self.assertEqual(inject.origin_of("file:///x"), "")

    def test_widget_source_is_loadable(self) -> None:
        source = widget_source()
        self.assertIn("__goatUpdate", source)
        self.assertIn("cc-composer", source)

    def test_payload_cannot_break_out_of_the_expression(self) -> None:
        expression = update_expression({"model": "</script><img onerror=alert(1)>"})
        self.assertNotIn("<", expression.replace("<!", ""))
        self.assertIn("\\u003c", expression)


class FakeSocket:
    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.replies = replies
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        if not self.replies:
            raise OSError("no more replies")
        return json.dumps(self.replies.pop(0))

    def close(self) -> None:
        self.closed = True


class CdpConnectionTestCase(unittest.TestCase):
    def fake_module(self, socket: FakeSocket) -> None:
        module = types.ModuleType("websocket")
        module.create_connection = lambda *a, **k: socket  # type: ignore[attr-defined]
        patcher = mock.patch.dict(sys.modules, {"websocket": module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_skips_events_and_matches_the_request_id(self) -> None:
        socket = FakeSocket(
            [
                {"method": "Page.loadEventFired"},
                {"id": 99, "result": {"unrelated": True}},
                {"id": 1, "result": {"identifier": "7"}},
            ]
        )
        self.fake_module(socket)
        connection = CdpConnection("ws://127.0.0.1:1/x")
        result = connection.call("Page.addScriptToEvaluateOnNewDocument", {"source": "s"})
        self.assertEqual(result, {"identifier": "7"})
        self.assertEqual(socket.sent[0]["method"], "Page.addScriptToEvaluateOnNewDocument")
        self.assertEqual(socket.sent[0]["params"], {"source": "s"})

    def test_error_reply_becomes_inject_error(self) -> None:
        socket = FakeSocket([{"id": 1, "error": {"message": "Not allowed"}}])
        self.fake_module(socket)
        connection = CdpConnection("ws://127.0.0.1:1/x")
        with self.assertRaises(InjectError):
            connection.call("Runtime.enable")

    def test_dropped_socket_reports_interruption(self) -> None:
        socket = FakeSocket([])
        self.fake_module(socket)
        connection = CdpConnection("ws://127.0.0.1:1/x")
        with self.assertRaises(InjectError):
            connection.call("Runtime.enable")


def make_target(page_id: str = "p1", url: str = "https://app.commandcode.ai/c/1") -> CdpTarget:
    return CdpTarget(
        page_id=page_id,
        ws_url=f"ws://127.0.0.1:9333/devtools/page/{page_id}",
        url=url,
        origin=inject.origin_of(url),
        title="Command Code",
    )


class RecordingConnection:
    def __init__(self, *, fail_on: str = "", replies: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False
        self.fail_on = fail_on
        self.replies = replies or {}

    def call(self, method: str, params: dict[str, object] | None = None) -> object:
        self.calls.append((method, dict(params or {})))
        if method == self.fail_on:
            raise InjectError(f"{method} refused")
        return self.replies.get(method)

    def close(self) -> None:
        self.closed = True


class InjectorLifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.payload_calls: list[int] = []

    def payload(self) -> dict[str, object]:
        self.payload_calls.append(1)
        return {"rates": {"requests_per_min": 1.5}}

    def build(self, connection: RecordingConnection) -> WidgetInjector:
        injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
        patcher = mock.patch.object(injector, "_open", return_value={"connection": connection, "identifier": "7", "target": make_target()})
        patcher.start()
        self.addCleanup(patcher.stop)
        return injector

    def test_attach_uses_the_registered_preload_then_evaluates_once(self) -> None:
        connection = RecordingConnection(replies={"Page.getFrameTree": {}})
        injector = self.build(connection)
        with mock.patch.object(inject, "devtools_port", return_value=9333), mock.patch.object(
            inject, "discover_targets", return_value=[make_target()]
        ):
            injector.attach()
        self.assertEqual(list(injector._sessions), ["p1"])

    def test_reconcile_of_a_live_page_does_not_deadlock_on_its_own_lock(self) -> None:
        # attach() probes liveness while holding the session lock, and that probe
        # re-enters through evaluate(). With a non-reentrant lock the watchdog
        # hangs on its very first reconcile, which no socket-less test ever reaches.
        connection = RecordingConnection(
            replies={"Runtime.evaluate": {"result": {"value": {"mounted": True}}}}
        )
        injector = self.build(connection)
        with mock.patch.object(inject, "devtools_port", return_value=9333), mock.patch.object(
            inject, "discover_targets", return_value=[make_target()]
        ):
            injector.attach()
            finished = threading.Event()

            def second() -> None:
                injector.attach()
                finished.set()

            worker = threading.Thread(target=second, daemon=True)
            worker.start()
            self.assertTrue(finished.wait(2.0), "attach() deadlocked reconciling a live page")
        self.assertEqual(injector.attached(), 1)

    def test_open_registers_and_evaluates_in_order(self) -> None:
        # Exercise the real _open path with a fake CdpConnection.
        connection = RecordingConnection(replies={"Page.addScriptToEvaluateOnNewDocument": {"identifier": "abc"}})
        with mock.patch.object(inject, "CdpConnection", return_value=connection):
            injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
            session = injector._open(make_target())
        methods = [name for name, _ in connection.calls]
        self.assertEqual(
            methods,
            ["Runtime.enable", "Page.enable", "Page.addScriptToEvaluateOnNewDocument", "Runtime.evaluate"],
        )
        self.assertEqual(session["identifier"], "abc")

    def test_evaluate_failure_removes_the_registered_preload(self) -> None:
        # A leaked addScriptToEvaluateOnNewDocument survives the socket, so it
        # must be undone when the first live evaluation fails.
        connection = RecordingConnection(
            fail_on="Runtime.evaluate",
            replies={"Page.addScriptToEvaluateOnNewDocument": {"identifier": "abc"}},
        )
        with mock.patch.object(inject, "CdpConnection", return_value=connection):
            injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
            with self.assertRaises(InjectError):
                injector._open(make_target())
        self.assertIn(
            "Page.removeScriptToEvaluateOnNewDocument",
            [name for name, _ in connection.calls],
        )
        self.assertTrue(connection.closed)

    def test_detach_disposes_the_widget_and_the_preload(self) -> None:
        connection = RecordingConnection(replies={"Page.addScriptToEvaluateOnNewDocument": {"identifier": "abc"}})
        with mock.patch.object(inject, "CdpConnection", return_value=connection):
            injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
            injector._sessions["p1"] = injector._open(make_target())
            injector.detach_all()
        names = [name for name, _ in connection.calls]
        self.assertIn("Runtime.evaluate", names)
        self.assertIn("Page.removeScriptToEvaluateOnNewDocument", names)
        self.assertEqual(injector._sessions, {})
        self.assertTrue(connection.closed)

    def test_push_carries_the_versioned_payload(self) -> None:
        connection = RecordingConnection()
        injector = self.build(connection)
        injector._sessions["p1"] = {"connection": connection, "identifier": "7", "target": make_target()}
        injector.push(self.payload())
        expression = connection.calls[-1][1].get("expression", "")
        self.assertTrue(str(expression).startswith("window.__goatUpdate"))
        self.assertIn("requests_per_min", str(expression))

    def test_snapshot_passes_the_builder_payload_through(self) -> None:
        injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
        self.assertEqual(injector.snapshot(), self.payload())

    def test_snapshot_error_does_not_kill_the_loop(self) -> None:
        def boom() -> dict[str, object]:
            raise RuntimeError("store closed")

        injector = WidgetInjector(profile=Path("/unused"), payload_builder=boom)
        self.assertEqual(injector.snapshot(), {})
        self.assertIn("store closed", injector.last_error)

    def test_pushed_payload_carries_the_expected_version(self) -> None:
        # A payload without a version is silently dropped by __goatUpdate, so
        # the builder itself must tag it.
        class Service:
            def live(self) -> dict[str, object]:
                return {
                    "server_time": 1,
                    "host": {"theme": "light"},
                    "rates": {"hit_rate_pct": 12.5, "requests_per_min": 2},
                    "projections": {"five_hour": {}},
                    "fresh": {"state": "live"},
                }

        payload = inject.build_payload(Service())  # type: ignore[arg-type]
        self.assertEqual(payload["version"], inject.WIDGET_VERSION)
        self.assertEqual(payload["rates"]["hit_rate_pct"], 12.5)
        self.assertEqual(payload["host"]["theme"], "light")
        self.assertNotIn("series", payload)

    def test_stale_page_is_dropped_on_next_attach(self) -> None:
        connection = RecordingConnection()
        injector = self.build(connection)
        injector._sessions["vanished"] = {
            "connection": connection,
            "identifier": "7",
            "target": make_target("vanished"),
        }
        with mock.patch.object(inject, "devtools_port", return_value=9333), mock.patch.object(
            inject, "discover_targets", return_value=[]
        ):
            injector.attach()
        self.assertEqual(injector._sessions, {})
        self.assertTrue(connection.closed)

    def test_host_theme_is_read_from_the_live_renderer(self) -> None:
        connection = RecordingConnection(
            replies={"Runtime.evaluate": {"result": {"value": "dark"}}}
        )
        injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
        injector._sessions["p1"] = {
            "connection": connection,
            "identifier": "7",
            "target": make_target(),
        }
        self.assertEqual(injector.read_theme(), "dark")
        expression = connection.calls[-1][1].get("expression", "")
        self.assertIn("data-theme", str(expression))
        self.assertIn("classList", str(expression))

    def test_unusable_theme_answer_is_ignored(self) -> None:
        connection = RecordingConnection(replies={"Runtime.evaluate": {"result": {"value": ""}}})
        injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
        injector._sessions["p1"] = {
            "connection": connection,
            "identifier": "7",
            "target": make_target(),
        }
        self.assertEqual(injector.read_theme(), "")

    def test_missing_debug_port_is_reported_without_raising(self) -> None:
        injector = WidgetInjector(profile=Path("/unused"), payload_builder=self.payload)
        with mock.patch.object(inject, "devtools_port", return_value=None):
            injector.attach()
        self.assertEqual(injector.last_error, "no-debug-port")

    def test_serve_loop_stops_on_event(self) -> None:
        connection = RecordingConnection()
        injector = self.build(connection)
        stop = threading.Event()
        with mock.patch.object(inject, "devtools_port", return_value=None):
            thread = threading.Thread(target=injector.serve, args=(stop,), kwargs={"interval": 0.01})
            thread.start()
            thread.join(timeout=2)
            stop.set()
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
