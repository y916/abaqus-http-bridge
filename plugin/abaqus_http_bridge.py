# -*- coding: utf-8 -*-
"""abaqus_http_bridge -- HTTP bridge that drives a live Abaqus/CAE kernel.

Runs *inside* Abaqus/CAE and exposes the CAE kernel over plain HTTP on
loopback, so an external agent (e.g. the DSH `dsh-cae-agent` Cordis plugin)
can execute Abaqus Python, query models, and submit jobs.

Design notes
------------
* **Kernel-safe.** This module never imports ``abaqusGui`` at module top, so it
  can be loaded from a ``script=``/``startup=`` bootstrap *and* from the GUI
  menu plugin. ``abaqusGui`` raises ``ImportError: Module abaqusGui can only be
  used in Abaqus/CAE GUI`` from the kernel engine, which is exactly why a
  GUI-only plugin can never auto-start a bridge.

* **HTTP, not raw sockets.** stdlib ``http.server`` only (Abaqus 2024 ships
  Python 3.10 and its site-packages are curated -- no third-party deps here).
  Requests are JSON; responses are JSON. This survives proxies/inspection and
  keeps a per-request envelope instead of a hand-rolled framing protocol.

* **Serialized execution.** CAE kernel state (``mdb``/``session``) is not
  thread-safe. Every ``/execute`` takes a module-level lock, so concurrent
  clients queue instead of interleaving kernel calls.

* **Persistent namespace.** Executed code shares one namespace that survives
  across requests, so multi-step workflows can keep Python locals between
  calls (the file-IPC design in Cai-aa/abaqus-mcp cannot do this).

* **Loopback + token.** Binds ``127.0.0.1`` only. An optional shared token
  (``ABAQUS_HTTP_TOKEN``) is required in the ``X-Bridge-Token`` header.

Wire contract. Every response is JSON; the ``/execute`` payload is nested under
``result`` (the flat shape belonged to the old socket bridge)::

    GET  /health   -> {"ok": true, "version": "...", "transport": "http",
                       "port": 49321, "pid": 27420, "thread": "MainThread",
                       "touches_abaqus": false}
                      Liveness probe. Touches no Abaqus object, so it answers
                      even while a kernel call is running. Together with
                      /ready it is the ONE route that does not need the token.

    GET  /status   -> {"python": ..., "executable": ..., "platform": ...,
                       "pid": ..., "cpu_count": ..., "cwd": ...,
                       "abaqus_version": ..., "models": [...],
                       "viewports": [...], "jobs": [...],
                       "bridge": {"version", "transport", "host", "port",
                                  "running", "processed", "uptime_seconds",
                                  "requires_token", "mode", "log"}}
                      (/ping is an alias.) Requires the token.

    POST /execute  {"code": "...", "timeout": 60}
      -> {"ok": true, "id": <echoed from the request>, "result": {
              "ok": true, "return_value": ..., "has_result": true,
              "stdout": "...", "stderr": "...",
              "error_type": "None", "core_error": "None"}}
      -> kernel raised: HTTP 200, "result": {
              "ok": false, "return_value": null, "has_result": false,
              "stdout": "...", "stderr": "...",
              "error_type": "builtins.KeyError", "core_error": "'NoSuchModel'",
              "recovery": {...}, "code_excerpt": "...",
              "traceback_tail": "..."}
      -> bad body: HTTP 400 {"ok": false, "error": "..."}

    POST /stop     -> {"ok": true, "result": {"success": true,
                                             "message": "stop requested"}}

``has_result`` reports whether the executed code set the ``result`` variable.
The namespace is persistent, so ``result`` is cleared before every execution --
without that, a request that sets nothing would silently return the previous
request's value.

Public API (callable from the CAE console or a GUI menu)::

    start_bridge() / mcp_start()   start the HTTP listener
    stop_bridge()  / mcp_stop()    stop it
    bridge_status()/ mcp_status()  status dict
"""

from __future__ import absolute_import, print_function

import ast
import io
import json
import os
import platform
import re
import select
import socket
import sys
import threading
import time
import traceback as _traceback

__version__ = "1.3.5"

#: Filename stamped on every snippet we compile. ``_excerpt``/``_offending_line``
#: match on it, so it lives in exactly one place.
_EXEC_FILENAME = "<abaqus_http_bridge>"

#: Hard cap for bridge.log. One rotated copy is kept as ``bridge.log.1``.
LOG_MAX_BYTES = 5 * 1024 * 1024

# --------------------------------------------------------------------------
# Configuration
#
# Values come from, in increasing precedence:
#   1. the defaults below
#   2. bridge_config.json  (next to this file, or in the state dir, or wherever
#      ABAQUS_HTTP_CONFIG points)
#   3. environment variables
#
# The config file is plain JSON and ships with the plugin, so it sits right next
# to the code you would edit to change behaviour.
# --------------------------------------------------------------------------

CONFIG_FILENAME = "bridge_config.json"

