"""Tests for the host-following watchdog (spec L-1..L-6, N-6, D-5/D-8)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from app import watchdog

NETSTAT_OURS = """
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:53584        0.0.0.0:0              LISTENING       4242
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       900
  TCP    127.0.0.1:53584        127.0.0.1:40001        ESTABLISHED     4242
  UDP    0.0.0.0:5353           *:*                                    900
"""
NETSTAT_OTHER = NETSTAT_OURS.replace("127.0.0.1:53584", "127.0.0.1:53585")
NETSTAT_REMOTE = """
  TCP    10.0.0.5:53584         0.0.0.0:0              LISTENING       4242
"""
CSV_OURS = '"Command Code.exe","4242","Console","1","300,000 K"\n'
CSV_OTHER = '"code.exe","4242","Console","1","300,000 K"\n'
CSV_HEADER_ONLY = '"Image Name","PID","Session Name","Session#","Mem Usage"\n' + CSV_OURS


class ParseTests(unittest.TestCase):
    def test_loopback_listener_wins_over_established_row(self) -> None:
        self.assertEqual(watchdog.parse_listening_pid(NETSTAT_OURS, 53584), 4242)

    def test_wrong_port_and_non_tcp_rows_are_ignored(self) -> None:
        self.assertIsNone(watchdog.parse_listening_pid(NETSTAT_OTHER, 53584))
        self.assertIsNone(watchdog.parse_listening_pid(NETSTAT_REMOTE, 53584))

    def test_non_loopback_bind_is_refused(self) -> None:
        self.assertIsNone(watchdog.parse_listening_pid(NETSTAT_REMOTE, 53584))

    def test_process_name_reads_by_position_not_header(self) -> None:
        self.assertEqual(watchdog.parse_process_name(CSV_HEADER_ONLY, 4242), "Command Code.exe")
        self.assertEqual(watchdog.parse_process_name(CSV_OURS, 4242), "Command Code.exe")
        self.assertEqual(watchdog.parse_process_name(CSV_OURS, 77), "")

    def test_process_count(self) -> None:
        self.assertEqual(watchdog.parse_process_count(CSV_OURS * 3, "Command Code.exe"), 3)
        self.assertEqual(watchdog.parse_process_count(CSV_HEADER_ONLY, "Command Code.exe"), 1)


class OwnershipTests(unittest.TestCase):
    def test_pid_is_returned_only_when_the_owner_is_the_host(self) -> None:
        def capture(argv):
            if argv[0] == "netstat":
                return NETSTAT_OURS
            return CSV_OURS

        self.assertEqual(watchdog.host_listener(53584, capture=capture), 4242)

        def capture_other(argv):
            if argv[0] == "netstat":
                return NETSTAT_OURS
            return CSV_OTHER

        self.assertIsNone(watchdog.host_listener(53584, capture=capture_other))

    def test_no_port_means_no_probe_and_no_lookup(self) -> None:
        def capture(argv):  # pragma: no cover - must not be reached
            raise AssertionError("缺席的端口不该发起系统查询")

        self.assertIsNone(watchdog.host_listener(None, capture=capture))
        self.assertIsNone(watchdog.host_listener(0, capture=capture))

    def test_alive_uses_tasklist_and_never_signal_zero(self) -> None:
        seen = []

        def capture(argv):
            seen.append(argv)
            return CSV_OURS

        self.assertTrue(watchdog.pid_alive(4242, capture=capture))
        self.assertFalse(watchdog.pid_alive(-1, capture=capture))
        self.assertTrue(seen and all("kill" not in " ".join(row).lower() for row in seen))


class RuntimeDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-watchdog-"))
        self.environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_atomic_write_and_read_round_trip(self) -> None:
        path = watchdog.runtime_dir() / "nested" / "state.json"
        watchdog.write_json_atomic(path, {"b": 2, "a": 1})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1, "b": 2})
        self.assertEqual([leftover.name for leftover in path.parent.iterdir()], ["state.json"])

    def test_writing_outside_the_runtime_dir_is_refused(self) -> None:
        outside = self.dir.parent / f"goat-escape-{self.dir.name}.json"
        with self.assertRaises(ValueError):
            watchdog.write_json_atomic(outside, {"x": 1})
        self.assertFalse(outside.exists())

    def test_corrupt_file_reads_as_absent(self) -> None:
        broken = watchdog.runtime_dir() / "broken.json"
        broken.write_text('{"pid": 1', encoding="utf-8")
        self.assertIsNone(watchdog.read_json(broken))

    def test_live_foreign_owner_blocks_us_but_a_dead_one_does_not(self) -> None:
        lock = watchdog.runtime_dir() / watchdog.LOCK_NAME
        watchdog.write_json_atomic(lock, {"pid": 9999, "token": "other"})
        dead = mock.Mock(return_value=False)
        alive = mock.Mock(return_value=True)
        self.assertTrue(watchdog.acquire_lock(lock, token="mine", pid=1, alive=dead))
        self.assertEqual(watchdog.read_json(lock)["token"], "mine")
        self.assertFalse(watchdog.acquire_lock(lock, token="third", pid=2, alive=alive))
        self.assertEqual(watchdog.read_json(lock)["token"], "mine")

    def test_release_only_removes_our_own_lock(self) -> None:
        lock = watchdog.runtime_dir() / watchdog.LOCK_NAME
        watchdog.write_json_atomic(lock, {"pid": 1, "token": "mine"})
        watchdog.release_lock(lock, token="someone-else")
        self.assertTrue(lock.exists())
        watchdog.release_lock(lock, token="mine")
        self.assertFalse(lock.exists())


class FakeInjector:
    def __init__(self, **kwargs) -> None:
        self.count = 0
        self.detach_calls = 0

    def attached(self) -> int:
        return self.count

    def attach(self) -> None:
        self.count = 1

    def detach_all(self) -> None:
        if self.count:
            self.detach_calls += 1
        self.count = 0


class FollowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-follow-"))
        self.environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.profile = self.dir / "profile"
        self.profile.mkdir()

    def _run(self, ports: list[int | None], capture, stops_after: int = 4) -> tuple[list, FakeInjector]:
        remaining = list(ports)
        logs: list[str] = []
        injector = FakeInjector()
        clock_values = iter([float(step) * 40.0 for step in range(stops_after)])

        def sleeper(_seconds: float) -> None:
            sleeper.calls += 1
            if sleeper.calls >= 4:
                raise KeyboardInterrupt

        sleeper.calls = 0

        with (
            mock.patch.object(watchdog, "WidgetInjector", lambda **kwargs: injector),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: remaining.pop(0) if remaining else None),
        ):
            watchdog.follow(
                profile=self.profile,
                log=logs.append,
                capture=capture,
                clock=lambda: next(clock_values),
                sleeper=sleeper,
            )
        return logs, injector

    def test_attaches_only_while_the_host_owns_the_port(self) -> None:
        def capture(argv):
            return NETSTAT_OURS if argv[0] == "netstat" else CSV_OURS

        logs, injector = self._run([None, 53584, 53584, None], capture)
        self.assertEqual(injector.count, 0)
        self.assertGreaterEqual(injector.detach_calls, 1)
        self.assertIn("宿主或调试端口已消失", " ".join(logs))

    def test_absent_host_opens_no_devtools_session(self) -> None:
        def capture(argv):  # pragma: no cover - a missing host must not be probed
            raise AssertionError("宿主缺席时不得发起任何系统查询")

        logs, injector = self._run([None, None, None, None], capture, stops_after=4)
        self.assertEqual(injector.count, 0)
        self.assertEqual(injector.detach_calls, 0)
        self.assertEqual(logs, [])

    def test_second_instance_backs_off(self) -> None:
        lock = watchdog.runtime_dir() / watchdog.LOCK_NAME
        watchdog.write_json_atomic(lock, {"pid": 4242, "token": "other"})
        logs, _injector = self._run(
            [None, None, None, None], lambda argv: CSV_OURS if argv[0] == "tasklist" else ""
        )
        self.assertIn("已有守护进程在跟随", " ".join(logs))
        self.assertEqual(watchdog.read_json(lock)["token"], "other")


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = Path(tempfile.mkdtemp(prefix="goat-profile-"))

    def test_running_without_a_port_refuses_to_restart_the_session(self) -> None:
        spawned = []

        def capture(argv):
            if argv[0] == "tasklist" and "IMAGENAME" in " ".join(argv):
                return CSV_OURS
            return CSV_OTHER  # no host-owned listener

        result = watchdog.launch_host(
            r"D:\Program Files\Command Code\Command Code.exe",
            profile=self.profile,
            capture=capture,
            spawn=spawned.append,
        )
        self.assertEqual(result["state"], "running-without-port")
        self.assertFalse(result["ok"])
        self.assertEqual(spawned, [])

    def test_launch_carries_loopback_and_random_port(self) -> None:
        spawned = []
        result = watchdog.launch_host(
            r"D:\Program Files\Command Code\Command Code.exe",
            profile=self.profile,
            capture=lambda argv: "",
            spawn=spawned.append,
        )
        self.assertTrue(result["ok"])
        self.assertEqual(spawned, [[r"D:\Program Files\Command Code\Command Code.exe", *watchdog.LAUNCH_ARGS]])
        self.assertIn("--remote-debugging-port=0", watchdog.LAUNCH_ARGS)
        self.assertIn("127.0.0.1", " ".join(watchdog.LAUNCH_ARGS))


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="goat-probe-"))
        self.environment = mock.patch.dict(os.environ, {"GOATGAUGE_DATA": str(self.dir)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.profile = Path(tempfile.mkdtemp(prefix="goat-probe-profile-"))

    def test_only_shapes_and_a_hash_are_written(self) -> None:
        value = {
            "ok": True,
            "accountHash": "a" * 12,
            # Anything the page returns beyond the declared fields must not be
            # echoed, so a stray value is planted to prove the whitelist holds.
            "leak": "alice@corp.example",
            "identity": "Alice",
            "methods": {"accountOverview": "function"},
            "shapes": {
                "accountOverview": {
                    "user": {"id": "string", "email": "string"},
                    "windowLimits": {"fiveHour": {"used": "number", "cap": "number"}},
                }
            },
        }
        page = mock.Mock(ws_url="ws://127.0.0.1:1/t", page_id="t")
        with (
            mock.patch.object(watchdog, "CdpConnection", _connection_factory(value)),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: 53584),
            mock.patch.object(watchdog, "discover_targets", lambda _port: [page]),
        ):
            result = watchdog.probe_host(profile=self.profile, now=lambda: 100.0)
        self.assertTrue(result["ok"])
        written = Path(result["path"])
        self.assertEqual(written.parent.name, "a" * 12)
        body = written.read_text(encoding="utf-8")
        self.assertIn("accountOverview", body)
        self.assertNotIn("alice@corp.example", body)
        self.assertNotIn("Alice", body)
        self.assertNotIn("a" * 12, body)

    def test_no_resolvable_account_writes_nothing(self) -> None:
        value = {"ok": False, "reason": "no-account-identity", "shapes": {}}
        page = mock.Mock(ws_url="ws://127.0.0.1:1/t", page_id="t")
        with (
            mock.patch.object(watchdog, "CdpConnection", _connection_factory(value)),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: 53584),
            mock.patch.object(watchdog, "discover_targets", lambda _port: [page]),
        ):
            result = watchdog.probe_host(profile=self.profile)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no-account-identity")
        self.assertEqual(sorted(path.name for path in watchdog.runtime_dir().glob("*")), [])

    def test_oversized_self_report_is_rejected(self) -> None:
        value = {
            "ok": True,
            "accountHash": "b" * 12,
            "methods": {},
            "shapes": {"accountOverview": {"k": "v" * (watchdog.MAX_PROBE_BYTES + 1)}},
        }
        page = mock.Mock(ws_url="ws://127.0.0.1:1/t", page_id="t")
        with (
            mock.patch.object(watchdog, "CdpConnection", _connection_factory(value)),
            mock.patch.object(watchdog, "devtools_port", lambda _profile: 53584),
            mock.patch.object(watchdog, "discover_targets", lambda _port: [page]),
        ):
            result = watchdog.probe_host(profile=self.profile)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "probe-too-large")


def _connection_factory(value):
    class Connection:
        def __init__(self, _ws_url, *, timeout=0) -> None:
            pass

        def call(self, _method, _params=None):
            return {"result": {"value": value}}

        def close(self) -> None:
            pass

    return Connection


if __name__ == "__main__":
    unittest.main()
