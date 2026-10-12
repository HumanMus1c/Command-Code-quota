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


def machine_shortcut(env: dict[str, str] | None = None) -> Path:
    """The all-users entry the installer created. Start Menu search often resolves to
    this one instead of the per-user file, so patching only the latter leaves a
    double-click that carries no arguments - measured on the live machine."""
    source = env if env is not None else _environ()
    root = (source.get("ProgramData") or "").strip()
    base = Path(root) if root else Path("C:\\ProgramData")
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / SHORTCUT_NAME


def candidates(env: dict[str, str] | None = None) -> list[Path]:
    """Every existing shortcut by which the host can be launched from the shell."""
    found: list[Path] = []
    for path in (default_shortcut(env), machine_shortcut(env)):
        if path.exists() and path not in found:
            found.append(path)
    return found


def scope_of(path: Path) -> str:
    return "user" if str(path).lower().startswith("c:\\users") else "machine"


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


def lnk_backup_name(shortcut: Path) -> str:
    return f"launch-shortcut-{scope_of(shortcut)}.lnk.bak"


def _load_entries(record: Path) -> dict[str, Any]:
    if not record.exists():
        return {}
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if isinstance(entries, dict):
        return {str(key): dict(value) for key, value in entries.items()}
    # First release stored a single shortcut at the top level.
    if isinstance(payload, dict) and "shortcut" in payload:
        return {str(payload["shortcut"]): {"arguments": payload.get("arguments", ""),
                                           "target": payload.get("target", "")}}
    return {}


def _save_entries(record: Path, entries: dict[str, Any]) -> None:
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(
        json.dumps({"entries": entries}, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )


def _patch_one(shortcut: Path, record: Path, entries: dict[str, Any],
               capture: Callable[[list[str]], str] | None) -> dict[str, Any]:
    base = {"shortcut": str(shortcut), "scope": scope_of(shortcut)}
    current = read(shortcut, capture=capture)
    if current["state"] != "ok":
        return {**base, "ok": False, "state": current["state"]}
    if has_all(current["arguments"]):
        return {**base, "ok": True, "state": "already", "arguments": current["arguments"]}
    if str(shortcut) not in entries:
        # Back up before the first write, twice: the exact Arguments string, and the
        # whole file, because Save() rewrites the link and can drop fields we never read.
        entries[str(shortcut)] = {"arguments": current["arguments"], "target": current["target"]}
        _save_entries(record, entries)
        try:
            shutil.copy2(shortcut, record.parent / lnk_backup_name(shortcut))
        except OSError:
            pass
    merged = merge_arguments(current["arguments"])
    written = _run(WRITE_SCRIPT.format(path=quote(str(shortcut)), args=quote(merged)), capture)
    if written.get("ERR"):
        return {**base, "ok": False, "state": "denied-or-error", "error": written["ERR"]}
    after = read(shortcut, capture=capture)
    return {**base, "ok": has_all(after.get("arguments", "")), "state": "patched",
            "before": current["arguments"], "after": after.get("arguments", "")}


def apply(shortcut: Path | None = None, *, capture: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Add the debugging arguments once to every launch shortcut that exists.

    Both the per-user entry and the installer's all-users entry are patched: the Start
    Menu search result the user actually clicks is frequently the machine-wide one, so
    a per-user-only patch silently does nothing.
    """
    paths = [shortcut] if shortcut is not None else candidates()
    if not paths:
        return {"ok": False, "state": "absent", "results": []}
    record = backup_path()
    entries = _load_entries(record)
    results = [_patch_one(path, record, entries, capture) for path in paths]
    patched = [item for item in results if item["state"] == "patched"]
    failed = [item for item in results if not item["ok"]]
    state = "patched" if patched else ("already" if not failed else failed[0]["state"])
    return {"ok": not failed, "state": state, "results": results, "backup": str(record),
            "elevated_for": [item["shortcut"] for item in failed] if failed else []}


def restore(*, capture: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Put back exactly the arguments we found before the first patch."""
    record = backup_path()
    entries = _load_entries(record)
    if not entries:
        return {"ok": True, "state": "nothing-to-restore"}
    results: list[dict[str, Any]] = []
    for raw_path, stored in list(entries.items()):
        shortcut = Path(raw_path)
        original = str(stored.get("arguments") or "")
        if not shortcut.exists():
            results.append({"shortcut": raw_path, "ok": True, "state": "gone"})
            del entries[raw_path]
            continue
        written = _run(WRITE_SCRIPT.format(path=quote(raw_path), args=quote(original)), capture)
        after = read(shortcut, capture=capture)
        exact = not written.get("ERR") and after.get("arguments", "") == original
        if exact:
            del entries[raw_path]
            (record.parent / lnk_backup_name(shortcut)).unlink(missing_ok=True)
        results.append({"shortcut": raw_path, "ok": exact,
                        "state": "restored" if exact else "differs",
                        "expected": original, "actual": after.get("arguments", "")})
    if entries:
        _save_entries(record, entries)
    else:
        record.unlink(missing_ok=True)
    return {"ok": all(item["ok"] for item in results), "state": "restored", "results": results}


def inspect(shortcut: Path | None = None, *, capture: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    """Reported by ``--status``: whether a plain double-click would carry the port."""
    paths = [shortcut] if shortcut is not None else candidates()
    if not paths:
        return {"state": "absent", "shortcut": str(default_shortcut()), "entries": []}
    entries: list[dict[str, Any]] = []
    for path in paths:
        current = read(path, capture=capture)
        entries.append({
            "scope": scope_of(path),
            "shortcut": str(path),
            "state": current["state"],
            "arguments": current.get("arguments", ""),
        })
    usable = [item for item in entries if item["state"] == "ok"]
    ready = [item for item in usable if has_all(item["arguments"])]
    if usable and len(ready) == len(usable):
        state = "ready"
    elif ready:
        state = "partially-ready"
    elif usable:
        state = "missing-args"
    elif all(item["state"] == "absent" for item in entries):
        state = "absent"
    else:
        state = "unreadable"
    return {
        "state": state,
        "shortcut": str(paths[0]),
        "entries": entries,
        "ready": [item["shortcut"] for item in ready],
        "missing": [item["shortcut"] for item in entries if item.get("state") == "ok"
                    and not has_all(item["arguments"])],
        "backed_up": backup_path().exists(),
    }
