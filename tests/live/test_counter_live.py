import os
import re
from pathlib import Path

import pytest

from tests.daemon.conftest import client
from tests.live.conftest import PY, add_project, drive, live_spec

pytestmark = pytest.mark.live

COUNTER = str(Path(__file__).resolve().parents[2] / "examples" / "checkpoint_counter.py")


def test_checkpoint_counter_resumes(live_daemon):
    d = live_daemon()
    add_project(d)
    spec = live_spec([PY, COUNTER, "--steps", "60", "--step-s", "0.05"], checkpoint="cooperative",
                     runtime={"estimate_s": 60, "limit_s": 2})
    jid = client(d, "submit", spec=spec)["job"]
    drive(d, lambda: d.store.get(jid)["state"] in ("done", "failed", "lost", "cancelled"), timeout=30)
    r = d.store.get(jid)
    assert r["state"] == "done" and r["attempt"] == 2, dict(r)
    out = open(os.path.join(d.paths.job_dir(jid), "stdout.log")).read()
    m = re.search(r"checkpointed at (\d+)", out)
    assert m, out
    saved = int(m.group(1))
    assert 0 < saved < 60
    assert f"resumed at {saved}" in out
    after = out.split(f"resumed at {saved}")[1]
    assert after.strip().splitlines()[0] == f"step {saved + 1}"
    assert out.strip().endswith("done")
    sigs = [e["body"]["sig"] for e in d.store.events(job=jid, since_ms=0) if e["kind"] == "signal"]
    assert sigs == ["SIGUSR1"]
    assert "requeue" in [e["kind"] for e in d.store.events(job=jid, since_ms=0)]