DEFAULT_CONFIG = {
    # Kept field-for-field in sync with the shipped bridge_config.json (see the
    # note in the README): the file is the copy a user edits, this dict is the
    # fallback when no file is found at all. Change one, change the other.
    "_comment": [
        "Abaqus HTTP Bridge configuration.",
        "Restart Abaqus/CAE after editing.",
        "enabled: true opens the port automatically at CAE startup (GUI plugin and bootstrap).",
        "token:   when non-empty, every request except /health and /ready must send X-Bridge-Token.",
        "Environment variables override this file.",
    ],
    "enabled": False,
    "host": "127.0.0.1",
    "port": 49321,
    "port_candidates": [8791, 18152, 33001, 34001, 49321, 51234, 56789, 52345, 60001, 65000],
    "token": "",
    "allow_port_fallback": True,
    "timeout": 120,
    "max_body_bytes": 8388608,
    "log_requests": True,
    "state_dir": "",
}

#: Keys a config file may set (anything else is ignored, and "_*" is reserved
#: for comments so a hand-edited file can explain itself).
_CONFIG_KEYS = tuple(k for k in DEFAULT_CONFIG if not k.startswith("_"))


def _home():
    try:
        return os.path.expanduser("~")
    except Exception:
        return os.getcwd()


def _here():
    """Directory holding this file, or None when Abaqus gives us no __file__."""
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return None


def config_search_paths():
    """Where bridge_config.json is looked for, first match wins."""
    out = []
    env = os.environ.get("ABAQUS_HTTP_CONFIG", "").strip()
    if env:
        out.append(env)
    here = _here()
    if here:
        out.append(os.path.join(here, CONFIG_FILENAME))
    out.append(os.path.join(_home(), ".abaqus-http-bridge", CONFIG_FILENAME))
    return out


def load_config():
    """Merge: defaults <- config file <- environment variables."""
    cfg = dict(DEFAULT_CONFIG)
    source = None
    for path in config_search_paths():
        try:
            with io.open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        if isinstance(data, dict):
            for key in _CONFIG_KEYS:
                if key in data:
                    cfg[key] = data[key]
            source = path
            break
    cfg["_source"] = source

    env = os.environ
    if env.get("ABAQUS_HTTP_HOST"):
        cfg["host"] = env["ABAQUS_HTTP_HOST"].strip()
    if env.get("ABAQUS_HTTP_PORT"):
        try:
            cfg["port"] = int(env["ABAQUS_HTTP_PORT"])
        except ValueError:
            pass
    # An *empty* ABAQUS_HTTP_TOKEN deliberately clears a token that the config
    # file set: "environment overrides the file" has to hold in both directions,
    # otherwise there is no way to turn the token off without editing the file.
    if env.get("ABAQUS_HTTP_TOKEN") is not None:
        cfg["token"] = env["ABAQUS_HTTP_TOKEN"].strip()
    if env.get("ABAQUS_HTTP_TIMEOUT"):
        try:
            cfg["timeout"] = float(env["ABAQUS_HTTP_TIMEOUT"])
        except ValueError:
            pass
    if env.get("ABAQUS_HTTP_STATE_DIR"):
        cfg["state_dir"] = env["ABAQUS_HTTP_STATE_DIR"].strip()
    if env.get("ABAQUS_HTTP_AUTOSTART") is not None and env.get("ABAQUS_HTTP_AUTOSTART", "") != "":
        cfg["enabled"] = env["ABAQUS_HTTP_AUTOSTART"].strip() not in ("0", "false", "False")
    if env.get("ABAQUS_HTTP_LOG_REQUESTS", "") != "":
        cfg["log_requests"] = env["ABAQUS_HTTP_LOG_REQUESTS"].strip() not in ("0", "false", "False")
    return cfg


CONFIG = load_config()

HOST = str(CONFIG.get("host") or "127.0.0.1").strip() or "127.0.0.1"

#: Primary port, then the candidates tried in order. 48152 (the upstream
#: default) is deliberately absent: on Windows/Hyper-V hosts it frequently lands
#: inside a *reserved* port range and ``bind`` fails with ``WinError 10013``.
DEFAULT_PORT = int(CONFIG.get("port") or 49321)
PORT_CANDIDATES = []
for _p in [DEFAULT_PORT] + [int(x) for x in (CONFIG.get("port_candidates") or [])]:
    if _p not in PORT_CANDIDATES:
        PORT_CANDIDATES.append(_p)

ALLOW_PORT_FALLBACK = bool(CONFIG.get("allow_port_fallback", True))
TOKEN = str(CONFIG.get("token") or "").strip()
DEFAULT_TIMEOUT = float(CONFIG.get("timeout") or 120)
MAX_BODY_BYTES = int(CONFIG.get("max_body_bytes") or 8 * 1024 * 1024)
LOG_REQUESTS = bool(CONFIG.get("log_requests", True))

#: Where the chosen host/port/token are published so an external client can
#: discover the bridge without guessing (the port may have fallen back).
STATE_DIR = str(CONFIG.get("state_dir") or "").strip() or os.path.join(
    _home(), ".abaqus-http-bridge")
