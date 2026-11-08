"""yield_apps (QUESTIONS #41): quit an owner-named desktop app while its memory is needed, reopen it afterwards."""

import plistlib

import pytest

from gsched import status as gstatus
from gsched.config import Config
from gsched.errors import ConfigError
from gsched.platform.fake import SIGTERM
from tests.daemon.conftest import client, project, run_ticks, spec
from tests.policy.builders import ms

R = {"name": "t233-pass", "days": ["mon", "tue", "wed", "thu", "fri"], "start": "19:00", "end": "22:45",
     "project": "latent-alpha", "memory_gib": 20, "gpu": "exclusive"}


@pytest.fixture
def bundle(tmp_path):
    app = tmp_path / "Data Terminal.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": "DataTerminal", "CFBundleIdentifier": "com.example.dataterminal"}, fh)
    return str(app)


class App:
    """A fake multi-process app: the main executable, a re-exec'd child of it and a helper."""

    def __init__(self, plat, bundle, gib=2.0):
        self.plat, self.bundle, self.gib = plat, bundle, gib
        self.start()

    def start(self):
        exe = f"{self.bundle}/Contents/MacOS/DataTerminal"
        self.main = self.plat.add_foreign("DataTerminal", self.gib * 0.5, path=exe)
        self.child = self.plat.add_foreign("DataTerminal", self.gib * 0.3, path=exe, ppid=self.main)
        self.helper = self.plat.add_foreign("Helper", self.gib * 0.2, ppid=self.main,
                                            path=f"{self.bundle}/Contents/Frameworks/H.app/Contents/MacOS/Helper")

    def running(self):
        return self.main in self.plat._procs

    def on_sigterm_quit(self):
        """Like an Electron app: SIGTERM to the main process quits the whole app."""
        def hook(rec, sig):
            if sig == SIGTERM and rec.pid == self.main:
                for p in (self.child, self.helper):
                    self.plat._procs.pop(p, None)
        self.plat.signal_hook = hook


def ycfg(bundle, **over):
    a = {"name": "terminal", "app": bundle, "reservations": ["t233-pass"], "quit": "sigterm", "reopen_delay_s": 60,
         **over}
    return Config.defaults().replace(enforce=True, timezone="UTC", night_window=None, reservations=(R,),
                                     yield_apps=(a,))


def events(d, kind):
    return d.store.events(kinds=[kind], since_ms=0)


@pytest.fixture(autouse=True)
def no_tick_errors(monkeypatch):
    """Every test here also fails on a tick_error (a guarded phase swallows exceptions)."""
    from gsched.tick import Ticker
    seen = []
    orig = Ticker._phase_failed

    def spy(self, name, e):
        seen.append((name, repr(e)))
        orig(self, name, e)
    monkeypatch.setattr(Ticker, "_phase_failed", spy)
    yield
    assert seen == []


def opens(plat):
    return [a for a in plat.async_spawned if a[0] == "/usr/bin/open"]


def reopen_hook(app):
    def hook(argv):
        if argv[0] == "/usr/bin/open":
            app.start()
            return (0, "", 0)
        return None
    return hook


def test_config_validation(bundle):
    base = Config.defaults().replace(reservations=(R,))
    ok = base.replace(yield_apps=({"name": "terminal", "app": bundle, "reservations": ["t233-pass"]},))
    assert ok.yield_apps[0]["name"] == "terminal"
    bad = [({"name": "x", "app": bundle}, "never yields"),
           ({"name": "x", "app": bundle, "reservations": ["nope"]}, "no reservation named"),
           ({"name": "x", "app": "relative.app", "for_jobs_gib": 8}, "absolute path"),
           ({"name": "x", "app": bundle, "for_jobs_gib": 8, "quit": "sigkill"}, "applescript or sigterm"),
           ({"name": "x", "app": bundle, "for_jobs_gib": 8, "busy_ports": [0]}, "TCP ports"),
           ({"name": "x", "app": bundle, "for_jobs_gib": 8, "colour": 1}, "unknown key")]
    for entry, msg in bad:
        with pytest.raises(ConfigError, match=msg):
            base.replace(yield_apps=(entry,))
    with pytest.raises(ConfigError, match="duplicate"):
        base.replace(yield_apps=({"name": "x", "app": bundle, "for_jobs_gib": 8},) * 2)


