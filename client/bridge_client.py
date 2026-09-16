#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone client for the Abaqus HTTP Bridge.

No third-party dependencies (stdlib urllib only), so it runs anywhere: PyCharm,
WSL, a CI runner, a plain cmd.exe.

As a library::

    from bridge_client import AbaqusBridge, AbaqusKernelError

    ab = AbaqusBridge()                  # or AbaqusBridge.from_state_file()
    print(ab.status()["models"])         # ['Model-1']

    value = ab.value("from abaqus import mdb\\nresult = sorted(mdb.models.keys())")
    print(value)                         # ['Model-1']

    ab.value("mdb.models['Model-1'].parts['P'].generateMesh()", timeout=300)

As a command line::

    python bridge_client.py status
    python bridge_client.py endpoint
    python bridge_client.py exec "print(mdb.models.keys())"
    python bridge_client.py run my_script.py
    python bridge_client.py stop

Exit codes: 0 success, 1 kernel/transport error, 2 bad usage.
"""
from __future__ import print_function

import argparse
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "127.0.0.1"
#: Mirrors the server-side default in abaqus_http_bridge.py. Only reached when
#: there is no state file at all, so the two cannot disagree in practice.
DEFAULT_PORT = 49321
DEFAULT_TIMEOUT = 60.0
_STATE_SUBDIR = ".abaqus-http-bridge"


def state_file_candidates():
    """Places the bridge's state file may live, best first.

    On WSL this matters a lot: Abaqus runs on Windows and writes its state into
    the *Windows* profile (C:\\Users\\<you>\\.abaqus-http-bridge), while ``~``
    in WSL is /home/<you>. Looking only at the Linux home makes the client fail
    with "no state file" even though the bridge is running fine.
    """
    out = []
    env = os.environ.get("ABAQUS_HTTP_STATE_DIR")
    if env:
        out.append(env)
    if sys.platform == "linux" and os.path.isdir("/mnt/c/Users"):
        try:
            for name in sorted(os.listdir("/mnt/c/Users")):
                base = os.path.join("/mnt/c/Users", name)
                if os.path.isdir(os.path.join(base, _STATE_SUBDIR)):
                    out.append(os.path.join(base, _STATE_SUBDIR))
        except Exception:
            pass
    out.append(os.path.join(os.path.expanduser("~"), _STATE_SUBDIR))
    return out


def find_state_file():
    """First existing state file, or the primary candidate if none exists yet."""
    candidates = state_file_candidates()      # scanned once: the WSL branch lists a directory
    for d in candidates:
        path = os.path.join(d, "bridge.json")
        if os.path.isfile(path):
            return path
    return os.path.join(candidates[-1], "bridge.json")


class AbaqusBridgeError(Exception):
    """Transport-level failure: could not reach the bridge at all."""


class AbaqusKernelError(Exception):
    """The bridge was reached, but the kernel raised while running the code.

    Carries the diagnostics the bridge sends back, so a caller can print them.
    """

    def __init__(self, message, error_type=None, core_error=None,
                 recovery=None, code_excerpt=None, traceback_tail=None):
        Exception.__init__(self, message)
        self.error_type = error_type
        self.core_error = core_error
        self.recovery = recovery
        self.code_excerpt = code_excerpt
        self.traceback_tail = traceback_tail

    def report(self):
        """A readable, multi-line diagnosis."""
        lines = ["%s: %s" % (self.error_type or "Error", self.core_error or "unknown error")]
        if self.recovery:
            if self.recovery.get("missing_key") is not None:
                lines.append("  missing key: %r" % (self.recovery["missing_key"],))
            if self.recovery.get("parent_object_path"):
                lines.append("  object     : %s" % self.recovery["parent_object_path"])
            if self.recovery.get("possible_keys"):
                lines.append("  existing   : %s" % (self.recovery["possible_keys"],))
        if self.code_excerpt:
            lines.append("  code:\n%s" % self.code_excerpt)
        if self.traceback_tail:
            lines.append("  traceback tail:\n%s" % self.traceback_tail)
        return "\n".join(lines)


class AbaqusBridge(object):
    """Talk to the HTTP bridge running inside Abaqus/CAE."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, token="",
                 timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = int(port)
        self.token = token or ""
        self.timeout = float(timeout)

    # -- construction helpers ---------------------------------------------

    @classmethod
    def from_state_file(cls, path=None, timeout=DEFAULT_TIMEOUT):
        """Build a client from the bridge's published state file.

        The bridge may have fallen back to a different port if the primary one
        was taken, so this is the reliable way to find it.
        """
        path = path or find_state_file()
        try:
            with open(path, "r") as fh:
                state = json.load(fh)
        except Exception as exc:
            raise AbaqusBridgeError(
                "cannot read the bridge state file %s (%s)\n"
                "  looked in: %s\n"
                "  Is the bridge started? Open it with"
                " Plug-ins > Abaqus HTTP Bridge > Start Bridge,"
                "\n  or pass --port explicitly."
                % (path, exc, state_file_candidates()))
        return cls(host=state.get("host", DEFAULT_HOST),
                   port=state.get("port", DEFAULT_PORT),
                   token=state.get("token", "") or "",
                   timeout=timeout)

    def base_url(self):
        return "http://%s:%d" % (self.host, self.port)

    # -- transport ---------------------------------------------------------

    def _request(self, method, path, body=None, timeout=None):
        url = self.base_url() + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("X-Bridge-Token", self.token)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8")
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, {"ok": False, "error": raw or str(exc)}
        except urllib.error.URLError as exc:
            raise AbaqusBridgeError(
                "cannot reach the Abaqus HTTP bridge at %s (%s).\n"
                "  - is Abaqus/CAE running?\n"
                "  - is the bridge started? (Plug-ins > Abaqus HTTP Bridge > Start Bridge,\n"
                "    or ABAQUS_HTTP_AUTOSTART=1 with abaqus cae noGUI=bootstrap.py)\n"
                "  - the port is CLOSED by default."
                % (self.base_url(), getattr(exc, "reason", exc)))
        except Exception as exc:
            raise AbaqusBridgeError("bridge request failed (%s: %s)"
                                    % (type(exc).__name__, exc))

    # -- API ---------------------------------------------------------------

    def health(self):
        """Liveness probe. Touches no Abaqus object, so it answers even when the
        session is busy."""
        # Capped at 5 s: a liveness probe that can hang for the full --timeout is
        # not a liveness probe. A shorter --timeout still wins.
        status, body = self._request(
            "GET", "/health", timeout=min(float(self.timeout), 5.0))
        if status != 200 or not body.get("ok"):
            raise AbaqusBridgeError("health failed: HTTP %s %s" % (status, body))
        return body

    def status(self):
        """Session facts: python, pid, cwd, models, viewports, jobs, bridge mode."""
        status, body = self._request("GET", "/status")
        if status != 200:
            raise AbaqusBridgeError("status failed: HTTP %s %s" % (status, body))
        return body

    def execute(self, code, timeout=None):
        """Run `code` in the live kernel and return the full result dict.

        Raises AbaqusKernelError if the kernel raised (the result carries the
        diagnostics), AbaqusBridgeError if the bridge was unreachable.
        """
        if not code or not str(code).strip():
            raise ValueError("code must not be empty")
        secs = float(timeout or self.timeout)
        status, body = self._request(
            "POST", "/execute",
            {"code": str(code), "timeout": secs},
            timeout=secs + 15.0)
        if status == 400:
            raise ValueError("bridge rejected the request: %s" % body.get("error"))
        if status != 200 or not body.get("ok"):
            raise AbaqusBridgeError("execute failed: HTTP %s %s" % (status, body))
        result = body.get("result") or {}
        if not result.get("ok"):
            raise AbaqusKernelError(
                "%s: %s" % (result.get("error_type"), result.get("core_error")),
                error_type=result.get("error_type"),
                core_error=result.get("core_error"),
                recovery=result.get("recovery"),
                code_excerpt=result.get("code_excerpt"),
                traceback_tail=result.get("traceback_tail"))
        return result

    def value(self, code, timeout=None):
        """Run `code` and return just the value of its `result` variable.

        ``None`` therefore means "the code set nothing, or set it to None". Use
        ``execute()`` and its ``has_result`` flag when the difference matters.
        """
        return self.execute(code, timeout=timeout).get("return_value")

    def stop(self):
        """Ask the bridge to close its port."""
        status, body = self._request("POST", "/stop", {}, timeout=10.0)
        if status != 200:
            raise AbaqusBridgeError("stop failed: HTTP %s %s" % (status, body))
        return body

    def wait_until_up(self, seconds=30.0, interval=0.5):
        """Poll until the bridge answers, or raise after `seconds`."""
        deadline = time.time() + float(seconds)
        last = None
        while time.time() < deadline:
            try:
                return self.health()
            except AbaqusBridgeError as exc:
                last = exc
                time.sleep(interval)
        raise AbaqusBridgeError("bridge did not come up within %ss (%s)" % (seconds, last))


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _read_script(path):
    """Read a script file for the ``run`` subcommand.

    utf-8 explicitly: the platform default on Windows is the ANSI code page, so
    a UTF-8 script containing non-ASCII text would otherwise fail to decode.
    """
    try:
        with io.open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except UnicodeDecodeError as exc:
        raise ValueError("cannot read %s as UTF-8 (%s); re-save the script as UTF-8"
                         % (path, exc))


