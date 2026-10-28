"""Talking to iMediX (B.C. Roy Technology Hospital, IIT Kharagpur): login, the duty roster,
clinic choice, the server clock, and the timed booking itself.

The booking opens 60 min before an OPD session. From WATCH_FROM seconds before, two
watchers check the roster's own "open" flag (it flips in the last second) and book the
moment it does, on connections that are already open. The server accepts duplicate
bookings, so only one booking request is ever in flight: a "not yet" reply lets the next
shot go shortly after; a slow reply is waited for; an unclear one is checked on the
booking slip before firing again. Only the final confirm request is sent; the clinic id
is fetched in advance.
"""

import html as htmllib
import http.client
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from bcroy_booker import system

BASE = "https://imedixbcr.iitkgp.ac.in/iMediX/"
ROSTER = BASE + "jspfiles/processDutyRoster.jsp"
# India has no daylight saving, so a fixed offset is exact (and needs no tz database,
# which Windows Python lacks).
IST = timezone(timedelta(hours=5, minutes=30), "IST")

# Slots are gone within ~2 minutes and other students run bots too, so: start knocking just
# before the server's opening second, keep several requests in flight so one slow reply
# never holds things up, and stop at the first definite answer.
WATCH_FROM = 20  # watch the server's own "open" flag from this many seconds before opening
                 # (it flips in the last second; 20 s keeps each user's load on the server low)
WATCH_EVERY = 0.5  # seconds between roster checks while watching
FIRE_EARLY = 0.3  # old tested timing; the aimed shot stays within FIRE_CLAMP of it
FIRE_CLAMP = 0.5
AIM_AFTER = 0.05  # aim the first shot to reach the server this long after opening
STRIKE_GAP = 0.1  # min seconds between booking shots (after a definite "not yet")
SHOT_TIMEOUT = 30  # wait this long for a booking reply: the server books duplicates, so
                   # never fire again while one may still be landing
RETRY_FOR = 120  # keep trying this long after opening
STOP_WORDS = re.compile(r"already|filled|full|time is over|no (slot|seat)", re.I)


LOG_LISTENERS = []  # e.g. the app window, which shows every log line


def log(msg):
    line = f"[{datetime.now(IST):%H:%M:%S.%f}"[:-3] + f"] {msg}"
    print(line, flush=True)
    with (system.home() / "bot_log.txt").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    for listener in LOG_LISTENERS:
        listener(line)


def notify(msg):
    system.notify(msg)


class AmbiguousDoctor(RuntimeError):
    pass


def session_expired(text):
    return 'top.location="../index.jsp"' in text


def get_json(ctx, params):
    r = ctx.request.get(ROSTER, params=params)
    text = r.text()
    if session_expired(text):
        raise RuntimeError("logged out (session expired)")
    res = json.loads(text.strip())
    if "data" not in res:  # e.g. "You have already booked an appoinment with Dr. X ... try after 12 AM"
        raise RuntimeError(re.sub(r"<[^>]+>", " ", str(res.get("message", res))).strip())
    return res


def already_booked(msg, docname):
    """The server allows one booking per patient per day. For its "You have already booked an
    appoinment with Dr. X" reply: "ours" if X is our doctor, "other" if not, None otherwise."""
    # (sic: the server writes "appoinment"; the name may be followed by ".<br>" or ". Booking")
    m = re.search(r"already booked an \w+ with (?:dr\.?\s*)?(.+?)\.\s*(?:<br>|booking|please|$)",
                  msg, re.I)
    if not m:
        return None
    norm = lambda n: re.sub(r"^dr\.?\s*", "", n.strip().lower())
    return "ours" if norm(m.group(1)) == norm(docname) else "other"


