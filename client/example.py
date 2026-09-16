#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""End-to-end example: build a small model, solve it, read the result.

Prerequisite: Abaqus/CAE running with the bridge started
(Plug-ins > Abaqus HTTP Bridge > Start Bridge).

    python example.py

Safe to re-run: the demo model and job are deleted before they are recreated,
and the job is released at the end so the name is free next time.
"""
from __future__ import print_function

import sys
import time

from bridge_client import AbaqusBridge, AbaqusBridgeError, AbaqusKernelError


def main():
    ab = AbaqusBridge.from_state_file()          # finds the real port
    ab.wait_until_up(seconds=10)

    print("connected : %s" % ab.base_url())
    info = ab.status()
    print("mode      : %s" % (info.get("bridge") or {}).get("mode"))
    print("models    : %s" % (info.get("models") or []))
    print("")

    # 1) Model -- one call, state persists in the kernel afterwards.
    ab.value("""
from abaqus import mdb
from abaqusConstants import THREE_D, DEFORMABLE

if 'BridgeDemo' in mdb.models.keys():
    del mdb.models['BridgeDemo']
m = mdb.Model(name='BridgeDemo')

m.ConstrainedSketch(name='s', sheetSize=200.0)
m.sketches['s'].rectangle(point1=(0.0, 0.0), point2=(100.0, 20.0))
m.Part(name='Bar', dimensionality=THREE_D, type=DEFORMABLE)
m.parts['Bar'].BaseSolidExtrude(sketch=m.sketches['s'], depth=10.0)

m.Material(name='Steel')
m.materials['Steel'].Elastic(table=((210000.0, 0.3),))
m.materials['Steel'].Density(table=((7.85e-9,),))
m.HomogeneousSolidSection(name='Sec', material='Steel', thickness=None)
m.parts['Bar'].SectionAssignment(region=(m.parts['Bar'].cells,), sectionName='Sec')

m.StaticStep(name='Step-1', previous='Initial')
result = sorted(mdb.models.keys())
""")
    print("1) model built")

    # 2) Mesh -- a heavier call, so give it a real timeout.
    ab.value("""
from abaqus import mdb
p = mdb.models['BridgeDemo'].parts['Bar']
p.seedPart(size=5.0, deviationFactor=0.1)
p.generateMesh()
result = len(p.elements)
""", timeout=300)
    print("2) meshed")

    # 3) Jobs -- submit and poll. Never block on waitForCompletion(): that would
    #    hold the kernel, and with it the CAE window, for the whole solve.
    ab.value("""
from abaqus import mdb
if 'BridgeDemoJob' in mdb.jobs.keys():
    del mdb.jobs['BridgeDemoJob']
mdb.Job(name='BridgeDemoJob', model='BridgeDemo', type='ANALYSIS', numCpus=1)
mdb.jobs['BridgeDemoJob'].submit(consistencyChecking=False)
result = 'submitted'
""")
    print("3) submitted -- polling")

    status = None
    for _ in range(120):
        status = ab.value("from abaqus import mdb\nresult = str(mdb.jobs['BridgeDemoJob'].status)")
        if status in ("COMPLETED", "ABORTED"):
            break
        time.sleep(1)
    print("   status: %s" % status)

    # 4) Post-process the ODB. The job writes it next to the kernel's working
    #    directory, which is what os.getcwd() returns inside the kernel.
    odb = ab.value("""
from abaqus import mdb
import os
path = os.path.join(os.getcwd(), 'BridgeDemoJob.odb')
from odbAccess import openOdb
o = openOdb(path=path, readOnly=True)
try:
    result = {'steps': list(o.steps.keys()),
              'frames': [len(o.steps[s].frames) for s in o.steps.keys()]}
finally:
    o.close()
""", timeout=120)
    print("4) odb: %s" % odb)

    # 5) Release the job so a re-run can reuse the name. The .odb itself is left
    #    on disk, which is where you would want to inspect it anyway.
    ab.value("""
from abaqus import mdb
if 'BridgeDemoJob' in mdb.jobs.keys():
    del mdb.jobs['BridgeDemoJob']
result = 'released'
""")
    print("5) job released (the .odb is kept)")
    print("\ndone")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AbaqusKernelError as exc:
        print("Abaqus kernel error:\n%s" % exc.report(), file=sys.stderr)
        sys.exit(1)
    except AbaqusBridgeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
