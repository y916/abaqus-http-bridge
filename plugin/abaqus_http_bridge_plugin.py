# -*- coding: utf-8 -*-
"""abaqus_http_bridge_plugin.py -- GUI menu + non-blocking serving pump.

Everything in this package installs as ONE Abaqus plugin directory::

    ~/abaqus_plugins/abaqus_http_bridge/
        __init__.py
        abaqus_http_bridge_plugin.py   <- Abaqus scans this (GUI process)
        abaqus_http_bridge.py          <- kernel-side HTTP service
        bootstrap.py                   <- abaqus cae script=<this>
        selftest_http.py
        bridge_config.json             <- edit me

WHY ONE DIRECTORY WORKS EVEN THOUGH `~/abaqus_plugins` IS NOT AN IMPORT PATH
Abaqus only *scans* ~/abaqus_plugins and imports each package's ``*_plugin.py``
into the GUI process; it does not add the directory to ``sys.path`` for either
process (measured: the kernel's sys.path has no abaqus_plugins entry, and
``import`` of a module sitting there fails in both the kernel and the GUI).

Two measured facts make a single directory enough anyway:
  * a GUI plugin **does** get ``__file__``, so this file can put its own
    directory on the kernel's sys.path in the ``sendCommand`` bootstrap;
  * a ``script=`` script gets no ``__file__`` but **does** get its path in
    ``sys.argv``, so ``bootstrap.py`` can locate itself.

WHY A GUI-SIDE PUMP IS NECESSARY  (measured on Abaqus 2024)
  * The kernel's Python threads run only while a kernel command is executing: a
    ticker thread got 8 ticks during a 4 s command and **0 in the 18 s after it
    returned**. A background accept() thread therefore never runs.
  * The GUI process behaves the same way -- its C++ event loop holds the GIL
    while idle, so a socket server in a GUI thread accepted once (during plugin
    loading) and then froze in recv.
  * ``sendcmd.sendCommand`` and the ``guiInternal.sendCommand`` beneath it are
    both synchronous; a 3 s kernel sleep returned after 3.004 s.
  * A **FOX timer** callback does run, because the event loop that holds the GIL
    is what invokes it. Verified: 78 ticks in 30 s at 300 ms.

So the kernel must be invited back periodically, and the FOX timer is the only
invitation mechanism. Each tick does bounded work and returns.

WHY THE WINDOW STAYS RESPONSIVE
The kernel-side ``mcp_serve_slice()`` polls its socket with a **zero-timeout
select**, so a tick with no pending request returns in microseconds; a tick with
no bridge running does not touch the kernel at all. Blocking happens only for
the duration of an actual request -- as if that command were typed into the CLI.

`functionName` IS NOT AN EXPRESSION
``registerKernelMenuButton(moduleName=X, functionName=Y)`` builds the literal
string ``X + "." + Y`` and hands it to ``sendcmd.sendCommand``. So ``Y`` must be
an attribute chain relative to ``X`` and must not contain single quotes. The old
``functionName="__import__('sys')..."`` form produced
``sendCommand('__main__.__import__('sys')...')``, which fails twice: builtins are
not module attributes, and the quotes tore the string apart.
"""

import io
import json
import os
import sys
import time

from abaqusConstants import ALL
from abaqusGui import FXMAPFUNC, FXObject, SEL_TIMEOUT, getAFXApp
from sendcmd import sendCommand  # GUI -> kernel; see module docstring

# --------------------------------------------------------------------------
# Locate ourselves; this IS the bridge directory now.
# --------------------------------------------------------------------------

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
BRIDGE_DIR = os.environ.get("ABAQUS_HTTP_BRIDGE_DIR", "").strip() or PLUGIN_DIR

try:
    # Kernel-safe module: importing it in the GUI process is fine (it never
    # imports abaqusGui). Reused here purely for load_config().
    import abaqus_http_bridge as _bridge
    CONFIG = _bridge.load_config()
except Exception:                                    # noqa: BLE001
    CONFIG = {"enabled": False, "timeout": 120}

STATE_DIR = str(CONFIG.get("state_dir") or "").strip() or os.path.join(
    os.path.expanduser("~"), ".abaqus-http-bridge")
STATE_FILE = os.path.join(STATE_DIR, "bridge.json")
AUTOSTART = bool(CONFIG.get("enabled", False))


# --------------------------------------------------------------------------
# Kernel preparation (runs once, at plugin load)
# --------------------------------------------------------------------------