def login(page, interactive=True, force_manual=False):
    """Log in and return the patient id. Uses the saved login; with none saved (or when it
    fails) waits for you to log in by hand in the browser window and saves what you type
    in the system password store (Keychain / Credential Manager)."""
    page.goto(BASE)
    creds = None if force_manual else system.load_creds()
    typed = {}

    def grab(req):  # remember what you type so it can be saved
        if req.method == "POST" and req.url.endswith("login_n.jsp"):
            form = parse_qs(req.post_data or "")
            typed.update(uid=form.get("uid", [""])[0], pwd=form.get("pwd", [""])[0])

    page.on("request", grab)
    logged_in = re.compile(r"jspfiles/patient")
    try:
        if creds:
            log(f"Logging in as {creds[0]} (saved login)")
            page.fill("#uid", creds[0])
            page.fill("#pwd", creds[1])
            page.wait_for_function("typeof cd === 'string' && cd.length > 0")
            page.fill("#UserCaptchaCode", page.evaluate("removeSpaces(cd)"))
            page.click("input[type=submit][value=Login]")
            try:
                page.wait_for_url(logged_in, timeout=20000)
            except Exception:
                log("Saved login did not work (password changed?).")
                notify("Saved login failed - run: bcroy-book setup")
                creds = None
        if not creds:
            if not interactive:
                raise RuntimeError("no working saved login; run `bcroy-book setup` first")
            log("Log in in the browser window (roll no, password, captcha). Waiting...")
            page.wait_for_url(logged_in, timeout=0)
            if typed.get("uid") and typed.get("pwd"):
                system.save_creds(typed["uid"], typed["pwd"])
                log("Saved your login in this computer's password store; next time is automatic.")
    finally:
        page.remove_listener("request", grab)
    page.wait_for_load_state("domcontentloaded")
    # "Relationship With Primary Account Holder" popup -> pick Self.
    try:
        page.get_by_text("Self", exact=True).first.click(timeout=4000)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1500)
    except Exception:
        pass
    # Clicking Self reloads the page; read it once it has settled (retry, don't crash).
    for _ in range(10):
        try:
            page.wait_for_load_state("domcontentloaded")
            m = re.search(r'pat_id:\s*"([^"]+)"', page.content())
            if m:
                log("Logged in.")
                return m.group(1)
        except Exception:
            pass
        page.wait_for_timeout(1000)
    raise RuntimeError("could not find patient id on the page")


def get_ccode(ctx, pat_id):
    text = ctx.request.get(BASE + "jspfiles/patient_show_roster.jsp").text()
    m = re.search(r'ccode:\s*"([^"]+)"', text)
    return m.group(1) if m else pat_id[:7]


class ClockSync:
    """Server-minus-local clock offset from HTTP Date headers (1 s resolution).

    Each reply says the server's clock read S..S+1 at some instant between our send and
    receive, i.e. offset in (S - t_recv, S + 1 - t_send). This server's stamps are not fully
    consistent (some look stale by up to ~0.6 s; measured 16 Oct), so instead of requiring
    every reply to agree we take the offsets that (nearly) the most replies agree on, and
    report that range's half-width as the honest uncertainty (about +-0.2 s here)."""

    def __init__(self, fallback=0.0):
        self.iv, self.min_rtt, self.fallback = [], float("inf"), fallback
        self.lock = threading.Lock()
        self._cache = (None, None)

    @property
    def n(self):
        return len(self.iv)

    def add(self, t_send, t_recv, date_header):
        try:
            s = parsedate_to_datetime(date_header).timestamp()
        except (TypeError, ValueError, IndexError):
            return
        with self.lock:
            self.iv.append((s - t_recv, s + 1 - t_send))
            self.min_rtt = min(self.min_rtt, t_recv - t_send)

    def _region(self):
        """(lo, hi, agree, total) of the offsets agreed by (nearly) the most replies."""
        with self.lock:
            iv = list(self.iv)
        if not iv:
            return None
        if self._cache[0] == len(iv):
            return self._cache[1]
        grid_lo, grid_hi = min(lo for lo, _ in iv), max(hi for _, hi in iv)
        steps = min(2000, int((grid_hi - grid_lo) / 0.005) + 1)
        xs = [grid_lo + i * (grid_hi - grid_lo) / max(1, steps - 1) for i in range(steps)]
        cover = [sum(lo <= x <= hi for lo, hi in iv) for x in xs]
        best = max(cover)
        tol = max(1, round(0.05 * len(iv)))  # allow a few stale stamps
        good = [x for x, c in zip(xs, cover) if c >= best - tol]
        region = (min(good), max(good), best, len(iv))
        self._cache = (len(iv), region)
        return region

    def offset(self):
        r = self._region()
        return (r[0] + r[1]) / 2 if r and r[1] - r[0] < 2 else self.fallback

    def halfwidth(self):
        r = self._region()
        return max(0.02, (r[1] - r[0]) / 2) if r and r[1] - r[0] < 2 else 0.5

    def describe(self):
        r = self._region()
        agree = f", {r[2]}/{r[3]} replies agree" if r else ", no samples"
        return (f"{self.offset():+.3f}s vs this Mac (\u00b1{self.halfwidth():.3f}s{agree}, "
                f"min round trip {self.min_rtt:.3f}s)")


