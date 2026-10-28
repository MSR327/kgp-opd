"""Command line: `kgp-opd setup | doctors | book | schedule | unschedule | forget`."""

import argparse
import sys
import threading
import time
from datetime import datetime, timedelta

from kgp_opd import __version__, system
from kgp_opd.imedix import (AIM_AFTER, BASE, FIRE_CLAMP, FIRE_EARLY, IST, RETRY_FOR, ROSTER,
                                 WATCH_FROM, AmbiguousDoctor, ClockSync, already_booked, bookable,
                                 clinic_options, doctor_matches, fetch_slip, find_session, get_ccode,
                                 get_json, log, login, notify, pick_clinic, roster_sessions,
                                 slip_shows_booking, sync_clock, watch_and_strike)

PREPARE_TRIES = 3  # login / roster hiccups before anything is sent are retried


def open_browser(p, headless):
    """Google Chrome if installed, else Microsoft Edge (always there on Windows), else
    Playwright's own Chromium."""
    profile = str(system.home() / "browser_profile")
    last = None
    for channel in ("chrome", "msedge", None):
        try:
            kw = {"channel": channel} if channel else {}
            return p.chromium.launch_persistent_context(profile, headless=headless,
                                                        no_viewport=True, **kw)
        except Exception as e:
            last = e
    raise RuntimeError("could not start a browser. Install Google Chrome, or run: "
                       f"python -m playwright install chromium ({str(last).splitlines()[0]})")


def cookie_header(ctx):
    return "; ".join(f"{c['name']}={c['value']}" for c in ctx.cookies(BASE))


def _session_name(s):
    return "Forenoon" if s.get("session") == "FN" else "Afternoon"


def status(s, now):
    if s.get("available") != "1":
        return "not available"
    if s.get("booking_status") == "2":
        return "booking over"
    if int(s.get("patients") or 0) >= int(s.get("max_no_of_patients") or 0):
        return "full"
    if s.get("booking_status") == "1":
        return "OPEN now"
    return f"opens {s['opens']:%a %d %b %H:%M}" if s["opens"] > now else "open"


# ── commands ──