def test_reservation_window_closes_app_and_reopens_after(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle))
    app = App(plat, bundle)
    app.on_sigterm_quit()
    plat.async_hook = reopen_hook(app)
    clock.set_wall(ms("2026-11-09T18:59:00Z"))                    # Monday
    run_ticks(d, clock, 30)
    assert app.running() and plat.signals == []                   # not before the window
    run_ticks(d, clock, 60)                                       # 19:00 opens
    assert [(s[0], s[1]) for s in plat.signals] == [(app.main, SIGTERM)]   # the top-level main only, once
    run_ticks(d, clock, 3)
    assert not app.running()
    [closed] = events(d, "yield_app_closed")
    assert closed["body"]["reason"] == "reservation t233-pass" and closed["body"]["freed_gib"] == pytest.approx(2.0, 0.01)
    st = client(d, "status")
    assert st["yield_apps"][0]["phase"] == "closed"
    assert "terminal closed for reservation t233-pass" in gstatus.brief_line(st)
    run_ticks(d, clock, 600)
    assert len(plat.signals) == 1 and opens(plat) == []           # stays closed through the window
    clock.set_wall(ms("2026-11-09T22:45:30Z"))                   # window over
    run_ticks(d, clock, 30)
    assert opens(plat) == []                                      # reopen_delay_s not yet over
    run_ticks(d, clock, 40)
    assert opens(plat) == [["/usr/bin/open", "-g", "-a", bundle]]
    run_ticks(d, clock, 3)
    assert app.running() and len(events(d, "yield_app_reopened")) == 1
    assert client(d, "status")["yield_apps"][0]["phase"] == "idle"
    run_ticks(d, clock, 120)
    assert len(plat.signals) == 1 and len(opens(plat)) == 1       # nothing more today
    clock.set_wall(ms("2026-11-10T19:00:05Z"))                   # Tuesday: a new window, a new episode
    run_ticks(d, clock, 5)
    assert len(events(d, "yield_app_quit")) == 2


def test_app_not_running_is_left_alone_and_never_opened(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle))
    clock.set_wall(ms("2026-11-09T19:00:05Z"))
    run_ticks(d, clock, 30)
    clock.set_wall(ms("2026-11-09T23:00:00Z"))
    run_ticks(d, clock, 120)
    assert plat.signals == [] and opens(plat) == [] and events(d, "yield_app_quit") == []


def test_busy_port_client_postpones_the_quit(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle, busy_ports=[9001]))
    app = App(plat, bundle)
    app.on_sigterm_quit()
    clients = {"pids": [4242, app.main]}         # an ingest client, plus the app's own end of the connection

    def hook(argv):
        if argv[0] == "/usr/sbin/lsof":
            assert "-iTCP:9001" in argv and "-sTCP:ESTABLISHED" in argv
            return (0, "".join(f"{p}\n" for p in clients["pids"]), 0)
        return None
    plat.async_hook = hook
    clock.set_wall(ms("2026-11-09T19:00:05Z"))
    run_ticks(d, clock, 60)
    assert plat.signals == [] and app.running()
    [busy] = events(d, "yield_app_busy")
    assert busy["body"]["clients"] == [4242]
    clients["pids"] = [app.main]                  # the ingest finished
    run_ticks(d, clock, 35)
    assert [(s[0], s[1]) for s in plat.signals] == [(app.main, SIGTERM)] and not app.running()


def test_user_reopening_during_the_window_is_respected(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle))
    app = App(plat, bundle)
    app.on_sigterm_quit()
    clock.set_wall(ms("2026-11-09T19:00:05Z"))
    run_ticks(d, clock, 5)
    assert not app.running()
    app.start()                                    # the owner opens it again at 19:01
    run_ticks(d, clock, 600)
    assert app.running() and len(plat.signals) == 1
    assert len(events(d, "yield_app_reopened_by_user")) == 1
    clock.set_wall(ms("2026-11-09T23:00:00Z"))
    run_ticks(d, clock, 120)
    assert opens(plat) == []                       # it was not ours to reopen


