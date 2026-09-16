# -*- coding: utf-8 -*-
"""bootstrap.py -- load and start the Abaqus HTTP bridge inside Abaqus/CAE.

Supported entry points
----------------------
1. **Automatic** (what the DSH plugin's ``abaqus_launch_cae`` does)::

       abaqus cae script=bootstrap.py

2. **Manual**, in an already-open CAE: ``File > Run Script...`` and pick this
   file.

Either way the control functions are published into the kernel's ``__main__``,
which is where the GUI menu plugin resolves ``moduleName='__main__'``. Whether
the listener actually opens is a separate decision -- see the auto-start switch
below; by default it stays closed.

Why path resolution is non-trivial
----------------------------------
Abaqus does **not** define ``__file__`` in scripts started via ``script=`` /
``noGUI=`` (verified on Abaqus 2024: neither in the script globals nor in
``__main__``). ``sys.argv`` is however populated, e.g.::

    [ABQcaeK.exe, -cae, -noGUI, _test.py, -tmpdir, ...]

so the script's own location is recoverable from there. The full chain is
resolved by :func:`resolve_bridge_dir`, with a documented last-resort default
so a Run-Script launch still works when the console's cwd is elsewhere.

Environment overrides (all optional)::

    ABAQUS_HTTP_BRIDGE_DIR   directory containing abaqus_http_bridge.py
    ABAQUS_HTTP_HOST         default 127.0.0.1
    ABAQUS_HTTP_PORT         default 49321 (falls back through candidates)
    ABAQUS_HTTP_TOKEN        default empty (no auth). An empty value deliberately
                             clears a token set in bridge_config.json.
    ABAQUS_HTTP_AUTOSTART    overrides bridge_config.json's "enabled"; when the
                             variable is unset the config file decides, and the
                             default is OFF
    ABAQUS_HTTP_BLOCKING     "1" = serve on this thread until stopped;
                             "0" = default, only open the listener and let the
                             GUI plugin's timer pump it
    ABAQUS_HTTP_STATE_DIR    default ~/.abaqus-http-bridge
"""

from __future__ import absolute_import, print_function

import os
import sys

BRIDGE_MODULE = "abaqus_http_bridge.py"

#: Last-resort location: this file's own package inside the plugins root.
DEFAULT_BRIDGE_DIR = os.path.join(os.path.expanduser("~"), "abaqus_plugins",
                                  "abaqus_http_bridge")


def _has_bridge(directory):
    try:
        return bool(directory) and os.path.isfile(os.path.join(directory, BRIDGE_MODULE))
    except Exception:
        return False


def _script_dir_from_argv():
    """Recover this script's directory from Abaqus' ``sys.argv``."""
    for arg in list(getattr(sys, "argv", []) or []):
        try:
            if not isinstance(arg, str) or not arg.lower().endswith(".py"):
                continue
            candidate = os.path.dirname(os.path.abspath(arg))
            if _has_bridge(candidate):
                return candidate
        except Exception:
            continue
    return None


def _looks_headless():
    """True when Abaqus was started without a GUI (``-noGUI`` in argv).

    Only used to warn: in a headless session nothing ever calls
    ``mcp_serve_slice()``, so a listener opened in non-blocking mode is reachable
    but mute. That is a confusing failure, so say it out loud instead.
    """
    for arg in list(getattr(sys, "argv", []) or []):
        try:
            if isinstance(arg, str) and arg.lower() in ("-nogui", "--nogui"):
                return True
        except Exception:
            continue
    return False


def resolve_bridge_dir():
    """Find the directory holding ``abaqus_http_bridge.py``.

    Order: explicit env var -> ``__file__`` (absent under Abaqus scripts) ->
    ``sys.argv`` -> cwd -> already-importable module -> documented default.
    """
    # 1. explicit override
    env = os.environ.get("ABAQUS_HTTP_BRIDGE_DIR", "").strip()
    if _has_bridge(env):
        return env

    # 2. __file__ (defined for Run-Script in some Abaqus builds, not 2024 noGUI)
    try:
        candidate = os.path.dirname(os.path.abspath(__file__))
        if _has_bridge(candidate):
            return candidate
    except NameError:
        pass

    # 3. sys.argv (Abaqus puts the script path here)
    candidate = _script_dir_from_argv()
    if candidate:
        return candidate

    # 4. current working directory
    if _has_bridge(os.getcwd()):
        return os.getcwd()

    # 5. already importable (e.g. PYTHONPATH configured in abaqus_v6.env)
    try:
        import abaqus_http_bridge
        found = os.path.dirname(os.path.abspath(abaqus_http_bridge.__file__))
        if _has_bridge(found):
            return found
    except Exception:
        pass

    # 6. documented default: this package inside the Abaqus plugins root
    return DEFAULT_BRIDGE_DIR


def main():
    bridge_dir = resolve_bridge_dir()
    if bridge_dir not in sys.path:
        sys.path.insert(0, bridge_dir)

    print("")
    print("=" * 60)
    print("Abaqus HTTP Bridge bootstrap")
    print("=" * 60)
    print("Bridge dir: %s" % bridge_dir)

    try:
        import abaqus_http_bridge as bridge
    except Exception as exc:
        print("FATAL: cannot import abaqus_http_bridge from %s" % bridge_dir)
        print("       %s: %s" % (type(exc).__name__, exc))
        print("       Set ABAQUS_HTTP_BRIDGE_DIR to the directory holding")
        print("       %s and retry." % BRIDGE_MODULE)
        raise

    print("Module:     v%s" % bridge.__version__)

    # Publish control functions into the kernel __main__ so that the GUI menu
    # plugin (moduleName='__main__') can resolve them.
    if bridge.install_into_main():
        print("Menu funcs: installed into __main__")
    else:
        print("Menu funcs: FAILED to install into __main__")

    try:
        # ABAQUS_HTTP_BLOCKING=1: this process exists only to serve, so block on
        # the pump loop. Default 0, i.e. just open the listener and let the GUI
        # plugin's FOX-timer pump drive it, so a visible CAE window stays
        # responsive.
        _blocking = os.environ.get('ABAQUS_HTTP_BLOCKING', '0').strip() not in ('0', 'false', 'False')
        if not _blocking and _looks_headless():
            # Worth shouting about: with no GUI there is no timer to call
            # mcp_serve_slice(), so the port would accept connections and then
            # never answer any of them.
            print("")
            print("WARNING: this looks like a headless (-noGUI) session, but")
            print("         ABAQUS_HTTP_BLOCKING is not set, so nothing will")
            print("         pump the listener and every request will time out.")
            print("         Set ABAQUS_HTTP_BLOCKING=1 for a headless session.")
        print(bridge.auto_start_from_env(blocking=_blocking))
    except Exception as exc:
        print("Could not start the HTTP bridge: %s: %s" % (type(exc).__name__, exc))
        print("Ports tried: %s" % (bridge.PORT_CANDIDATES,))
        return

    st = bridge.bridge_status()
    print("Endpoint:   %s" % st["endpoint"])
    print("State file: %s" % st["state_file"])
    print("Menu:       Plug-ins > Abaqus HTTP Bridge")
    print("=" * 60)
    print("")


main()