STATE_FILE = os.path.join(STATE_DIR, "bridge.json")
LOG_FILE = os.path.join(STATE_DIR, "bridge.log")

# --------------------------------------------------------------------------
# Module state
# --------------------------------------------------------------------------

_SERVER = None
_STOP_REQUESTED = False
_BOUND_PORT = None
_START_TIME = 0.0
_PROCESSED = 0
_EXEC_LOCK = threading.RLock()          # serializes kernel access
_KERNEL_NAMESPACE = {"__name__": "__abaqus_http_exec__"}
_LAST_ERROR = None
_MODE = "not-started"
#: PID of the process owning an *adopted* bridge, i.e. one that was already
#: serving when this session asked to start. Nothing is served here in that
#: case, but /status and the GUI menu must say so instead of looking like a
#: start that silently did nothing.
_ADOPTED_PID = None


def _ensure_state_dir():
    try:
        if not os.path.isdir(STATE_DIR):
            os.makedirs(STATE_DIR)
    except Exception:
        pass


def _rotate_log_if_needed():
    """Keep ``bridge.log`` bounded: one rotated copy, then start over.

    A bridge left running for weeks logs a line per request, so without a cap
    the file grows forever. Best-effort: any failure just means we keep
    appending to the current file.
    """
    try:
        if os.path.getsize(LOG_FILE) < LOG_MAX_BYTES:
            return
        rotated = LOG_FILE + ".1"
        try:
            if os.path.exists(rotated):
                os.remove(rotated)
        except Exception:
            pass
        os.replace(LOG_FILE, rotated)
    except Exception:
        pass


def _log(message):
    """Best-effort append to the bridge log; never raises."""
    try:
        _ensure_state_dir()
        _rotate_log_if_needed()
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with io.open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(u"[%s] %s\n" % (stamp, message))
    except Exception:
        pass


def _announce(message):
    print("abaqus-http-bridge: " + str(message))
    _log(message)


def _publish_state(running):
    # NOTE: the token is published in cleartext on purpose -- the bundled client
    # reads it back from here so a caller never has to be told the secret. That
    # makes bridge.json a credential; see the security section of the README.
    payload = {
        "version": __version__,
        "transport": "http",
        "host": HOST,
        "port": _BOUND_PORT,
        "token": TOKEN,
        "running": bool(running),
        "mode": _MODE,
        "pid": os.getpid(),
        "python": sys.version,
        "started_at": _START_TIME,
        "updated_at": time.time(),
        "log": LOG_FILE,
    }
    try:
        _ensure_state_dir()
        tmp = STATE_FILE + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        try:
            os.replace(tmp, STATE_FILE)
        except Exception:
            with io.open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
    except Exception as exc:
        _log("state publish failed: %s" % exc)


# --------------------------------------------------------------------------
# Kernel operations
# --------------------------------------------------------------------------

def _kernel_ping():
    """Session facts for ``GET /status``.

    It touches ``mdb``/``session``, so it is **not** thread-safe and must only
    run on the serving thread -- which is what happens: the server is
    single-threaded and handles one request at a time. It deliberately does not
    take ``_EXEC_LOCK`` (that lock exists to serialise ``/execute`` calls against
    each other); a probe can therefore never be the thing that blocks a kernel
    command.
    """
    info = {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "pid": os.getpid(),
        "cpu_count": os.cpu_count(),
        "cwd": os.getcwd(),
    }
    try:
        from abaqus import mdb, session
        info["abaqus_version"] = str(getattr(session, "version", None))
        info["models"] = list(mdb.models.keys())
        try:
            info["viewports"] = list(session.viewports.keys())
        except Exception:
            info["viewports"] = []
        try:
            info["jobs"] = list(mdb.jobs.keys())
        except Exception:
            info["jobs"] = []
    except Exception as exc:
        info["abaqus_error"] = str(exc)
    return info


