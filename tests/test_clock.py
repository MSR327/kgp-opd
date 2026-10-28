import random
import sys
import threading
import time
from email.utils import formatdate
from pathlib import Path

import pytest


from bcroy_booker.booker import ClockSync, sync_clock, watch_and_strike  # noqa: E402
from tests.test_fire import FakeIMediX, FORM, Q, roster_url  # noqa: E402


def test_offset_from_date_headers_is_tight():
    rng = random.Random(7)
    true_offset, clock, t = 0.37, ClockSync(), 1_000_000.0
    for _ in range(40):
        t += rng.uniform(0.05, 0.4)
        rtt = rng.uniform(0.15, 0.35)
        server_now = t + rng.uniform(0, rtt) + true_offset  # server stamps somewhere in flight
        clock.add(t, t + rtt, formatdate(int(server_now), usegmt=True))
        t += rtt
    assert abs(clock.offset() - true_offset) < 0.1 and clock.halfwidth() < 0.15
    assert clock.min_rtt < 0.2


def test_outliers_are_outvoted_and_widen_honestly():
    rng = random.Random(3)
    true_offset, clock, t = -0.7, ClockSync(), 2_000_000.0
    for i in range(60):
        t += rng.uniform(0.05, 0.3)
        rtt = rng.uniform(0.17, 0.25)
        stale = rng.uniform(0, 0.6) if i % 5 == 0 else 0  # like the real server: some stale stamps
        clock.add(t, t + rtt, formatdate(int(t + rng.uniform(0, rtt) + true_offset - stale), usegmt=True))
        t += rtt
    clock.add(t, t + 0.2, formatdate(int(t + 50), usegmt=True))  # wildly wrong sample
    assert abs(clock.offset() - true_offset) <= clock.halfwidth() + 0.02
    assert clock.halfwidth() < 0.35


def test_no_samples_falls_back():
    assert ClockSync(fallback=-0.4).offset() == -0.4 and ClockSync().halfwidth() == 0.5


@pytest.fixture(params=["noflag", "slow_roster"])
def server(request):
    # flag never flips, so the aimed shot must do it; slow_roster: each check takes 0.3 s
    srv = FakeIMediX(time.time() + 2.5, request.param)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def test_aimed_first_shot_lands_just_after_opening(server):
    clock = ClockSync()
    sync_clock(roster_url(server), "c", {"cmd": "opd-dates-current"}, clock, n=30, spread=0.05)
    server.open_at = time.time() + 1.5  # syncing takes ~10 s with slow replies; open after it
    assert abs(clock.offset()) <= clock.halfwidth() + 0.01  # same machine: true offset 0 is inside
    fire_at = lambda: server.open_at - clock.offset() + max(0.05, clock.halfwidth()) - clock.min_rtt / 2
    out = watch_and_strike(roster_url(server), "c", Q, "42093", FORM, fallback_at=fire_at,
                           deadline=server.open_at + 5, every=0.1, log=lambda m: None,
                           clock=clock)
    assert out["kind"] == "booked" and server.bookings == 1
    if server.mode == "noflag":  # fast replies: a tight clock, so no wasted early shot
        assert not [p for p in server.posts if p[0] < server.open_at]
        assert server.booked_at - server.open_at < 0.15
    else:  # 0.3 s replies: looser clock, still lands promptly and exactly once
        assert server.booked_at - server.open_at < 0.35


def test_stale_samples_first_do_not_anchor_the_estimate():
    rng = random.Random(11)
    true_offset, clock, t = -0.7, ClockSync(), 3_000_000.0
    for i in range(50):
        t += rng.uniform(0.05, 0.3)
        rtt = rng.uniform(0.17, 0.25)
        stale = 0.55 if i < 4 else 0  # the first few replies carry a stale clock stamp
        clock.add(t, t + rtt, formatdate(int(t + rng.uniform(0, rtt) + true_offset - stale), usegmt=True))
        t += rtt
    assert abs(clock.offset() - true_offset) <= clock.halfwidth() + 0.02
    assert clock.halfwidth() < 0.35
