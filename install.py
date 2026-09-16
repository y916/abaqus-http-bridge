#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Installer for Abaqus HTTP Bridge.

Installs ONE directory -- the whole plugin package -- into your Abaqus plugins
root. Abaqus scans <plugins root>/<package>/ for *_plugin.py, and the package
carries its own kernel-side module next to it, so no second location and no
PYTHONPATH setup is needed.

    python install.py
    python install.py --plugins-dir D:\\abaqus_plugins
    python install.py --dry-run

Stdlib only, so it runs under any Python 3 -- including the one Abaqus ships.
"""
from __future__ import print_function

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_PKG = os.path.join(HERE, "plugin")
PKG_NAME = "abaqus_http_bridge"

REQUIRED = ("__init__.py", "abaqus_http_bridge_plugin.py", "abaqus_http_bridge.py",
            "bootstrap.py", "selftest_http.py")
# Copy the shipped config, but never overwrite one the user has edited.
OPTIONAL_DO_NOT_CLOBBER = ("bridge_config.json",)


def _is_wsl():
    if sys.platform != "linux":
        return False
    if os.path.isdir("/mnt/c/Windows"):
        return True
    try:
        with open("/proc/version", "r") as fh:
            return "microsoft" in fh.read().lower()
    except Exception:
        return False


def _windows_home():
    """The Windows user profile, as seen from WSL.

    Installing into the *Linux* home when Abaqus lives on Windows is the obvious
    trap: ``~`` in WSL is /home/<you>, while Abaqus reads
    C:/Users/<you>/abaqus_plugins. Prefer the profile that already carries this
    deployment, then any real profile (one with NTUSER.DAT).
    """
    users = "/mnt/c/Users"
    if not os.path.isdir(users):
        return None
    try:
        names = sorted(d for d in os.listdir(users)
                       if os.path.isdir(os.path.join(users, d)))
    except Exception:
        return None
    for d in names:
        base = os.path.join(users, d)
        if (os.path.isdir(os.path.join(base, "abaqus_plugins"))
                or os.path.isdir(os.path.join(base, ".abaqus-http-bridge"))):
            return base
    for d in names:
        if os.path.isfile(os.path.join(users, d, "NTUSER.DAT")):
            return os.path.join(users, d)
    return None


def _home_base():
    """Where ~ points for the machine Abaqus actually runs on."""
    if _is_wsl():
        win = _windows_home()
        if win:
            return win, "windows profile as seen from WSL"
    return os.path.expanduser("~"), "this machine"


def default_plugins_dir():
    return os.path.join(_home_base()[0], "abaqus_plugins")


def show_abaqus():
    """Report the Abaqus Python engine.

    Abaqus ships BOTH a legacy python2.7 tree and the python3.x engine CAE
    actually runs on (2024 has python2.7 AND python3.10), so decide on python3
    -- looking for python2.7 alone is a false alarm.
    """
    roots = ["C:\\SIMULIA", "C:\\Program Files\\Dassault Systemes", "/opt/SIMULIA"]
    if _is_wsl():
        roots = ["/mnt/c/SIMULIA", "/mnt/c/Program Files/Dassault Systemes"] + roots
    found = [r for r in roots if os.path.isdir(r)]
    if not found:
        print("  note: no Abaqus install found in the usual locations.")
        print("        Install this on the machine that runs Abaqus/CAE.")
        return
    print("  Abaqus install: %s" % ", ".join(found))
    py3, py2 = [], []
    for root in found:
        for dirpath, dirnames, _files in os.walk(root):
            for d in list(dirnames):
                if d.startswith("python3"):
                    py3.append(os.path.join(dirpath, d))
                elif d.startswith("python2"):
                    py2.append(os.path.join(dirpath, d))
            dirnames[:] = [x for x in dirnames if not x.startswith("python")][:30]
            if py3:
                # One hit is all this report needs, and stopping here keeps the
                # scan short on machines carrying several Abaqus releases.
                break
        if py3:
            break
    if py3:
        print("  Python 3 engine: %s" % py3[0])
        print("  -> ok, the bridge needs Python 3 (Abaqus 2024 or newer).")
    elif py2:
        print("  WARNING: only Python 2.7 found:")
        for h in py2[:3]:
            print("           %s" % h)
        print("  -> the bridge requires Python 3 (Abaqus 2024 or newer).")
    else:
        print("  Could not determine the Abaqus Python version; the bridge")
        print("  requires Python 3 (Abaqus 2024 or newer).")


def main():
    ap = argparse.ArgumentParser(
        description="Install the Abaqus HTTP Bridge as a single Abaqus plugin package.")
    ap.add_argument("--plugins-dir", default=None,
                    help="Abaqus plugins root (default: ~/abaqus_plugins)")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be copied, change nothing")
    args = ap.parse_args()

    plugins_dir = os.path.abspath(args.plugins_dir or default_plugins_dir())
    target = os.path.join(plugins_dir, PKG_NAME)

    print("")
    print("=" * 68)
    print("Abaqus HTTP Bridge -- installer")
    print("=" * 68)
    home, why = _home_base()
    print("  this package : %s" % HERE)
    print("  target home  : %s  (%s)" % (home, why))
    print("  install to   : %s" % target)
    print("")
    show_abaqus()
    print("")

    if not os.path.isfile(os.path.join(SRC_PKG, REQUIRED[0])):
        raise SystemExit("package is incomplete: %s missing" % os.path.join(SRC_PKG, REQUIRED[0]))

    if not args.dry_run and not os.path.isdir(target):
        os.makedirs(target)

    written, kept = [], []
    for name in REQUIRED:
        src = os.path.join(SRC_PKG, name)
        if not os.path.isfile(src):
            raise SystemExit("missing source file: %s" % src)
        dst = os.path.join(target, name)
        if not args.dry_run:
            shutil.copy2(src, dst)
        written.append(dst)
    for name in OPTIONAL_DO_NOT_CLOBBER:
        src = os.path.join(SRC_PKG, name)
        dst = os.path.join(target, name)
        if os.path.isfile(dst):
            kept.append(dst)
            continue
        if not os.path.isfile(src):
            # Checked here too, not just for REQUIRED: a missing optional file
            # used to surface as a bare FileNotFoundError from shutil.copy2.
            raise SystemExit("missing source file: %s" % src)
        if not args.dry_run:
            shutil.copy2(src, dst)
        written.append(dst)

    for path in written:
        print("  %s %s" % ("would write" if args.dry_run else "wrote      ", path))
    for path in kept:
        print("  kept existing  %s" % path)

    print("")
    if args.dry_run:
        print("Dry run: nothing changed.")
        print("=" * 68)
        return 0

    print("=" * 68)
    print("INSTALLED. Next steps:")
    print("=" * 68)
    print("  1. RESTART Abaqus/CAE. The menu appears under")
    print("     Plug-ins > Abaqus HTTP Bridge")
    print("")
    print("  2. Configure it (this file is YOURS -- reinstall never overwrites it):")
    print("     %s" % os.path.join(target, "bridge_config.json"))
    print("       enabled  : true = open the port automatically at CAE startup")
    print("       port     : 49321")
    print("       token    : set a value to require X-Bridge-Token on every request")
    print("")
    print("  3. Check the load report:")
    print("     %s" % os.path.join(os.path.expanduser("~"), ".abaqus-http-bridge",
                                     "gui_plugin.json"))
    print("")
    print("  4. Talk to it (no third-party deps needed):")
    print("     python %s status" % os.path.join(HERE, "client", "bridge_client.py"))
    print("     python %s exec \"print(mdb.models.keys())\""
          % os.path.join(HERE, "client", "bridge_client.py"))
    print("")
    print("  Headless (no CAE window -- best for scripting/agents):")
    print("     set ABAQUS_HTTP_AUTOSTART=1")
    print("     set ABAQUS_HTTP_BLOCKING=1")
    print('     abaqus cae noGUI=%s' % os.path.join(target, "bootstrap.py"))
    print("     (BLOCKING=1 is required: with no GUI there is no timer to pump")
    print("      the listener, so a non-blocking start would accept connections")
    print("      and then never answer them.)")
    print("=" * 68)
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