def _prepare_kernel():
    """Put this directory on the kernel's sys.path and publish the bridge control
    functions into the kernel ``__main__`` -- the namespace
    ``moduleName='__main__'`` resolves against. Idempotent."""
    command = (
        'import sys;'
        'sys.path.insert(0, r"{dir}");'
        'import abaqus_http_bridge as _abaqus_http_bridge;'
        '_abaqus_http_bridge.install_into_main()'
    ).format(dir=BRIDGE_DIR)
    sendCommand(command)
    return command


# --------------------------------------------------------------------------
# The serving pump  (FOX timer -> kernel slice)
# --------------------------------------------------------------------------

def _bridge_is_listening(not_before=None):
    """Cheap local check of the bridge's published state.

    Touches no kernel, so a tick costs ~microseconds while the bridge is down.

    ``not_before`` rejects a **stale** state file -- one written before this CAE
    session loaded the plugin. Without it, a bridge.json left behind by a
    previous run (or by a headless bridge elsewhere) makes the pump go active
    during CAE *startup* and hammer the GUI<->kernel IPC, which segfaults
    ABQcaeG (``ipc_TOO_LITTLE_SENT`` / ``WSAENOTSOCK``).
    """
    try:
        if not_before is not None:
            try:
                if os.path.getmtime(STATE_FILE) <= not_before:
                    return False
            except Exception:
                return False
        with io.open(STATE_FILE, "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("running"))
    except Exception:
        return False


class BridgePump(FXObject):
    """Re-arms a one-shot FOX timeout and asks the kernel to serve one slice.

    Load-bearing construction details (each found the hard way):

    * ``FXMAPFUNC``'s first argument must own ``FXMSGMAP`` -- the **instance**,
      not the class. Passing the class registers without error and then the
      handler is never found.
    * ``SEL_TIMEOUT`` is the message **type**; the same app-specific **id** must
      go to both ``FXMAPFUNC`` and ``addTimeout``. Passing ``SEL_TIMEOUT`` as
      the id is accepted and never fires.
    * A FOX timeout is **one-shot**; the callback must re-arm it, and the
      instance must be held in a module-level reference or it is collected.

    A failing kernel call backs the pump off, it does not switch it off: after
    ``COOLDOWN_S`` the pump tries again, and ``OK_STREAK_TO_CLEAR`` clean ticks
    in a row reset the error state. Making ``errors`` a one-way counter used to
    mean a single transient ``sendCommand`` failure disabled the bridge for the
    rest of the CAE session -- the menu could not bring it back either.
    """

    ID_TIMER = 1001
    ACTIVE_MS = 100          # while the bridge is listening
    IDLE_MS = 500            # while it is not: just a local file read
    ERROR_MS = 2000          # while backing off after a failing kernel call
    COOLDOWN_S = 5.0         # stop serving this long after a failure, then retry
    OK_STREAK_TO_CLEAR = 3   # consecutive clean ticks that reset `errors`
    WARMUP_TICKS = 12        # ~6 s at IDLE_MS: let CAE finish coming up first
    MAX_REQUESTS_PER_TICK = 1
    AUTOSTART_ATTEMPTS = 3   # the kernel may still be initialising: retry a bit

    def __init__(self):
        FXObject.__init__(self)
        self.ticks = 0
        self.slices = 0
        self.errors = 0
        self.ok_streak = 0
        self.last_error = None
        self._cooldown_until = 0.0
        self._load_time = time.time()
        self._warm = False
        self._autostart_done = False
        self._autostart_attempts = 0
        FXMAPFUNC(self, SEL_TIMEOUT, self.ID_TIMER, BridgePump.onTimeout)

    def start(self):
        self._arm()

    def _arm(self):
        getAFXApp().addTimeout(self._interval_ms(), self, self.ID_TIMER)

    def _interval_ms(self):
        if self.errors:
            return self.ERROR_MS
        return self.ACTIVE_MS if self.running else self.IDLE_MS

    def _cooling_down(self):
        return time.time() < self._cooldown_until

    def _note_ok(self):
        """Record a tick that reached the kernel cleanly."""
        if not self.errors:
            return
        self.ok_streak += 1
        if self.ok_streak >= self.OK_STREAK_TO_CLEAR:
            self.errors = 0
            self.ok_streak = 0
            self.last_error = None

    def _note_error(self, exc):
        """Record a failure and back off -- but stay recoverable."""
        self.errors += 1
        self.ok_streak = 0
        self.last_error = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        self._cooldown_until = time.time() + self.COOLDOWN_S

    @property
    def running(self):
        return _bridge_is_listening(self._load_time)

    def onTimeout(self, sender, sel, ptr):
        self.ticks += 1

        # Warm-up: plugin load happens while CAE is still initialising. Driving
        # the GUI<->kernel IPC in that window is what segfaulted ABQcaeG.
        if not self._warm:
            if self.ticks >= self.WARMUP_TICKS:
                self._warm = True
            else:
                self._arm()
                return 1

        # config "enabled": open the port automatically, once, on request.
        if not self._autostart_done:
            if AUTOSTART and not self.running:
                try:
                    sendCommand('__main__.mcp_start()')
                    self._autostart_done = True
                except Exception as exc:                  # noqa: BLE001
                    # Give the kernel a few chances before giving up, instead of
                    # marking the attempt done after the very first failure.
                    self._autostart_attempts += 1
                    self.last_error = "autostart: %s: %s" % (type(exc).__name__, exc)
                    if self._autostart_attempts >= self.AUTOSTART_ATTEMPTS:
                        self._autostart_done = True
            else:
                self._autostart_done = True

        if self.running and not self._cooling_down():
            try:
                # Blocks only for the duration of an actual request; returns in
                # microseconds when the queue is empty (zero-timeout select).
                sendCommand('__main__.mcp_serve_slice(%d)' % self.MAX_REQUESTS_PER_TICK)
                self.slices += 1
                self._note_ok()
            except Exception as exc:                      # noqa: BLE001
                self._note_error(exc)
        self._arm()
        return 1


_pump = None


def _start_pump():
    global _pump
    if _pump is None:
        _pump = BridgePump()
        _pump.start()


# --------------------------------------------------------------------------
# Load-time sequence
# --------------------------------------------------------------------------

def _record_load(bootstrap_ok, error=None):
    payload = {
        "plugin": "abaqus_http_bridge",
        "version": _META["version"],
        "gui_plugin_loaded": True,
        "plugin_dir": PLUGIN_DIR,
        "bridge_dir": BRIDGE_DIR,
        "config_source": CONFIG.get("_source"),
        "config_enabled": AUTOSTART,
        "kernel_bootstrap_sent": bootstrap_ok,
        "pump_armed": _pump is not None,
        "pump_interval_ms_active": BridgePump.ACTIVE_MS,
        "pump_interval_ms_idle": BridgePump.IDLE_MS,
        "pump_interval_ms_error": BridgePump.ERROR_MS,
        "menu_namespace": _META["moduleName"],
        "buttons": ["Start", "Restart", "Stop", "Status", "Endpoint"],
    }
    if error:
        payload["error"] = str(error)
    try:
        if not os.path.isdir(STATE_DIR):
            os.makedirs(STATE_DIR)
        # The encoding is explicit on purpose: the Windows default is the ANSI
        # code page, so a non-ASCII user name would make json.dump raise -- and
        # this file is exactly what the troubleshooting section points at.
        with io.open(os.path.join(STATE_DIR, "gui_plugin.json"),
                     "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except Exception:
        pass


_bootstrap_ok = False
_bootstrap_error = None
try:
    _prepare_kernel()
    _bootstrap_ok = True
except Exception as _exc:                              # noqa: BLE001
    _bootstrap_error = _exc

try:
    _start_pump()
except Exception as _exc:                              # noqa: BLE001
    _bootstrap_error = _bootstrap_error or _exc


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------

toolset = getAFXApp().getAFXMainWindow().getPluginToolset()

_META = dict(
    moduleName="__main__",      # namespace the command string resolves against
    icon=None,
    applicableModules=ALL,
    version="1.3.5",
    author="Thompson Labs",
    helpUrl="",
)

toolset.registerKernelMenuButton(
    buttonText="Abaqus HTTP Bridge|Start Bridge",
    functionName="mcp_start()",
    description=("Open the HTTP bridge listener. Returns immediately: a GUI-side timer "
                 "pumps the socket, so the CAE window stays responsive."),
    **_META
)

toolset.registerKernelMenuButton(
    buttonText="Abaqus HTTP Bridge|Restart Bridge (re-pick port)",
    functionName="mcp_restart()",
    description="Close the listener and open it again, walking the port candidate list.",
    **_META
)

toolset.registerKernelMenuButton(
    buttonText="Abaqus HTTP Bridge|Stop Bridge",
    functionName="mcp_stop()",
    description="Close the HTTP bridge. Works even while the bridge is serving.",
    **_META
)

toolset.registerKernelMenuButton(
    buttonText="Abaqus HTTP Bridge|Bridge Status",
    functionName="mcp_status()",
    description="Print bridge endpoint, uptime, and last error to the message area.",
    **_META
)

toolset.registerKernelMenuButton(
    buttonText="Abaqus HTTP Bridge|Show Endpoint",
    functionName="mcp_endpoint()",
    description="Print the bridge base URL so it can be pasted into a client.",
    **_META
)

_record_load(_bootstrap_ok, _bootstrap_error)
