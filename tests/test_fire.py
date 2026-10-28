import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


from kgp_opd.imedix import already_booked, classify, slip_shows_booking, watch_and_strike  # noqa: E402

BLOCKED = ("You have already booked an appoinment with Dr. Other Doctor.<br>Booking ID: "
           "01-26-00000000001<br>Please try after 12 AM.<br><br><a id='btn-print-booking-slip' "
           "class='btn btn-success' href='..\\/hmis_booking\\/booking_slip.jsp'>Print Booking Slip<\\/a>")
LOGGED_OUT = '<html><script type="text/javascript">top.location="../index.jsp";</script></html>'


class FakeIMediX(ThreadingHTTPServer):
    """Mimics processDutyRoster.jsp set-opd-appointment replies."""

    def __init__(self, open_at, mode="normal"):
        super().__init__(("127.0.0.1", 0), Handler)
        self.open_at, self.mode = open_at, mode
        self.lock = threading.Lock()
        self.booked_at = None
        self.bookings = 0  # the real server accepts duplicates, so count them
        self.connections = 0  # TCP connections opened (the real server keeps them alive)
        self.posts = []  # (time, body, cookie)
        self.first_after_open = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep connections alive like the real server

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def log_message(self, *a):
        pass

    def do_POST(self):
        s = self.server
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        t = time.time()
        with s.lock:
            s.posts.append((t, body, self.headers.get("Cookie")))
            slow = s.mode == "slow_first" and t >= s.open_at and s.first_after_open
            if t >= s.open_at:
                s.first_after_open = False
        if slow:
            time.sleep(2)  # server overloaded on this one request (it still books, late)
        elif s.mode == "slow_all":
            time.sleep(0.4)  # every booking reply takes a while, like the real server (1.3 s)
        if s.mode == "logged_out":
            return self.reply(LOGGED_OUT)
        if t < s.open_at:
            return self.reply(json.dumps({"status": "error",
                                          "message": "booking will start from <br>17 Oct 2026 08:00 AM"}))
        if s.mode == "full":
            return self.reply(json.dumps({"status": "error", "message": "Filled Up"}))
        with s.lock:  # like the real server: every booking request after opening succeeds
            s.bookings += 1
            if s.booked_at is None:
                s.booked_at = time.time()
            drop = s.mode == "drop_first" and s.bookings == 1
        if drop:
            self.close_connection = True
            return  # booked, but the reply is lost (connection closes with no response)
        self.reply(json.dumps({"status": "success", "message":
                               "Slot booked successfully.<br>Booking ID: 01-26-0000000000" + str(s.bookings)}))

    def do_GET(self):  # processDutyRoster.jsp?cmd=opd-roster: the server's own open flag
        s = self.server
        if s.mode == "logged_out":
            return self.reply(LOGGED_OUT)
        if s.mode == "slow_roster":
            time.sleep(0.3)  # the real roster takes ~0.35 s
        if s.mode == "bad_roster":  # an error page instead of JSON (possible under load)
            return self.reply("<html>Service busy</html>")
        if s.mode == "blocked":  # real reply after a booking that day (seen 16 Oct)
            return self.reply(json.dumps({"hour": "16", "status": "error", "message": BLOCKED}))
        status = "1" if time.time() >= s.open_at and s.mode not in ("noflag", "slow_roster") else "0"
        other = {"id": "42094", "session": "AN", "booking_status": "0"}
        self.reply(json.dumps({"status": "success", "data": [
            {"doc_id": "someone", "sessions": [{"id": "1", "session": "FN", "booking_status": "1"}]},
            {"doc_id": "doc1", "sessions": [{"id": "42093", "session": "FN",
                                                "booking_status": status}, other]}]}))

    def reply(self, text):
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def server(request):
    open_at = time.time() + 0.8
    srv = FakeIMediX(open_at, getattr(request, "param", "normal"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.mark.parametrize("status,text,kind", [
    (200, '{"status":"success","message":"Appointment set"}', "booked"),
    (200, '{"status":"error","message":"booking will start from <br>17 Oct"}', "retry"),
    (200, '{"status":"error","message":"booking not allowed yet"}', "retry"),
    (200, '{"status":"error","message":"Filled Up"}', "stop"),
    (200, '{"status":"error","message":"booking time is over"}', "stop"),
    (200, '{"status":"error","message":"You have already booked"}', "stop"),
    (200, LOGGED_OUT, "logged_out"),
    (502, "<html>Bad Gateway</html>", "retry"),
    (None, "ConnectionResetError", "retry"),
])
def test_classify(status, text, kind):
    assert classify(status, text)[0] == kind


def roster_url(srv):
    return f"http://127.0.0.1:{srv.server_address[1]}/iMediX/jspfiles/processDutyRoster.jsp"


Q = {"cmd": "opd-roster", "ccode": "TEST001", "rdate": "2026-10-17"}
FORM = {"cmd": "set-opd-appointment", "slot_id": "42093", "pat_id": "P1", "clinic_id": "13"}


def strike(srv, fallback_at, deadline_after=5, url=None):
    return watch_and_strike(url or roster_url(srv), "JSESSIONID=abc", Q, "42093", FORM,
                            fallback_at=fallback_at, deadline=srv.open_at + deadline_after,
                            every=0.1, log=lambda m: None)


def test_strikes_the_moment_the_flag_flips(server):
    # Server opens a full second before our own clock says so ("late in the 59th minute").
    out = strike(server, fallback_at=server.open_at + 1.0)
    assert out["state"] == "open" and out["kind"] == "booked"
    assert server.booked_at - server.open_at < 0.3  # hot connection, no new handshake
    # Timing record: the last "closed" check went out before opening, the flip was seen soon after.
    assert out["last_closed_sent"] < server.open_at <= out["flag_seen"] < server.open_at + 0.3
    assert not [p for p in server.posts if p[0] < server.open_at]  # never posted before it opened
    assert all(c == "JSESSIONID=abc" and "slot_id=42093" in b for _, b, c in server.posts)


@pytest.mark.parametrize("server", ["noflag"], indirect=True)
def test_fallback_keeps_striking_on_hot_connections(server):
    # Flag never flips; we fire 0.3 s early, get "will start", and keep going until it opens.
    out = strike(server, fallback_at=server.open_at - 0.3)
    assert out["state"] == "fallback" and out["kind"] == "booked"
    assert server.booked_at - server.open_at < 0.35
    assert [p for p in server.posts if p[0] < server.open_at], "early shots were rejected"


@pytest.mark.parametrize("server", ["slow_first"], indirect=True)
def test_slow_reply_is_awaited_not_duplicated(server):
    # Under load the first accepted request answers slowly. Firing again would book twice
    # (the real server allows that), so we wait for the answer instead.
    out = strike(server, fallback_at=server.open_at + 1.0)
    time.sleep(2.5)  # let the slow request land before counting
    assert out["kind"] == "booked" and server.bookings == 1


@pytest.mark.parametrize("server", ["full"], indirect=True)
def test_stops_when_full(server):
    out = strike(server, fallback_at=server.open_at + 1.0)
    assert out["kind"] == "stop" and "Filled" in out["msg"]
    assert len(server.posts) <= 2


@pytest.mark.parametrize("server", ["logged_out"], indirect=True)
def test_watch_reports_logged_out(server):
    out = strike(server, fallback_at=time.time() + 2)
    assert out["kind"] == "logged_out"
    assert not server.posts


def test_gives_up_at_deadline_when_server_unreachable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens here now
    t0 = time.time()
    out = watch_and_strike(f"http://127.0.0.1:{port}/x", "c", Q, "42093", FORM, fallback_at=t0,
                           deadline=t0 + 1.0, every=0.1, timeout=1, log=lambda m: None)
    assert out["kind"] == "gave_up"
    assert time.time() - t0 < 3


def test_trace_records_every_check_and_shot(server):
    lines = []
    out = watch_and_strike(roster_url(server), "JSESSIONID=abc", Q, "42093", FORM,
                           fallback_at=server.open_at + 1.0, deadline=server.open_at + 5,
                           every=0.1, log=lambda m: None,
                           trace=lambda what, t0, t1, detail: lines.append((what, t0, t1, detail)))
    assert out["kind"] == "booked"
    checks = [x for x in lines if "roster check" in x[0]]
    shots = [x for x in lines if "booking shot" in x[0]]
    assert any(d == "closed" for *_, d in checks) and any(d == "open" for *_, d in checks)
    assert shots and "booked" in shots[0][3] and all(t0 <= t1 for _, t0, t1, _ in lines)
    assert {w.split()[0] for w, *_ in checks} == {"watcher0", "watcher1"}


@pytest.mark.parametrize("server", ["normal", "noflag", "slow_all"], indirect=True)
def test_books_exactly_once(server):
    strike(server, fallback_at=server.open_at - 0.3)
    time.sleep(1.0)  # let any stray request land
    assert server.bookings == 1


@pytest.mark.parametrize("server", ["slow_all"], indirect=True)
def test_only_one_booking_request_in_flight(server):
    starts, ends = [], []
    out = watch_and_strike(roster_url(server), "c", Q, "42093", FORM,
                           fallback_at=server.open_at - 0.6, deadline=server.open_at + 5,
                           every=0.1, log=lambda m: None,
                           trace=lambda what, t0, t1, d: (starts.append(t0), ends.append(t1))
                           if "booking shot" in what else None)
    assert out["kind"] == "booked"
    shots = sorted(zip(starts, ends))
    assert len(shots) >= 2 and all(a[1] <= b[0] for a, b in zip(shots, shots[1:]))


@pytest.mark.parametrize("server", ["drop_first"], indirect=True)
def test_lost_reply_is_checked_on_the_slip_not_refired(server):
    checks = []
    out = watch_and_strike(roster_url(server), "c", Q, "42093", FORM,
                           fallback_at=server.open_at + 1.0, deadline=server.open_at + 5,
                           every=0.1, log=lambda m: None,
                           confirm=lambda: checks.append(1) or server.bookings > 0)
    time.sleep(0.5)
    assert out["kind"] == "booked" and "slip" in out["msg"]
    assert checks and server.bookings == 1


SLIP = """<html><body><h3>BOOKING/MEDICINE SLIP</h3><table>
<tr><td>Patient:</td><td>Mallareddy Sri Shanth Reddy</td></tr><tr><td>Releation:</td><td>Self</td></tr>
<tr><td>HMIS Reg No:</td><td>01-26-00000000001</td></tr>
<tr><td>Doctor:</td><td>Other Doctor</td></tr>
<tr><td>Booking Time:</td><td>16/10/2026 04:19 PM</td></tr></table></body></html>"""


def test_slip_shows_booking():
    from datetime import datetime
    from kgp_opd.imedix import IST
    since = datetime(2026, 10, 16, 16, 0, tzinfo=IST)
    assert slip_shows_booking(SLIP, "Dr. Other Doctor", since)
    assert not slip_shows_booking(SLIP, "Dr. Test Doctor", since)  # other doctor
    assert not slip_shows_booking(SLIP, "Dr. Other Doctor",
                                  datetime(2026, 10, 17, 7, 55, tzinfo=IST))  # old booking
    assert not slip_shows_booking("<html>nothing</html>", "Dr. Other Doctor", since)


def test_pick_clinic_never_defaults_to_emergency():
    from kgp_opd.imedix import pick_clinic
    assert pick_clinic([("45", "Emergency"), ("31", "General Clinic")], None) == ("31", "General Clinic")
    assert pick_clinic([("13", "Medicine")], None) == ("13", "Medicine")
    assert pick_clinic([("45", "Emergency")], None) is None  # better to stop than book Emergency
    assert pick_clinic([("45", "Emergency"), ("31", "General Clinic")], "emerg") == ("45", "Emergency")


@pytest.mark.parametrize("server", ["blocked"], indirect=True)
def test_blocked_roster_is_reported_not_crashed(server):
    out = strike(server, fallback_at=time.time() + 3)
    assert out["kind"] == "already" and "Other Doctor" in out["msg"]
    assert not server.posts  # nothing fired


def test_already_booked_interpretation():
    msg = "You have already booked an appoinment with Dr. Test Doctor. Booking ID: 01-26-1 Please try after 12 AM."
    assert already_booked(msg, "Dr. Test Doctor") == "ours"
    assert already_booked(msg.replace("Test Doctor", "Other Doctor"), "Dr. Test Doctor") == "other"
    assert already_booked("booking will start from 17 Oct 2026 08:00 AM", "Dr. Test Doctor") is None
    assert classify(200, '{"status":"error","message":"' + msg + '"}')[0] == "stop"


@pytest.mark.parametrize("server", ["bad_roster"], indirect=True)
def test_unreadable_replies_keep_the_hot_connection(server):
    out = strike(server, fallback_at=server.open_at + 0.05)
    assert out["kind"] == "booked" and server.bookings == 1
    assert server.booked_at - server.open_at < 0.2  # fired on time on a hot connection
    assert server.connections <= 2  # one per watcher, no reconnect after unreadable replies