def _kernel_execute(code, timeout=None):
    """Execute ``code`` in the shared, persistent kernel namespace.

    ``timeout`` is accepted because it is part of the wire request, but it is
    deliberately ignored: the bridge never aborts a call that has already
    started, so there is nothing to enforce. A long command is stopped the same
    way a hand-typed one is -- Ctrl+C inside Abaqus.

    Returns the ``result`` member of the response envelope (see the wire
    contract in the module docstring).
    """
    if not isinstance(code, str) or not code.strip():
        # Guard for direct callers (selftest_http.py). The HTTP route validates
        # the body first and answers 400 without ever reaching this line.
        raise ValueError("params.code must be a non-empty string")

    from contextlib import redirect_stderr, redirect_stdout

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    namespace = _KERNEL_NAMESPACE

    try:
        from abaqus import mdb, session
        namespace.setdefault("mdb", mdb)
        namespace.setdefault("session", session)
    except Exception:
        pass

    with _EXEC_LOCK:
        # The namespace survives across requests, so ``result`` has to be
        # dropped before every run: otherwise a request that sets nothing would
        # report the *previous* request's value, i.e. silent bad data instead of
        # a visible failure. ``has_result`` tells the caller which happened.
        namespace.pop("result", None)
        codeobj = None
        try:
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                codeobj, returned = _run_user_code(code, namespace)

            return {
                "ok": True,
                "return_value": returned,
                "has_result": "result" in namespace,
                "stdout": stdout_buf.getvalue(),
                "stderr": stderr_buf.getvalue(),
                "error_type": "None",
                "core_error": "None",
            }
        except KeyboardInterrupt:
            # Never swallow Ctrl+C: it is the documented way to interrupt a long
            # kernel command and must keep propagating into Abaqus.
            raise
        except BaseException as exc:  # noqa: BLE001
            # BaseException rather than Exception on purpose: ``sys.exit()`` and
            # ``raise SystemExit`` are BaseExceptions, and letting one escape
            # here used to kill the serving pump for the rest of the CAE session
            # (and, in blocking mode, tear down the whole pump loop).
            global _LAST_ERROR
            _LAST_ERROR = str(exc)
            return {
                "ok": False,
                "return_value": None,
                "has_result": False,
                "stdout": stdout_buf.getvalue(),
                "stderr": stderr_buf.getvalue(),
                "error_type": "%s.%s" % (type(exc).__module__, type(exc).__name__),
                "core_error": str(exc),
                "recovery": _recover(code, exc, codeobj),
                "code_excerpt": _excerpt(code, exc, codeobj),
                "traceback_tail": _traceback.format_exc(),
            }


def _run_user_code(code, namespace):
    """Compile and run ``code``; returns ``(code_object, return_value)``.

    A lone expression is evaluated and its value becomes ``result``; anything
    else runs as a statement list and ``result`` is read back from the
    namespace. Both paths compile with the SAME filename so that diagnostics
    can recognise the frame this call just created.
    """
    try:
        parsed = ast.parse(code, mode="exec")
        simple = len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Expr)
    except Exception:
        simple = False

    if simple:
        # Compiled explicitly instead of calling eval(code, ...) directly: that
        # would label the code "<string>" and _excerpt() would then have no line
        # to point at, which is exactly the case an agent hits most often.
        codeobj = compile(code, _EXEC_FILENAME, "eval")
        value = eval(codeobj, namespace)  # noqa: S307 - caller-supplied by design
        namespace["result"] = value
        return codeobj, value

    codeobj = compile(code, _EXEC_FILENAME, "exec")
    exec(codeobj, namespace)  # noqa: S102 - caller-supplied by design
    return codeobj, namespace.get("result")


#: Stores worth a "did you mean" hint, most specific first.
_RECOVER_STORES = ("parts", "materials", "steps", "jobs")


def _recover(code, exc, codeobj=None):
    """Map a common Abaqus 'KeyError'-style failure to a similar-keys hint.

    The store is guessed from the *offending line* when the traceback can name
    one, and from the whole snippet otherwise. Matching is word-bounded, so a
    local called ``my_parts_cache`` no longer looks like a ``parts`` lookup.
    """
    try:
        if not isinstance(exc, KeyError):
            return None
        missing = exc.args[0] if exc.args else None
        haystack = _offending_line(code, codeobj) or code
        store = None
        for candidate in _RECOVER_STORES:
            if re.search(r"\b%s\b" % candidate, haystack):
                store = candidate
                break
        if store == "jobs":
            from abaqus import mdb
            keys = list(mdb.jobs.keys())
        elif store:
            from abaqus import mdb
            keys = []
            for name in mdb.models.keys():
                obj = getattr(mdb.models[name], store, None)
                if obj is not None:
                    keys.extend(list(obj.keys()))
        else:
            keys = []
        return {
            "parent_object_path": store,
            "possible_keys": keys[:20],
            "callable_signature": None,
            "missing_key": missing,
        }
    except Exception:
        return None


def _offending_lineno(codeobj=None):
    """Line number of the innermost frame belonging to the code we just ran.

    Matching the exact code object matters: a function defined by an *earlier*
    request was compiled with the same filename, and its line numbers refer to
    that older source. A filename-only match would happily print the wrong
    lines. When the error happened inside such a function we return None and the
    caller shows no excerpt, which beats showing a misleading one.

    Must be called from inside the ``except`` block -- it reads
    ``sys.exc_info()``.
    """
    try:
        tb = sys.exc_info()[2]
        lineno = None
        while tb is not None:
            frame_code = tb.tb_frame.f_code
            if codeobj is not None:
                if frame_code is codeobj:
                    lineno = tb.tb_lineno
            elif frame_code.co_filename == _EXEC_FILENAME:
                lineno = tb.tb_lineno
            tb = tb.tb_next
        return lineno
    except Exception:
        return None


def _offending_line(code, codeobj=None):
    """The source line the traceback points at, or None."""
    lineno = _offending_lineno(codeobj)
    if lineno is None:
        return None
    try:
        lines = code.splitlines()
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1]
    except Exception:
        pass
    return None