def cmd_setup(args):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = open_browser(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        login(page, interactive=True, force_manual=True)
        ctx.close()
    if system.load_creds():
        print("\nDone: your login is saved. Next, find your doctor:  kgp-opd doctors")
        return 0
    print("\nLogged in, but the login could not be captured. Run setup again.")
    return 1


def fetch_sessions(date=None):
    """Every session in the current roster (logs in with the saved login)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        ctx = open_browser(p, headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ccode = get_ccode(ctx, login(page, interactive=False))
        sessions = roster_sessions(ctx, ccode, date)
        ctx.close()
    return sessions


def cmd_doctors(args):
    sessions = fetch_sessions(args.date)
    if args.name:
        sessions = [s for s in sessions if doctor_matches(s, args.name)]
    if not sessions:
        print("No sessions in the current roster" + (f" for {args.name!r}" if args.name else "") + ".")
        return 0
    now = datetime.now(IST)
    print(f"{'When':<22} {'Doctor':<34} {'Id':<14} {'Booked':<8} Status")
    for s in sessions:
        when = f"{s['start']:%a %d %b %H:%M} {s.get('session', '')}"
        print(f"{when:<22} {s['docname'][:33]:<34} {s['doc_id'][:13]:<14} "
              f"{s.get('patients', '?')}/{s.get('max_no_of_patients', '?'):<6} {status(s, now)}")
    print("\nBook with:  kgp-opd book --doctor \"<part of the name, or the id>\"")
    return 0


def prepare(ctx, page, args):
    """Everything before the timed part: login, target session, clinic, server clock.
    Returns None when there is nothing to book."""
    pat_id = login(page, interactive=not args.headless)
    ccode = get_ccode(ctx, pat_id)
    target = find_session(ctx, ccode, args.date, args.session, args.doctor)
    if not target:
        return None
    log(f"Target: {target['docname']} {target['start']:%a %d %b %H:%M} ({_session_name(target)}), "
        f"slot id {target['id']}, {target['patients']}/{target['max_no_of_patients']} booked, "
        f"booking opens {target['opens']:%a %H:%M:%S}")
    opts, raw = clinic_options(ctx, target["id"])
    log(f"Clinic options: {opts if opts else 'none parsed yet'}")
    clinic = pick_clinic(opts, args.clinic)
    if args.clinic and not clinic:
        log(f"No clinic matches --clinic {args.clinic!r}")
    clock = sync_clock(ROSTER, cookie_header(ctx), {"cmd": "opd-dates-current", "ccode": ccode},
                       ClockSync())
    log(f"Server clock is {clock.describe()}")
    return dict(pat_id=pat_id, ccode=ccode, target=target, clinic=clinic, clock=clock,
                prepared_at=time.time())


def cmd_book(args):
    from playwright.sync_api import sync_playwright
    try:
        with system.KeepAwake(), sync_playwright() as p:
            system.wait_for_network(BASE, log=log)
            ctx = open_browser(p, args.headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            prep = "error"
            for attempt in range(1, PREPARE_TRIES + 1):
                try:
                    prep = prepare(ctx, page, args)
                    break
                except AmbiguousDoctor as e:
                    log(str(e))
                    return 2
                except Exception as e:  # nothing has been sent yet, so retrying is safe
                    log(f"Setup failed (try {attempt}/{PREPARE_TRIES}): {type(e).__name__}: "
                        f"{str(e).splitlines()[0][:160] if str(e) else ''}")
                    if attempt < PREPARE_TRIES:
                        page.wait_for_timeout(15_000)
            if prep == "error":
                notify("Could not get ready to book - see bot_log.txt")
                return 1
            if prep is None:
                log(f"No upcoming bookable session for {args.doctor!r} in the roster. Nothing to do.")
                notify("No upcoming session found")
                return 0
            if getattr(args, "on_target", None):  # e.g. the app window's countdown
                args.on_target(prep["target"], prep["clock"].offset())
            return run_booking(ctx, page, args, **prep)
    finally:
        if args.scheduled_job:  # last step of a scheduled run: remove the job (ends this process)
            system.unschedule(args.scheduled_job)


def run_booking(ctx, page, args, pat_id, ccode, target, clinic, clock, prepared_at):
    slot_id = target["id"]
    offset = clock.offset()

    if args.dry_run:
        # Exercise the booking fast path with a harmless request instead of the booking.
        out = watch_and_strike(ROSTER, cookie_header(ctx), {"cmd": "opd-dates-current", "ccode": ccode},
                               slot_id, {"cmd": "opd-dates-current", "ccode": ccode},
                               fallback_at=time.time(), deadline=time.time() + 10,
                               log=lambda m: None)
        ok = out["kind"] == "booked"
        log("DRY RUN: fast path " + ("OK (logged-in request went through)" if ok else f"FAILED: {out}"))
        log(f"DRY RUN: would book slot {slot_id} with clinic {clinic}. Not booking.")
        return 0 if ok else 1

    now = lambda: time.time() + offset
    opens_at = target["opens"].timestamp()
    if args.rehearse:
        opens_at = now() + args.rehearse
        log(f"REHEARSAL: pretending booking opens in {args.rehearse}s; nothing will be booked")

    # Wait until watching starts. Re-check login + clinic list ~15 s before that.
    watch_at = opens_at - WATCH_FROM  # server clock
    rechecked = False
    while now() < watch_at - 1:
        left = watch_at - now()
        if left < 15 and not rechecked:
            rechecked = True
            try:
                opts, _ = clinic_options(ctx, slot_id)
                clinic = pick_clinic(opts, args.clinic) or clinic
                log(f"T-{left + WATCH_FROM:.0f}s: still logged in. Clinic: {clinic}")
            except RuntimeError as e:
                log(f"T-{left + WATCH_FROM:.0f}s: {e}. Logging in again.")
                pat_id = login(page, interactive=not args.headless)
            if time.time() - prepared_at > 20 * 60:  # started long ago: measure the clock afresh
                clock = sync_clock(ROSTER, cookie_header(ctx),
                                   {"cmd": "opd-dates-current", "ccode": ccode}, ClockSync())
                log(f"Server clock re-measured: {clock.describe()}")
        if left > 120:
            log(f"Waiting... booking opens in {(left + WATCH_FROM) / 60:.1f} min")
            page.wait_for_timeout(min(600 if left > 900 else 60, left - 100) * 1000)
        else:
            page.wait_for_timeout(max(0, min(left - 14 if not rechecked else left - 1, 60)) * 1000)

    if not clinic:
        opts, _ = clinic_options(ctx, slot_id)
        clinic = pick_clinic(opts, args.clinic)
        log(f"Clinic options at opening: {opts}")
    if not clinic:
        log("No clinic option available; cannot book. Book by hand on the website!")
        notify("No clinic option - book by hand!")
        return 1

    form = {"cmd": "set-opd-appointment", "slot_id": slot_id, "pat_id": pat_id,
            "clinic_id": clinic[0]}
    trial = bool(args.rehearse or args.observe)
    if trial:
        form = {"cmd": "opd-dates-current", "ccode": ccode}  # harmless stand-in
        log("OBSERVE/REHEARSAL: a harmless request stands in for the booking")
    base_fire = opens_at - offset - FIRE_EARLY  # the timing tested live

    def fire_at():  # local time to send the aimed shot; re-aims as the watchers refine the clock
        aimed = (opens_at - clock.offset() + max(AIM_AFTER, clock.halfwidth())
                 - min(clock.min_rtt, 0.4) / 2)
        return min(max(aimed, base_fire - FIRE_CLAMP), base_fire + FIRE_CLAMP)

    deadline = max(opens_at - offset, time.time()) + RETRY_FOR
    query = {"cmd": "opd-roster", "ccode": ccode, "rdate": f"{target['start']:%Y-%m-%d}"}
    while now() < watch_at:
        time.sleep(0.01)
    log(f"Watching the roster for slot {slot_id} (clinic {clinic[1]!r}); booking the moment "
        f"it opens, or with a shot aimed to land {AIM_AFTER}s after {target['opens']:%H:%M:%S}")
    trace_path = system.home() / "trace.txt"
    trace_lock = threading.Lock()

    def trace(what, t0, t1, detail):
        if trial:
            detail = detail.replace(": booked", ": accepted (stand-in)")
        stamp = lambda t: f"{datetime.fromtimestamp(t, IST):%H:%M:%S.%f}"[:-3]
        rel = lambda t: f"{t + offset - opens_at:+.3f}s"
        with trace_lock, trace_path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp(t0)} -> {stamp(t1)}  (server {rel(t0)} -> {rel(t1)}, "
                    f"{t1 - t0:.3f}s)  {what}: {detail}\n")

    with trace_path.open("a", encoding="utf-8") as f:
        f.write(f"=== {datetime.now(IST):%Y-%m-%d %H:%M:%S} {'TRIAL ' if trial else ''}slot {slot_id} "
                f"({target['docname']} {target['start']:%a %d %b %H:%M}), opens "
                f"{datetime.fromtimestamp(opens_at, IST):%H:%M:%S} server time, "
                f"server clock {offset:+.2f}s vs this computer\n")
    strike_log = (lambda m: log(m.replace(": booked", ": accepted (stand-in)"))) if trial else log
    for round_ in (1, 2):
        cookie = cookie_header(ctx)
        since = target["opens"] - timedelta(minutes=5)
        confirm = (lambda: False) if trial else (
            lambda c=cookie: slip_shows_booking(fetch_slip(c), target["docname"], since))
        out = watch_and_strike(ROSTER, cookie, query, slot_id, form,
                               fallback_at=fire_at, deadline=deadline, log=strike_log,
                               trace=trace, confirm=confirm, clock=clock)
        if out["kind"] != "logged_out" or round_ == 2:
            break
        log("Logged out; logging in again and continuing.")
        form["pat_id"] = login(page, interactive=not args.headless)
    if out["kind"] in ("stop", "already"):
        whose = already_booked(out["msg"], target["docname"])
        if whose == "ours":  # e.g. our first shot landed but its reply was lost
            out["kind"] = "booked"
        elif whose == "other":
            out["kind"] = "stop"
            log("Blocked: the account already has a booking today with another doctor.")
    kind_txt = "accepted (stand-in)" if trial and out["kind"] == "booked" else out["kind"]
    log(f"Result ({out['state']}) after {out['attempts']} attempts, {now() - opens_at:+.2f}s "
        f"vs opening: {kind_txt} - {out['msg']}")
    rel = lambda t: f"{t + offset - opens_at:+.2f}s" if t else "n/a"
    log(f"Server clock at the end: {clock.describe()}")
    log(f"Timing vs opening: last 'closed' check sent {rel(out['last_closed_sent'])}, "
        f"flag seen open {rel(out['flag_seen'])}, answer {now() - opens_at:+.2f}s")
    if trial:
        log(f"TRIAL done: request {'accepted' if out['kind'] == 'booked' else out['kind']}")
        if args.observe:  # how fast do the slots go?
            for _ in range(12):
                page.wait_for_timeout(10_000)
                try:
                    ses = next(x for d in get_json(ctx, query)["data"] for x in d["sessions"]
                               if str(x["id"]) == str(slot_id))
                    log(f"fill at {now() - opens_at:+.0f}s: {ses['patients']}/"
                        f"{ses['max_no_of_patients']} booked")
                except Exception as e:
                    log(f"fill check failed: {type(e).__name__}")
        return 0
    if out["kind"] == "booked":
        log("BOOKED!")
        notify(f"BOOKED: {target['docname']} {target['start']:%a %d %b %H:%M}")
    elif out["kind"] == "stop":
        notify(f"Not booked: {out['msg'][:80]}")
    else:
        notify(f"Not booked ({out['kind']}) - check bot_log.txt")
    if not args.headless:
        page.reload()
        log("Leaving the browser open for 10 min so you can check. Close it whenever.")
        try:
            page.wait_for_timeout(600_000)
        except Exception:
            pass
    return 0 if out["kind"] == "booked" else 1


def cmd_schedule(args):
    if not system.IS_MAC:
        print("Scheduling is macOS-only. On Windows, start `kgp-opd book ...` any time before "
              "booking opens (keep the laptop plugged in and the lid open); it waits by itself.")
        return 2
    book_args = ["--doctor", args.doctor]
    if args.at:
        when = datetime.strptime(args.at, "%Y-%m-%d %H:%M")
        for flag, val in (("--date", args.date), ("--session", args.session)):
            if val:
                book_args += [flag, val]
    else:  # look the session up now and start 10 minutes before its booking opens
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = open_browser(p, headless=True)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            ccode = get_ccode(ctx, login(page, interactive=False))
            target = find_session(ctx, ccode, args.date, args.session, args.doctor)
            ctx.close()
        if not target:
            print(f"No upcoming bookable session for {args.doctor!r}. See: kgp-opd doctors")
            return 1
        when = (target["opens"] - timedelta(minutes=10)).astimezone().replace(tzinfo=None)
        book_args += ["--date", f"{target['start']:%Y-%m-%d}", "--session", target["session"]]
        print(f"Found: {target['docname']} {target['start']:%a %d %b %H:%M}, "
              f"booking opens {target['opens']:%a %d %b %H:%M}")
    if args.clinic:
        book_args += ["--clinic", args.clinic]
    if when <= datetime.now():
        print(f"{when:%a %d %b %H:%M} is in the past.")
        return 1
    label = system.schedule(when, book_args)
    print(f"Scheduled for {when:%a %d %b %H:%M} ({label}). The Mac must be awake then: run\n"
          f"  sudo pmset schedule wake \"{(when - timedelta(minutes=5)):%m/%d/%y %H:%M:%S}\"\n"
          f"to wake it. Log: {system.home() / 'scheduled.log'}. Cancel: kgp-opd unschedule")
    return 0


def cmd_unschedule(args):
    jobs = system.scheduled_jobs()
    if not jobs:
        print("Nothing scheduled.")
    for label in jobs:
        system.unschedule(label)
        print(f"Removed {label}")
    return 0


def cmd_forget(args):
    print("Saved login removed." if system.forget_creds() else "No saved login.")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="kgp-opd",
        description="Book an OPD slot at B.C. Roy Technology Hospital (iMediX) the moment booking opens.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="log in once by hand; your login is saved on this computer")

    d = sub.add_parser("doctors", help="list upcoming OPD sessions and when booking opens")
    d.add_argument("name", nargs="?", help="only doctors whose name contains this")
    d.add_argument("--date", help="YYYY-MM-DD")

    def target_args(p):
        p.add_argument("--doctor", required=True, help="part of the doctor's name, or their id")
        p.add_argument("--date", help="only this visit date, YYYY-MM-DD")
        p.add_argument("--session", choices=["FN", "AN"], help="forenoon or afternoon")
        p.add_argument("--clinic", help="clinic whose name contains this (default: not Emergency)")

    b = sub.add_parser("book", help="wait for booking to open, then book")
    target_args(b)
    b.add_argument("--headless", action="store_true", help="no browser window")
    mode = b.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="everything except the booking")
    mode.add_argument("--rehearse", type=int, metavar="N",
                      help="full timed run as if booking opened in N s; never books")
    mode.add_argument("--observe", action="store_true",
                      help="watch a real opening with a harmless request; never books")
    b.add_argument("--scheduled-job", help=argparse.SUPPRESS)

    s = sub.add_parser("schedule", help="(macOS) run `book` automatically before booking opens")
    target_args(s)
    s.add_argument("--at", metavar="'YYYY-MM-DD HH:MM'",
                   help="start time (default: 10 min before booking opens, looked up now)")

    sub.add_parser("unschedule", help="(macOS) cancel scheduled runs")
    sub.add_parser("forget", help="delete the saved login from this computer")
    return ap


COMMANDS = {"setup": cmd_setup, "doctors": cmd_doctors, "book": cmd_book,
            "schedule": cmd_schedule, "unschedule": cmd_unschedule, "forget": cmd_forget}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows consoles may not handle every character
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
