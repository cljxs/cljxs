"""Run a scan from Scout's house (owner, 2026-10-16: "Can you add a run scan
option inside scouts house"). scout.js starts the unit the 06:30 timer
starts, never a second one while it runs, and reads back its state.

    python3 -m unittest tests.test_scout_scan -b

systemctl is stood in for by a script on PATH that records each call and
plays a oneshot unit: "activating" from start until it is told to stop.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = ROOT / "mission-control-api"
NODE = shutil.which("node")


@unittest.skipUnless(NODE and (API / "node_modules" / "express").is_dir(), "node or the Deck's modules missing")
class TheScanButton(unittest.TestCase):

    def test_start_once_and_say_so(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "bin").mkdir()
            calls, flag = d / "calls", d / "running"
            fake = d / "bin" / "systemctl"
            fake.write_text(f"""#!/bin/sh
echo "$*" >> {calls}
case "$1" in
  is-active) if [ -f {flag} ]; then echo activating; else echo inactive; exit 3; fi ;;
  start) touch {flag} ;;
esac
""")
            fake.chmod(0o755)
            scans = d / "agents" / "scout" / "state" / "scans"
            scans.mkdir(parents=True)
            (scans / "s.json").write_text("{}")
            js = (
                f"const express=require({json.dumps(str(API / 'node_modules' / 'express'))});"
                f"const scout=require({json.dumps(str(API / 'scout.js'))});"
                "const http=require('http');const app=express();app.use(express.json());scout.register(app,0);"
                "const srv=app.listen(0,'127.0.0.1',async()=>{const port=srv.address().port;"
                "const go=(m,p)=>new Promise(ok=>{const q=http.request({host:'127.0.0.1',port,path:p,method:m},r=>{"
                "let s='';r.on('data',c=>s+=c);r.on('end',()=>ok([r.statusCode,JSON.parse(s)]));});q.end();});"
                "const out={idle:await go('GET','/api/scout/scan'),start:await go('POST','/api/scout/scan'),"
                "busy:await go('POST','/api/scout/scan'),state:await go('GET','/api/scout/scan')};"
                "process.stdout.write(JSON.stringify(out));srv.close();});")
            r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, ECOSYSTEM_ROOT=str(d), PATH=f"{d / 'bin'}:{os.environ['PATH']}"))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)
            self.assertEqual(out["idle"][0], 200)
            self.assertFalse(out["idle"][1]["running"])
            self.assertTrue(out["idle"][1]["last_scan_utc"], "the newest scan file's time")
            self.assertEqual((out["start"][0], out["start"][1]["running"]), (202, True))
            self.assertEqual(out["busy"][0], 409, "a second press while it runs starts nothing")
            self.assertTrue(out["state"][1]["running"])
            starts = [l for l in calls.read_text().splitlines() if l.startswith("start")]
            self.assertEqual(starts, ["start --no-block scout-niches.service"], "the timer's own unit, once")

    def test_the_house_has_the_button(self):
        page = (API / "public" / "village.html").read_text()
        self.assertIn('<div id="scoutScan"></div>', page)
        self.assertIn("fetch('/api/scout/scan', { method: 'POST' })", page)
        self.assertIn("ExecStart=/usr/bin/python3 /root/ecosystem/scripts/niche-scan.py run",
                      (ROOT / "deploy" / "scout-niches.service").read_text(), "the button runs what the timer runs")


if __name__ == "__main__":
    unittest.main()
