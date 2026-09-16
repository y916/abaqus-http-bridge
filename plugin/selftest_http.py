# -*- coding: utf-8 -*-
"""selftest_http.py -- DIAGNOSTIC ONLY. Validates the HTTP bridge end to end.

Started as ``abaqus cae noGUI=selftest_http.py`` (or ``script=`` in a visible
session). It exercises the kernel path locally, then enters the bridge's pump
loop so a client can drive it.

The report is written to JSON because Abaqus swallows stdout under ``noGUI=``,
and it is flushed BEFORE the pump loop is entered -- ``start_bridge`` blocks by
design, so anything printed afterwards would only be seen once the bridge is
stopped.

Report location, in order of precedence:

1. ``$ABAQUS_TEST_OUT`` when set;
2. ``<state_dir>/selftest_out.json`` -- the same directory as ``bridge.json``
   and ``bridge.log``, i.e. where a user already looks;
3. ``./selftest_out.json`` when the bridge module could not be imported, so that
   the import failure itself is still reported somewhere predictable.

It does NOT consult ``enabled``/``ABAQUS_HTTP_AUTOSTART``: a diagnostic should
run when it is asked to run.
"""

from __future__ import absolute_import

import io
import json
import os
import sys

_ENV_OUT = (os.environ.get("ABAQUS_TEST_OUT") or "").strip()
OUT = _ENV_OUT or os.path.join(os.getcwd(), "selftest_out.json")


def emit(d):
    """Write the report. Never raises -- but never fails silently either."""
    try:
        d["report_path"] = OUT
        with io.open(OUT, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=2, default=str)
        print("selftest report: %s" % OUT)
    except Exception as exc:
        print("selftest: could not write %s (%s: %s)" % (OUT, type(exc).__name__, exc))


report = {"stage": "start", "sys_argv": list(sys.argv), "cwd": os.getcwd()}

here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else None
if not here:
    for a in sys.argv:
        if isinstance(a, str) and a.lower().endswith(".py") and os.path.isfile(a):
            here = os.path.dirname(os.path.abspath(a))
            break
if not here:
    here = os.getcwd()
report["bridge_dir"] = here
if here not in sys.path:
    sys.path.insert(0, here)

try:
    import abaqus_http_bridge as bridge
    report["import_ok"] = True
    report["version"] = bridge.__version__
except Exception as exc:
    report["import_ok"] = False
    report["import_error"] = "%s: %s" % (type(exc).__name__, exc)
    report["stage"] = "import_failed"
    emit(report)
    raise

# The module is importable, so use its own state directory: that keeps the
# report next to bridge.json / bridge.log instead of wherever the console's cwd
# happens to be.
if not _ENV_OUT:
    OUT = os.path.join(bridge.STATE_DIR, "selftest_out.json")

report["port_candidates"] = bridge.PORT_CANDIDATES

# Exercise the kernel path locally before blocking.
try:
    local = bridge._kernel_execute(
        "from abaqus import mdb, session\n"
        "result = {'models': list(mdb.models.keys()), 'viewports': list(session.viewports.keys())}"
    )
    report["local_exec_ok"] = local.get("ok")
    report["local_exec_value"] = local.get("return_value")
    report["local_exec_error"] = local.get("core_error")
except Exception as exc:
    report["local_exec_ok"] = False
    report["local_exec_error"] = str(exc)

bridge.install_into_main()
report["install_into_main"] = hasattr(sys.modules["__main__"], "start_bridge")

report["stage"] = "starting"
emit(report)

try:
    # Blocks in the pump loop, serving requests until POST /stop.
    bridge.start_bridge()
except Exception as exc:
    report["stage"] = "start_failed"
    report["start_error"] = "%s: %s" % (type(exc).__name__, exc)
    emit(report)
    raise

report["stage"] = "stopped"
emit(report)