def _make_client(args):
    """Build a client, with explicitly passed flags winning over the state file.

    ``--host``/``--token`` used to be dropped unless ``--port`` was also given,
    because the state-file branch threw them away. They are overrides now, which
    is what a caller who passes them expects.
    """
    if args.port:
        host, port, token = args.host or DEFAULT_HOST, args.port, args.token or ""
    else:
        discovered = AbaqusBridge.from_state_file(timeout=args.timeout)
        host, port, token = discovered.host, discovered.port, discovered.token
    if args.host:
        host = args.host
    if args.token is not None:
        token = args.token
    return AbaqusBridge(host=host, port=port, token=token, timeout=args.timeout)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Client for the Abaqus HTTP Bridge (stdlib only).")
    ap.add_argument("--host", default=None,
                    help="default: the host published in the state file")
    ap.add_argument("--port", type=int, default=None,
                    help="default: read the port from the bridge state file")
    ap.add_argument("--token", default=None,
                    help="default: the token published in the state file")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("health", help="liveness probe that touches no Abaqus object")
    sub.add_parser("status", help="print the session's models/jobs/viewports")
    sub.add_parser("endpoint", help="print the bridge base URL")
    sub.add_parser("stop", help="ask the bridge to close its port")

    p_exec = sub.add_parser("exec", help="run a Python snippet in the kernel")
    p_exec.add_argument("code")

    p_run = sub.add_parser("run", help="run a Python file in the kernel")
    p_run.add_argument("path")

    args = ap.parse_args(argv)
    if not args.cmd:
        ap.print_help()
        return 2

    try:
        ab = _make_client(args)
    except AbaqusBridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        if args.cmd == "endpoint":
            print(ab.base_url())
            return 0
        if args.cmd == "health":
            print(json.dumps(ab.health(), indent=2))
            return 0
        if args.cmd == "status":
            info = ab.status()
            print("endpoint : %s" % ab.base_url())
            bridge = info.get("bridge") or {}
            print("pid      : %s" % info.get("pid"))
            print("mode     : %s" % bridge.get("mode"))
            print("cwd      : %s" % info.get("cwd"))
            print("models   : %s" % (info.get("models") or []))
            print("viewports: %s" % (info.get("viewports") or []))
            print("jobs     : %s" % (info.get("jobs") or []))
            return 0
        if args.cmd == "stop":
            ab.stop()
            print("stop requested")
            return 0

        code = args.code if args.cmd == "exec" else _read_script(args.path)
        result = ab.execute(code, timeout=args.timeout)
        if result.get("stdout"):
            print(result["stdout"], end="" if result["stdout"].endswith("\n") else "\n")
        value = result.get("return_value")
        if value is not None:
            print(json.dumps(value, indent=2, default=str, ensure_ascii=False))
        return 0

    except AbaqusKernelError as exc:
        print("Abaqus kernel error:\n%s" % exc.report(), file=sys.stderr)
        return 1
    except AbaqusBridgeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        # The bridge answered 400, or a script could not be decoded: a usage /
        # input problem, not a transport failure. Exit 2 as documented instead of
        # letting the exception print a traceback.
        print(str(exc), file=sys.stderr)
        return 2
    except (IOError, OSError) as exc:
        print("cannot read %s (%s)" % (getattr(args, "path", "?"), exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