def test_quit_that_does_not_happen_is_given_up_without_force(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle, quit="applescript", quit_timeout_s=20))
    app = App(plat, bundle)
    plat.async_hook = lambda argv: (1, "execution error: User canceled. (-128)\n", 0) if argv[0] == "/usr/bin/osascript" else None
    clock.set_wall(ms("2026-11-09T19:00:05Z"))
    run_ticks(d, clock, 5)
    [osa] = [a for a in plat.async_spawned if a[0] == "/usr/bin/osascript"]
    assert osa[-1] == 'tell application id "com.example.dataterminal" to quit'
    run_ticks(d, clock, 30)
    assert app.running() and plat.signals == []    # no signal at all in applescript mode, never SIGKILL
    assert len(events(d, "yield_app_quit_failed")) == 1 and len(events(d, "yield_app_quit_refused")) == 1
    run_ticks(d, clock, 600)
    assert len([a for a in plat.async_spawned if a[0] == "/usr/bin/osascript"]) == 1   # not retried this window
    assert opens(plat) == []


def test_heavy_job_blocked_by_live_memory_closes_app_then_reopens(make_daemon, clock, plat, bundle):
    cfg = ycfg(bundle, reservations=[], for_jobs_gib=8, job_wait_s=60, reopen_delay_s=30)
    d = make_daemon(cfg)
    project(d, "p")
    app = App(plat, bundle, gib=6.0)
    app.on_sigterm_quit()
    plat.async_hook = reopen_hook(app)
    plat.set_mem(reclaimable_gib=34)               # live = 34 - 6 (app) - 2 (headroom) = 26
    small = client(d, "submit", spec=spec(resources={"memory_gib": 27, "gpu": "none"}, name="big"))["job"]
    run_ticks(d, clock, 30)
    assert d.store.get(small)["state"] == "queued" and plat.signals == []
    assert client(d, "get", job=small)["blocked_by"] == ["memory_live"]
    run_ticks(d, clock, 40)                        # blocked for job_wait_s
    assert [(s[0], s[1]) for s in plat.signals] == [(app.main, SIGTERM)]
    run_ticks(d, clock, 5)
    assert d.store.get(small)["state"] == "held"   # admitted on the freed memory
    assert events(d, "yield_app_quit")[0]["body"]["reason"].startswith(f"job {small}")
    run_ticks(d, clock, 120)
    assert opens(plat) == []                       # kept closed while the job runs
    plat.workload(small).exit(0)
    run_ticks(d, clock, 10)
    assert d.store.get(small)["state"] == "done" and opens(plat) == []
    run_ticks(d, clock, 30)
    assert len(opens(plat)) == 1 and app.running()


def test_small_or_hopeless_jobs_do_not_close_the_app(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle, reservations=[], for_jobs_gib=8, job_wait_s=10))
    project(d, "p")
    app = App(plat, bundle, gib=2.0)
    plat.set_mem(reclaimable_gib=20)               # live 16
    client(d, "submit", spec=spec(resources={"memory_gib": 30, "gpu": "none"}, name="hopeless"))  # 16 + 1.6 < 30
    run_ticks(d, clock, 60)
    assert plat.signals == [] and app.running() and events(d, "yield_app_quit") == []


def test_closed_state_survives_restart_and_is_reopened(make_daemon, clock, plat, bundle, short_tmp):
    cfg = ycfg(bundle)
    d = make_daemon(cfg)
    app = App(plat, bundle)
    app.on_sigterm_quit()
    plat.async_hook = reopen_hook(app)
    clock.set_wall(ms("2026-11-09T19:00:05Z"))
    run_ticks(d, clock, 5)
    assert not app.running()
    d.stop()
    clock.set_wall(ms("2026-11-09T23:00:00Z"))
    d2 = make_daemon(cfg)
    run_ticks(d2, clock, 70)
    assert len(opens(plat)) == 1 and app.running()


def test_removing_the_entry_reopens_what_was_closed(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle))
    app = App(plat, bundle)
    app.on_sigterm_quit()
    plat.async_hook = reopen_hook(app)
    clock.set_wall(ms("2026-11-09T19:00:05Z"))
    run_ticks(d, clock, 5)
    assert not app.running()
    d.cfg = d.cfg.replace(yield_apps=())
    run_ticks(d, clock, 3)
    assert len(opens(plat)) == 1 and app.running() and len(events(d, "yield_app_unconfigured")) == 1


def test_enforce_off_never_closes(make_daemon, clock, plat, bundle):
    d = make_daemon(ycfg(bundle).replace(enforce=False))
    App(plat, bundle)
    clock.set_wall(ms("2026-11-09T19:00:05Z"))
    run_ticks(d, clock, 60)
    assert plat.signals == [] and events(d, "yield_app_quit") == []
