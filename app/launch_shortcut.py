"""Give the user's own Start Menu shortcut the loopback debugging arguments.

The readout can only attach to a host that opened a debugging port, and Chromium
reads that flag at process start. Upstream ships the same answer for the same
problem: its README tells users to append the two arguments to the shortcut target
and keep using that shortcut. So this module writes exactly one thing - the
``Arguments`` field of the shortcut **inside the current user's profile** - and never
touches the host install directory or the machine-wide ``ProgramData`` shortcut.

Everything reversible is backed up twice: the original ``Arguments`` string in a JSON
record, and a byte copy of the ``.lnk`` before the first write.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

DEBUG_ARGS = ("--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1")
SHORTCUT_NAME = "Command Code.lnk"
BACKUP_NAME = "launch-shortcut.json"
LNK_BACKUP_NAME = "launch-shortcut.lnk.bak"

READ_SCRIPT = """$ErrorActionPreference = 'Stop'
try {{
  $shell = New-Object -ComObject WScript.Shell
  $link = $shell.CreateShortcut('{path}')
  'TARGET=' + $link.TargetPath
  'ARGS=' + $link.Arguments
  'WORKDIR=' + $link.WorkingDirectory
}} catch {{
  'ERR=' + $_.Exception.Message
}}
"""

WRITE_SCRIPT = """$ErrorActionPreference = 'Stop'
try {{
  $shell = New-Object -ComObject WScript.Shell
  $link = $shell.CreateShortcut('{path}')
  $link.Arguments = '{args}'
  $link.Save()
  'WROTE=' + $link.Arguments
}} catch {{
  'ERR=' + $_.Exception.Message
}}
"""


def default_shortcut(env: dict[str, str] | None = None) -> Path:
    """The per-user Start Menu entry, which the current user can write without elevation."""
    source = env if env is not None else _environ()
    appdata = (source.get("APPDATA") or "").strip()
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def quote(value: str) -> str:
    """PowerShell single-quoted literal: the only escape inside is a doubled quote."""
    return value.replace("'", "''")


def has_all(arguments: str, wanted: tuple[str, ...] = DEBUG_ARGS) -> bool:
    present = set((arguments or "").split())
    return all(flag in present for flag in wanted)


def merge_arguments(arguments: str, wanted: tuple[str, ...] = DEBUG_ARGS) -> str:
    """Append only the flags that are missing, so re-running never duplicates them."""
    parts = (arguments or "").split()
    for flag in wanted:
        if flag.split("=")[0] not in " ".join(parts):
            parts.append(flag)
    return " ".join(parts).strip()


def _run(script: str, capture: Callable[[list[str]], str] | None) -> dict[str, str]:
    argv = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    raw = capture(argv) if capture else subprocess.run(
        argv, capture_output=True, text=True, errors="replace", timeout=30
    ).stdout
    fields: dict[str, str] = {}
    for line in (raw or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    return fields


def read(shortcut: Path, capture: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    if not shortcut.exists():
        return {"state": "absent", "target": "", "arguments": ""}
    fields = _run(READ_SCRIPT.format(path=quote(str(shortcut))), capture)
    if fields.get("ERR"):
        return {"state": "error", "target": "", "arguments": "", "error": fields["ERR"]}
    return {"state": "ok", "target": fields.get("TARGET", ""), "arguments": fields.get("ARGS", "")}


def backup_path() -> Path:
    from .watchdog import runtime_dir

    return runtime_dir() / BACKUP_NAME


def apply(shortcut: Path | None = None, *, capture: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Add the debugging arguments once, keeping whatever the user already had."""
    shortcut = shortcut or default_shortcut()
    current = read(shortcut, capture=capture)
    if current["state"] != "ok":
        return {"ok": False, "state": current["state"], "shortcut": str(shortcut)}
    record = backup_path()
    if has_all(current["arguments"]):
        return {"ok": True, "state": "already", "shortcut": str(shortcut),
                "arguments": current["arguments"], "backed_up": record.exists()}
    if not record.exists():
        record.parent.mkdir(parents=True, exist_ok=True)
        payload = {"shortcut": str(shortcut), "arguments": current["arguments"], "target": current["target"]}
        record.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        shutil.copy2(shortcut, record.parent / LNK_BACKUP_NAME)
    merged = merge_arguments(current["arguments"])
    written = _run(WRITE_SCRIPT.format(path=quote(str(shortcut)), args=quote(merged)), capture)
    if written.get("ERR"):
        return {"ok": False, "state": "error", "shortcut": str(shortcut), "error": written["ERR"]}
    after = read(shortcut, capture=capture)
    return {"ok": has_all(after["arguments"]), "state": "patched" if after["state"] == "ok" else after["state"],
            "shortcut": str(shortcut), "before": current["arguments"], "after": after["arguments"],
            "backup": str(record)}


def restore(*, capture: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Put back exactly the arguments we found before the first patch."""
    record = backup_path()
    if not record.exists():
        return {"ok": True, "state": "nothing-to-restore"}
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "state": "unreadable-backup", "error": type(exc).__name__}
    shortcut = Path(str(payload.get("shortcut") or default_shortcut()))
    original = str(payload.get("arguments") or "")
    written = _run(WRITE_SCRIPT.format(path=quote(str(shortcut)), args=quote(original)), capture)
    if written.get("ERR"):
        return {"ok": False, "state": "error", "shortcut": str(shortcut), "error": written["ERR"]}
    after = read(shortcut, capture=capture)
    exact = after.get("arguments", "") == original
    if exact:
        record.unlink(missing_ok=True)
        (record.parent / LNK_BACKUP_NAME).unlink(missing_ok=True)
    return {"ok": exact, "state": "restored" if exact else "differs", "shortcut": str(shortcut),
            "expected": original, "actual": after.get("arguments", "")}


def inspect(shortcut: Path | None = None, *, capture: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Reported by ``--status``: whether a plain double-click would carry the port."""
    shortcut = shortcut or default_shortcut()
    current = read(shortcut, capture=capture)
    if current["state"] != "ok":
        return {"state": current["state"], "shortcut": str(shortcut)}
    return {
        "state": "ready" if has_all(current["arguments"]) else "missing-args",
        "shortcut": str(shortcut),
        "target": current["target"],
        "arguments": current["arguments"],
        "backed_up": backup_path().exists(),
    }
