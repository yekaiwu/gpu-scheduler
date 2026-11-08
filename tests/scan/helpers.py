import os

from gsched.platform import JobIdentity


def start_job(plat, job_id, tmp="/tmp"):
    """Spawn a fake shim+workload and return (row-like dict, FakeWorkload) as the store would hold them."""
    r, w = os.pipe()
    gr, gw = os.pipe()
    d = f"{tmp}/gs-scan-{os.getpid()}-{job_id}"
    os.makedirs(d, exist_ok=True)
    shim = plat.spawn_shim(["/usr/bin/python3", "-I", "-m", "gsched.shim", "--job", job_id, "--gate-fd", str(gr),
                            "--report-fd", str(w), "--exit-file", d + "/exit.json", "--", "sleep", "9"],
                           env={}, cwd=d, stdout_path=d + "/o", stderr_path=d + "/e", pass_fds=(gr, w))
    os.close(gr)
    os.close(w)
    wl = int(os.read(r, 64).strip())
    os.close(r)
    os.write(gw, b"1")
    p = plat.proc(wl)
    sp = plat.proc(shim)
    row = {"id": job_id, "pid": wl, "pgid": p.pgid, "shim_pid": shim, "spawn": "daemon", "listen_port": None}
    return row, plat.workload(job_id)