def sync_clock(url, cookie, query, clock, n=30, spread=0.15, timeout=10):
    """Feed `clock` from `n` quick GETs on one kept-alive connection, at varied phases."""
    u = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(u.netloc, timeout=timeout)
    path = f"{u.path}?{urlencode(query)}"
    try:
        conn.connect()
        for _ in range(n):
            t0 = time.time()
            conn.request("GET", path, headers={"Cookie": cookie, "X-Requested-With": "XMLHttpRequest"})
            r = conn.getresponse()
            r.read()
            clock.add(t0, time.time(), r.getheader("Date"))
            time.sleep(spread * (0.3 + 0.7 * (time.time() * 7919 % 1)))  # vary the phase
    except (http.client.HTTPException, OSError):
        pass
    finally:
        conn.close()
    return clock


def _norm_name(name):
    name = re.sub(r"^\s*dr\.?\s*", "", (name or "").lower())
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", name).split())


def doctor_matches(doc, query):
    """`query` is a doctor id ("doc_id" in the roster) or part of the name, any case."""
    q = query.strip().lower()
    return doc.get("doc_id", "").lower() == q or bool(_norm_name(query)) and \
        _norm_name(query) in _norm_name(doc.get("docname"))


def roster_sessions(ctx, ccode, want_date=None):
    """Every OPD session in the current roster: dicts with the roster's own fields plus
    docname, doc_id, start and opens (when booking opens), sorted by start."""
    dates = get_json(ctx, {"cmd": "opd-dates-current", "ccode": ccode})["data"]
    if want_date:
        dates = [d for d in dates if d == want_date]
    out = []
    for d in dates:
        for doc in get_json(ctx, {"cmd": "opd-roster", "ccode": ccode, "rdate": d})["data"]:
            for s in doc["sessions"]:
                try:
                    start = datetime.strptime(s["start_time"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                    opens = start - timedelta(minutes=int(s["booking_start_before"]))
                except (KeyError, ValueError, TypeError):
                    continue
                out.append(dict(s, start=start, opens=opens, docname=doc.get("docname", "?"),
                                doc_id=doc.get("doc_id", "")))
    return sorted(out, key=lambda s: (s["start"], s["docname"]))


def bookable(s):
    return (s.get("available") == "1" and s.get("booking_status") != "2"
            and int(s.get("patients") or 0) < int(s.get("max_no_of_patients") or 0))


def find_session(ctx, ccode, want_date, want_session, doctor):
    """The doctor's session whose booking opens soonest and still has places, or None.
    Raises AmbiguousDoctor when `doctor` matches more than one doctor's name."""
    sessions = [s for s in roster_sessions(ctx, ccode, want_date) if doctor_matches(s, doctor)]
    names = sorted({(s["doc_id"], s["docname"]) for s in sessions})
    if len(names) > 1:
        raise AmbiguousDoctor(f"{doctor!r} matches several doctors: "
                              + "; ".join(f"{n} (id {i})" for i, n in names)
                              + ". Use more of the name, or the id.")
    best = None
    for s in sessions:
        if not bookable(s) or (want_session and s["session"] != want_session):
            continue
        if best is None or s["opens"] < best["opens"]:
            best = s
    return best


def clinic_options(ctx, slot_id):
    text = ctx.request.post(BASE + "hmis_booking/select_clinic.jsp", form={"slot_id": slot_id}).text()
    if session_expired(text):
        raise RuntimeError("logged out (session expired)")
    opts = re.findall(r"<option[^>]*value\s*=\s*[\"']?([^\"'>]*)[\"']?[^>]*>(.*?)</option>", text, re.S | re.I)
    opts = [(v.strip(), htmllib.unescape(re.sub(r"<[^>]+>", "", t)).strip()) for v, t in opts]
    return [(v, t) for v, t in opts if v and v.lower() != "select"], text


def pick_clinic(opts, want):
    if want:
        for v, t in opts:
            if want.lower() in t.lower() or want == v:
                return v, t
        return None
    # Default: first clinic that is not Emergency (it is listed first for many doctors).
    return next(((v, t) for v, t in opts if "emergency" not in t.lower()), None)


def slip_shows_booking(slip_html, docname, since):
    """True if the booking slip shows a booking with `docname` made at/after `since`."""
    text = " ".join(re.sub(r"<[^>]+>", " ", slip_html).split())
    m = re.search(r"Doctor:\s*(.*?)\s*Booking Time:\s*(\d\d/\d\d/\d{4} \d\d:\d\d [AP]M)", text)
    if not m:
        return False
    norm = lambda n: re.sub(r"^dr\.?\s*", "", n.strip().lower())
    booked_at = datetime.strptime(m.group(2), "%d/%m/%Y %I:%M %p").replace(tzinfo=IST)
    return norm(m.group(1)) == norm(docname) and booked_at >= since.replace(second=0, microsecond=0)


def fetch_slip(cookie, timeout=15):
    u = urlsplit(BASE)
    try:
        c = http.client.HTTPSConnection(u.netloc, timeout=timeout)
        c.request("GET", u.path + "hmis_booking/booking_slip.jsp", headers={"Cookie": cookie})
        return c.getresponse().read().decode("utf-8", "replace")
    except (http.client.HTTPException, OSError):
        return ""


def definite(status, text):
    """A reply we can trust: HTTP 200 with JSON. Anything else may hide a booking that landed."""
    if status != 200:
        return False
    try:
        json.loads(text.strip())
        return True
    except ValueError:
        return session_expired(text)


def classify(status, text):
    """One booking reply -> (kind, message); kind is booked / stop / logged_out / retry."""
    if status is None:
        return "retry", text
    if session_expired(text):
        return "logged_out", "logged out"
    try:
        res = json.loads(text.strip())
    except ValueError:
        return "retry", f"HTTP {status}: {re.sub(r'<[^>]+>', ' ', text).strip()[:120]}"
    msg = re.sub(r"<[^>]+>", " ", str(res.get("message", ""))).strip()
    if res.get("status") == "success":
        return "booked", msg
    if STOP_WORDS.search(msg):
        return "stop", msg
    return "retry", msg


def watch_and_strike(url, cookie, query, slot_id, form, fallback_at, deadline, every=WATCH_EVERY,
                     strike_gap=STRIKE_GAP, workers=2, timeout=6, shot_timeout=SHOT_TIMEOUT,
                     log=log, trace=lambda what, t0, t1, detail: None, confirm=lambda: False,
                     clock=None):
    """Watch the roster until the server marks `slot_id` bookable (booking_status 1), then
    book on the same already-open connection, retrying until a definite answer or `deadline`.
    If the flag has not flipped by `fallback_at` (local clock; may be a callable that re-aims as
    `clock` learns from each roster reply's Date header), start booking then anyway.

    The server accepts duplicate bookings, so only ONE booking request is ever in flight, and
    after an unclear reply (none, an error page) `confirm()` (the booking slip) is asked before
    firing again.

    `workers` staggered watchers on their own connections, so one slow reply (the roster
    takes 0.4-0.8 s normally, more under load) never blinds or blocks us. Returns
    {state: open/over/fallback/watch, kind: booked/stop/logged_out/gave_up, msg, attempts}."""
    u = urlsplit(url)
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    path = f"{u.path}?{urlencode(query)}"
    headers = {"Cookie": cookie, "X-Requested-With": "XMLHttpRequest"}
    post_headers = dict(headers, **{"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    body = urlencode(form)
    done, lock = threading.Event(), threading.Lock()
    shot_gate, last_shot = threading.Lock(), {"end": 0.0}
    blocked = {}
    result = {"state": "fallback", "kind": "gave_up",
              "msg": "no definite answer before the deadline", "attempts": 0,
              "last_closed_sent": None, "flag_seen": None}  # local times, for the log

    def finish(**kw):
        with lock:
            if not done.is_set():
                result.update(kw)
                done.set()

    fire_time = fallback_at if callable(fallback_at) else (lambda: fallback_at)

    def flag(conn):
        t0 = time.time()
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        if clock:
            clock.add(t0, time.time(), r.getheader("Date"))
        if session_expired(text):
            return "logged_out"
        res = json.loads(text.strip())
        if "data" not in res and "already booked" in str(res.get("message", "")).lower():
            blocked["msg"] = re.sub(r"<[^>]+>", " ", res["message"]).strip()
            return "already"
        for doc in res["data"]:
            for ses in doc["sessions"]:
                if str(ses["id"]) == str(slot_id):
                    return {"1": "open", "2": "over"}.get(ses["booking_status"])
        return None

    def post(conn):
        """One booking request; never re-sent here (a re-send could book twice)."""
        try:
            conn = conn or conn_cls(u.netloc, timeout=timeout)
            if conn.sock:
                conn.sock.settimeout(shot_timeout)
            conn.timeout = shot_timeout
            conn.request("POST", u.path, body, post_headers)
            r = conn.getresponse()
            return conn, r.status, r.read().decode("utf-8", "replace")
        except (http.client.HTTPException, OSError) as e:
            if conn:
                conn.close()
            return None, None, f"{type(e).__name__}: {e}"

    def strike(k, conn, state, delay=0.0):
        time.sleep(delay)
        while not done.is_set() and time.time() < deadline:
            with shot_gate:  # one booking request in flight at a time
                if done.is_set() or time.time() >= deadline:
                    return
                time.sleep(max(0, last_shot["end"] + strike_gap - time.time()))
                t = time.time()
                conn, status, text = post(conn)
                last_shot["end"] = time.time()
                with lock:
                    result["attempts"] += 1
                    n = result["attempts"]
                kind, msg = classify(status, text)
                trace(f"watcher{k} booking shot {n} ({state})", t, time.time(),
                      f"HTTP {status}: {kind} - {msg}")
                log(f"attempt {n} ({state}): {kind} - {msg}")
                if kind != "retry":
                    return finish(state=state, kind=kind, msg=msg)
                if not definite(status, text):  # it may have booked anyway: look before firing again
                    t = time.time()
                    booked = confirm()
                    trace(f"watcher{k} slip check", t, time.time(),
                          "booking found" if booked else "no booking")
                    if booked:
                        return finish(state=state, kind="booked",
                                      msg="confirmed on the booking slip after an unclear reply")
            time.sleep(0.001)  # let the other watcher take the next shot

    def watcher(k):
        # Checks run on a fixed time grid, watcher k offset by k/workers of a period. Sleeping
        # "`every` after each check" instead lets late wake-ups add up (measured ~60 ms each
        # under launchd) and drift the watchers into step, doubling the blind gap.
        g0 = grid_start + k * every / workers
        time.sleep(max(0, g0 - time.time()))
        conn, last_check = None, 0.0
        while not done.is_set():
            t, fa = time.time(), fire_time()
            if t >= fa:
                if clock:
                    trace(f"watcher{k} aimed shot due", fa, t, f"clock {clock.describe()}")
                return strike(k, conn, "fallback", delay=k * strike_gap)
            if fa - t < last_check + 0.02:  # a check now would still be running at shot time
                time.sleep(min(max(0, fa - time.time()), 0.05))
                continue
            try:
                conn = conn or conn_cls(u.netloc, timeout=timeout)
                state = flag(conn)
                trace(f"watcher{k} roster check", t, time.time(), state or "closed")
                with lock:
                    if state is None:
                        result["last_closed_sent"] = max(t, result["last_closed_sent"] or 0)
                    elif result["flag_seen"] is None:
                        result["flag_seen"] = time.time()
                if state == "logged_out":
                    return finish(state="watch", kind="logged_out", msg="logged out")
                if state == "already":  # a booking already exists today (ours or another)
                    return finish(state="watch", kind="already", msg=blocked["msg"])
                if state:
                    log(f"roster flag: {state}")
                    return strike(k, conn, state)
            except (http.client.HTTPException, OSError) as e:  # network trouble: reconnect
                trace(f"watcher{k} roster check", t, time.time(), f"error {type(e).__name__}")
                if not done.is_set():
                    log(f"roster check failed: {type(e).__name__}")
                if conn:
                    conn.close()
                conn = None
            except (ValueError, KeyError, TypeError) as e:  # unreadable reply: keep the hot connection
                trace(f"watcher{k} roster check", t, time.time(), f"unreadable {type(e).__name__}")
            finally:
                last_check = time.time() - t  # don't start a check that would run past shot time
            nxt = g0 + (round((t - g0) / every) + 1) * every  # the grid point after this check's
            time.sleep(max(0, min(nxt - time.time(), fire_time() - time.time())))

    grid_start = time.time()
    threads = [threading.Thread(target=watcher, args=(k,), daemon=True) for k in range(workers)]
    for th in threads:
        th.start()
    # Until a definite answer, or both watchers stopped at the deadline (a hung request is
    # abandoned after `timeout`).
    while not done.wait(0.05):
        if not any(th.is_alive() for th in threads) or time.time() > deadline + timeout + 1:
            break
    with lock:
        return dict(result)