def _excerpt(code, exc, codeobj=None):
    """Return the offending source line, when the traceback points at one."""
    try:
        lineno = _offending_lineno(codeobj)
        if lineno is None:
            return None
        lines = code.splitlines()
        lo = max(0, lineno - 3)
        hi = min(len(lines), lineno + 2)
        return "\n".join(
            ("%s%4d| %s" % (">>" if i + 1 == lineno else "  ", i + 1, lines[i]))
            for i in range(lo, hi)
        )
    except Exception:
        return None


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

def _make_handler():
    from http.server import BaseHTTPRequestHandler

    class BridgeHandler(BaseHTTPRequestHandler):
        server_version = "AbaqusHTTPBridge/" + __version__
        protocol_version = "HTTP/1.1"

        # -- helpers -----------------------------------------------------
        def _send_json(self, status, payload):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # One request per connection. The pump loop calls handle_request()
            # in slices; leaving a keep-alive connection open would park the
            # single-threaded server inside this handler instead of returning to
            # processUpdates().
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def _authorized(self):
            if not TOKEN:
                return True
            return self.headers.get("X-Bridge-Token", "") == TOKEN

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                raise ValueError("request body exceeds %d bytes" % MAX_BODY_BYTES)
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise ValueError("invalid JSON body: %s" % exc)

        def log_message(self, fmt, *args):
            if not LOG_REQUESTS:
                return
            _log("%s %s" % (self.address_string(), fmt % args))

        # -- routes ------------------------------------------------------
        def do_GET(self):
            global _PROCESSED
            path = self.path.split("?")[0].rstrip("/") or "/"

            # Liveness probe that touches NO Abaqus object. Kept separate from
            # /status on purpose: if /health answers but /status hangs, the
            # HTTP/threading layer is fine and the hang is inside a kernel call.
            #
            # /health and /ready are also the only routes that skip the token
            # check. That is deliberate -- a health check should not need a
            # credential -- and it is stated in the README.
            if path in ("/health", "/ready"):
                self._send_json(200, {
                    "ok": True,
                    "version": __version__,
                    "transport": "http",
                    "port": _BOUND_PORT,
                    "pid": os.getpid(),
                    "thread": threading.current_thread().name,
                    "touches_abaqus": False,
                })
                return

            if path in ("/status", "/ping"):
                if not self._authorized():
                    self._send_json(401, {"ok": False, "error": "bad or missing X-Bridge-Token"})
                    return
                result = _kernel_ping()
                result["bridge"] = {
                    "version": __version__,
                    "transport": "http",
                    "host": HOST,
                    "port": _BOUND_PORT,
                    "running": _SERVER is not None,
                    "processed": _PROCESSED,
                    "uptime_seconds": int(time.time() - _START_TIME) if _START_TIME else 0,
                    "requires_token": bool(TOKEN),
                    "mode": _MODE,
                    "log": LOG_FILE,
                }
                _PROCESSED += 1
                self._send_json(200, result)
                return
            self._send_json(404, {"ok": False, "error": "no route %s" % path})

        def do_POST(self):
            global _PROCESSED
            path = self.path.split("?")[0].rstrip("/") or "/"
            if not self._authorized():
                self._send_json(401, {"ok": False, "error": "bad or missing X-Bridge-Token"})
                return
            try:
                params = self._read_json()
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return

            try:
                if path == "/execute":
                    code = params.get("code")
                    if not isinstance(code, str) or not code.strip():
                        # A client mistake, not a kernel failure: answer 400 and
                        # never leak an internal traceback for it.
                        self._send_json(400, {
                            "ok": False,
                            "error": "body must be {\"code\": \"<non-empty string>\", \"timeout\": <seconds>}",
                        })
                        return
                    if params.get("timeout") in (None, ""):
                        timeout = DEFAULT_TIMEOUT
                    else:
                        try:
                            timeout = float(params["timeout"])
                        except (TypeError, ValueError):
                            # A malformed field is a client mistake, so answer
                            # 400 instead of leaking a 500 + traceback for it.
                            self._send_json(400, {
                                "ok": False,
                                "error": "\"timeout\" must be a number of seconds",
                            })
                            return
                    result = _kernel_execute(code, timeout)
                    _PROCESSED += 1
                    self._send_json(200, {"ok": True, "id": params.get("id"), "result": result})
                    return
                if path == "/stop":
                    _PROCESSED += 1
                    self._send_json(200, {"ok": True, "result": {"success": True,
                                                                 "message": "stop requested"}})
                    # Answered; the pump loop sees the flag after handle_request
                    # returns and exits. Do not tear the server down from inside
                    # its own handler.
                    _request_stop()
                    return
            except Exception as exc:  # noqa: BLE001
                _log("route %s failed: %s" % (path, exc))
                self._send_json(500, {
                    "ok": False,
                    "error": {
                        "message": str(exc),
                        "type": "%s.%s" % (type(exc).__module__, type(exc).__name__),
                        "traceback": _traceback.format_exc(),
                    },
                })
                return
            self._send_json(404, {"ok": False, "error": "no route %s" % path})

    return BridgeHandler


