import plistlib
import sys
from datetime import datetime

import pytest

from kgp_opd import system


def test_creds_round_trip():
    assert system.load_creds() is None
    system.save_creds("23XX10000", 'pa"ss')
    assert system.load_creds() == ("23XX10000", 'pa"ss')
    assert system.forget_creds() is True
    assert system.load_creds() is None
    assert system.forget_creds() is False


def test_home_is_overridable(tmp_path):
    assert system.home() == tmp_path / "home" and system.home().is_dir()


def test_notify_never_raises(monkeypatch):
    monkeypatch.setattr(system.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(system.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    system.notify('quote " and \\ backslash')


def test_wait_for_network_gives_up_after_max_wait(monkeypatch):
    calls, logs = [], []
    monkeypatch.setattr(system.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(OSError("down")))
    clock = [0.0]
    monkeypatch.setattr(system.time, "time", lambda: clock[0])
    assert system.wait_for_network("https://x", max_wait=60, every=15, log=logs.append,
                                   sleep=lambda s: clock.__setitem__(0, clock[0] + s)) is False
    assert len(calls) == 5 and "No network after 1 min" in logs[-1]


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS-only")
def test_schedule_writes_an_interactive_one_shot_job(tmp_path, monkeypatch):
    monkeypatch.setattr(system, "agents_dir", lambda: tmp_path / "LaunchAgents")
    calls = []

    class R:
        returncode, stderr = 0, ""

    label = system.schedule(datetime(2026, 11, 2, 7, 50), ["--doctor", "doc1"],
                            launchctl=lambda *a: calls.append(a) or R())
    plist = plistlib.loads((tmp_path / "LaunchAgents" / f"{label}.plist").read_bytes())
    assert plist["ProcessType"] == "Interactive"
    assert plist["StartCalendarInterval"] == {"Month": 11, "Day": 2, "Hour": 7, "Minute": 50}
    args = plist["ProgramArguments"]
    assert args[1:4] == ["-m", "kgp_opd", "book"] and "--scheduled-job" in args
    monkeypatch.setattr(system.sys, "frozen", True, raising=False)
    assert system._self_command() == [system.sys.executable]  # the packaged app runs itself
    assert args[-2:] == ["--doctor", "doc1"]
    assert calls[-1][0] == "bootstrap"
    assert system.scheduled_jobs() == [label]
    system.unschedule(label, launchctl=lambda *a: calls.append(a) or R())
    assert system.scheduled_jobs() == [] and calls[-1][0] == "bootout"