def _port_available(host, port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def _state_says_running():
    """Local, instant read of our published state (no network)."""
    try:
        with io.open(STATE_FILE, "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("running"))
    except Exception:
        return False


def _existing_bridge_here(host, port, timeout=1.5):
    """Return the /status payload if a bridge already serves ``host:port``.

    Makes ``start_bridge`` idempotent across processes: a second CAE session
    (or a re-run of the bootstrap) adopts the running bridge instead of
    silently sliding to a different port, which would leave the client pointing
    at an endpoint nobody is serving.
    """
    import urllib.request
    url = "http://%s:%d/status" % (host, port)
    try:
        req = urllib.request.Request(url, method="GET")
        if TOKEN:
            req.add_header("X-Bridge-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        bridge = payload.get("bridge") or {}
        if bridge.get("transport") == "http":
            return payload
    except Exception:
        return None
    return None


def start_bridge(port=None, host=None, token=None, block=True, allow_fallback=True):
    """Start the HTTP bridge over the CAE kernel and pump it.

    The server is **driven from the calling thread**, not from a background
    thread, and that is not a style choice -- it is forced by how Abaqus/CAE
    schedules Python. Verified on Abaqus 2024: once a ``script=``/``startup=``
    script returns, the kernel's background threads stop being scheduled while
    the GUI session sits idle. A threaded ``serve_forever`` therefore accepts
    nothing: TCP connections land in the OS backlog (so a client sees the
    connection "established") but no Python handler ever runs, and every request
    times out. Serving from the calling thread -- which in a CAE script or a
    kernel menu callback IS the kernel main thread -- works in both GUI and
    noGUI modes.

    ``session.processUpdates()`` is called between request slices so the GUI
    keeps repainting and stays responsive for the whole session.

    Idempotent: if ``host:port`` already answers ``/status``, that bridge is
    adopted instead of silently sliding to another port.

    Blocks until ``POST /stop`` arrives (or ``stop_bridge()`` is called from the
    same thread). Pass ``block=False`` only where the caller keeps the kernel
    main thread busy itself.
    """
    global _SERVER, _BOUND_PORT, _START_TIME, TOKEN, HOST, _STOP_REQUESTED
    global _MODE, _ADOPTED_PID

    if _SERVER is not None:
        return "abaqus http bridge already listening on http://%s:%s" % (HOST, _BOUND_PORT)

    if host:
        HOST = host
    if token is not None:
        TOKEN = token

    primary = int(port) if port else DEFAULT_PORT

    # Only probe over HTTP when our own state file claims a bridge is up. A
    # blind probe costs a full connect timeout when nothing is listening, which
    # made Start Bridge take ~1.6 s before doing anything.
    existing = _existing_bridge_here(HOST, primary) if _state_says_running() else None
    if existing is not None:
        # Nothing is served from this process. Record *why*, so /status and the
        # GUI menu report "adopted by pid N" instead of looking like a start
        # that silently did nothing.
        _ADOPTED_PID = existing.get("pid")
        _MODE = "adopted (served by pid %s)" % _ADOPTED_PID
        _BOUND_PORT = primary
        message = ("adopted existing abaqus http bridge on http://%s:%s "
                   "(pid=%s) -- no new listener started" % (HOST, primary, _ADOPTED_PID))
        _announce(message)
        return message

    from http.server import HTTPServer

    candidates = [primary]
    if allow_fallback and ALLOW_PORT_FALLBACK:
        candidates.extend(p for p in PORT_CANDIDATES if p != primary)

    handler = _make_handler()
    last_error = None
    for candidate in candidates:
        try:
            server = HTTPServer((HOST, candidate), handler)
            server.timeout = float(os.environ.get("ABAQUS_HTTP_SLICE", "0.05"))
            _SERVER = server
            _BOUND_PORT = candidate
            break
        except Exception as exc:
            last_error = exc
            code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
            _log("bind %s:%s failed (%s)" % (HOST, candidate, code))
            continue

    if _SERVER is None:
        raise RuntimeError(
            "could not bind any candidate port on %s (tried %s): %s"
            % (HOST, candidates, last_error)
        )

    if _BOUND_PORT != primary:
        _log("WARNING: primary port %s unavailable; bound %s instead "
             "(endpoint published in %s)" % (primary, _BOUND_PORT, STATE_FILE))

    _START_TIME = time.time()
    _STOP_REQUESTED = False
    _ADOPTED_PID = None
    _MODE = "headless-blocking-serve" if block else "listener-only (GUI timer pumps)"
    _publish_state(True)
    message = "abaqus http bridge listening on http://%s:%s" % (HOST, _BOUND_PORT)
    _announce(message)
    _log("state file: %s" % STATE_FILE)

    if not block:
        return message

    _log("entering pump loop (thread=%s)" % threading.current_thread().name)
    _pump_loop()
    return message


def _pump_loop():
    """Serve requests in time slices, yielding to the GUI between them.

    Each slice serves at most one request. Between slices ``processUpdates()``
    lets the Qt event loop run, so an interactive CAE session stays usable while
    the kernel is dedicated to the bridge.
    """
    global _STOP_REQUESTED
    server = _SERVER
    while server is not None and not _STOP_REQUESTED:
        try:
            server.handle_request()
        except KeyboardInterrupt:
            # Ctrl+C is the documented way to interrupt a long kernel command;
            # let it reach Abaqus instead of swallowing it here.
            raise
        except BaseException as exc:  # noqa: BLE001
            # BaseException, not Exception: a SystemExit escaping from user code
            # must not be able to tear this loop down (see _kernel_execute).
            _log("handle_request error: %s: %s" % (type(exc).__name__, exc))
            time.sleep(0.05)
        try:
            from abaqus import session as _s
            _process_updates(_s)
        except Exception:
            pass
    _log("pump loop exited")
    # Tear the server down HERE, not in the /stop handler: that handler only
    # sets the flag (closing the socket there would pull the rug out from under
    # the response being written), and this loop is the one place guaranteed to
    # run afterwards. Without this, a headless session kept the socket bound,
    # left bridge.json claiming running=true, and a later start_bridge() in the
    # same process answered "already listening" while serving nothing at all.
    stop_bridge()


def _process_updates(session_obj):
    """Let the GUI repaint; a no-op in noGUI sessions that lack the call.

    There is deliberately no sleep in the fallback: ``handle_request`` already
    blocks for the server timeout when the queue is empty, so a delay here would
    only postpone the next slice.
    """
    fn = getattr(session_obj, "processUpdates", None)
    if fn is None:
        return
    try:
        fn()
    except Exception:
        pass


def stop_bridge():
    """Ask the pump loop to stop and release the socket."""
    global _SERVER, _BOUND_PORT, _STOP_REQUESTED, _ADOPTED_PID, _MODE
    if _SERVER is None:
        # Do not touch the stop flag when there is nothing to stop: leaving it
        # set used to be harmless only by accident (the next start reset it).
        if _ADOPTED_PID is not None:
            return ("abaqus http bridge on http://%s:%s is served by pid %s; "
                    "stop it from that session." % (HOST, _BOUND_PORT, _ADOPTED_PID))
        return "abaqus http bridge is not running."
    _STOP_REQUESTED = True
    try:
        _SERVER.server_close()
    except Exception:
        pass
    _SERVER = None
    _BOUND_PORT = None
    _ADOPTED_PID = None
    _MODE = "stopped"
    _publish_state(False)
    _announce("abaqus http bridge stopped.")
    return "abaqus http bridge stopped."


def _request_stop():
    """Set the stop flag only. Called from inside a request handler, where
    closing the socket would pull the rug out from under the response."""
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    _log("stop requested by client")


def bridge_status():
    """Status dict; also used by the GUI menu's status button."""
    return {
        "version": __version__,
        "transport": "http",
        "endpoint": "http://%s:%s" % (HOST, _BOUND_PORT) if _BOUND_PORT else None,
        "host": HOST,
        "port": _BOUND_PORT,
        "running": _SERVER is not None,
        "mode": _MODE,
        "adopted_pid": _ADOPTED_PID,
        "processed": _PROCESSED,
        "uptime_seconds": int(time.time() - _START_TIME) if _START_TIME else 0,
        "requires_token": bool(TOKEN),
        "state_file": STATE_FILE,
        "log": LOG_FILE,
        "last_error": _LAST_ERROR,
    }


# --------------------------------------------------------------------------
# Abaqus-console / GUI-menu aliases
# --------------------------------------------------------------------------

def mcp_start(port=None):
    """Open the listener and return immediately. NON-BLOCKING.

    This is the GUI-menu entry point. It only binds and listens -- serving is
    driven by the GUI side, which re-issues ``mcp_serve_slice()`` from a FOX
    timer. Nothing here ever waits, so the CAE window stays responsive.

    Why serving cannot live here (measured on Abaqus 2024): the kernel's Python
    threads are only scheduled *while a kernel command is executing*. Once a
    command returns, the kernel process owns the GIL but runs no bytecode, so a
    background accept() thread never runs and every request times out. Hence the
    GUI timer is the only thing that can pump the socket.
    """
    try:
        message = start_bridge(port=port, block=False)
    except Exception as exc:
        message = "could not start the abaqus http bridge: %s: %s" % (type(exc).__name__, exc)
        print(message)
        _log(message)
        return message
    print(message)
    _log("listener open (non-blocking); GUI timer must call mcp_serve_slice()")
    return message


def mcp_serve_slice(max_requests=1):
    """Serve at most ``max_requests`` pending requests, then return.

    Called by the GUI-side timer. The zero-timeout ``select`` is what makes this
    cheap: with no pending connection this returns in microseconds, so a tick
    costs the GUI nothing. It blocks only for the duration of an actual request.

    Never raises when the bridge is not listening -- the GUI pump ticks
    unconditionally and must not have to handle a normal "not started" state.
    """
    global _STOP_REQUESTED
    if _SERVER is None:
        return 0

    served = 0
    while served < max_requests:
        try:
            listener = getattr(_SERVER, "socket", _SERVER)
            ready, _, _ = select.select([listener], [], [], 0)
        except Exception as exc:
            _log("serve_slice select failed: %s" % exc)
            break
        if not ready:
            break
        try:
            _SERVER.handle_request()
            served += 1
        except KeyboardInterrupt:
            # Ctrl+C must keep reaching Abaqus (see _kernel_execute).
            raise
        except BaseException as exc:  # noqa: BLE001
            # BaseException so a SystemExit from user code cannot escape into
            # sendCommand and take the kernel with it.
            _log("serve_slice handle failed: %s: %s" % (type(exc).__name__, exc))
            break

    # A client asked us to stop (POST /stop). Close now that we are outside the
    # handler that received the request.
    if _STOP_REQUESTED:
        _STOP_REQUESTED = False
        stop_bridge()
    return served


def mcp_serve_blocking(port=None):
    """Open the listener and serve on the CALLING thread until stopped.

    Only correct where the calling thread is already dedicated to the bridge --
    i.e. a headless ``abaqus cae noGUI=`` bootstrap. In a GUI session this
    blocks the window for as long as the bridge runs, so the menu path uses
    ``mcp_start()`` + the GUI timer instead.
    """
    try:
        message = start_bridge(port=port, block=True)
    except Exception as exc:
        message = "could not start the abaqus http bridge: %s: %s" % (type(exc).__name__, exc)
        print(message)
        _log(message)
        return message
    return message


def mcp_stop():
    message = stop_bridge()
    print(message)
    return message


def mcp_restart(port=None):
    """Stop (if running) and start again, optionally on a specific port.

    Useful from the GUI menu when the first candidate port turned out to be
    taken: a restart walks the candidate list again.
    """
    try:
        stop_bridge()
    except Exception:
        pass
    return mcp_start(port=port)


def mcp_endpoint():
    """Print the base URL of the running bridge (for pasting into a client)."""
    st = bridge_status()
    endpoint = st["endpoint"]
    print(endpoint if endpoint else "abaqus http bridge is not running")
    return endpoint


def mcp_status():
    """Print a readable status block (mirrors the old plugin's output)."""
    st = bridge_status()
    print("")
    print("=" * 58)
    print("Abaqus HTTP Bridge v" + st["version"])
    print("=" * 58)
    print("Endpoint:  %s" % (st["endpoint"] or "(stopped)"))
    print("Running:   %s" % st["running"])
    print("Mode:      %s" % st["mode"])
    if st["adopted_pid"] is not None:
        print("Adopted:   served by pid %s in another session" % st["adopted_pid"])
    print("Processed: %s" % st["processed"])
    print("Uptime:    %ss" % st["uptime_seconds"])
    print("Token:     %s" % ("required" if st["requires_token"] else "not required"))
    print("State:     %s" % st["state_file"])
    print("Log:       %s" % st["log"])
    if st["last_error"]:
        print("LastError: %s" % st["last_error"])
    print("=" * 58)
    print("")
    return st


def install_into_main():
    """Publish the control functions into ``__main__``.

    The GUI menu plugin resolves ``moduleName='__main__'``, so the menu buttons
    only work once these names exist in the kernel's ``__main__``. Calling this
    from the bootstrap script makes auto-start and the GUI menu cooperate.
    """
    try:
        import __main__
        for name in ("start_bridge", "stop_bridge", "bridge_status",
                     "mcp_start", "mcp_stop", "mcp_status",
                     "mcp_restart", "mcp_endpoint",
                     "mcp_serve_slice", "mcp_serve_blocking"):
            setattr(__main__, name, globals()[name])
        return True
    except Exception as exc:
        _log("install_into_main failed: %s" % exc)
        return False


def auto_start_from_env(blocking=True):
    """Open the bridge unless auto-start is off. Safe to call twice.

    The switch is ``CONFIG["enabled"]``, which already merges the two sources in
    the documented order: ``ABAQUS_HTTP_AUTOSTART`` wins when it is set and
    non-empty, otherwise ``bridge_config.json``'s ``enabled`` decides. Reading
    only the environment variable here used to mean that ``enabled: true`` did
    nothing at all for a ``script=``/``noGUI=`` launch, even though the README
    promised otherwise.

    ``blocking=True`` is for a headless bootstrap, where the calling thread is
    dedicated to serving. ``blocking=False`` only opens the listener and is for
    GUI sessions driven by the FOX-timer pump.
    """
    # DEFAULT OFF: opening a port must be an explicit request (the GUI menu,
    # `enabled: true`, or abaqus_launch_cae setting ABAQUS_HTTP_AUTOSTART=1). A
    # bare `abaqus cae script=bootstrap.py` therefore opens nothing -- otherwise
    # any Abaqus start would silently leave a reachable kernel behind.
    if not CONFIG.get("enabled", False):
        return ("auto-start disabled (set ABAQUS_HTTP_AUTOSTART=1 or \"enabled\": true "
                "in bridge_config.json)")
    install_into_main()
    return start_bridge(block=blocking)
