# KGP OPD

Books an OPD slot at **B.C. Roy Technology Hospital, IIT Kharagpur** (iMediX) the moment booking opens, 60 minutes before the session. Popular doctors' slots are gone within about two minutes, which is hard to beat by hand.

It runs on your own computer (macOS or Windows) with your own iMediX login. It needs no AI, no API key and no server. Download the app and click; no terminal needed.

> **Please be fair.** Book only appointments you will attend, run one copy per person, and don't change the timings to poll the server harder. The hospital allows one booking per patient per day, and OPD bookings can't be cancelled online.

## The easy way: the app (no terminal)

1. Go to **[Releases](https://github.com/MSR327/kgp-opd/releases/latest)** and download the zip for your computer (`mac` for Apple Silicon Macs, `windows` for Windows). Unzip it.
2. Open **KGP OPD**. The first time, your computer warns that the developer is unknown, because the app isn't paid-signed:
   * **macOS:** click *Done*, then open *System Settings → Privacy & Security*, scroll down and click **Open Anyway**.
   * **Windows:** on *"Windows protected your PC"*, click **More info → Run anyway**. Inside the unzipped folder, open `KGP OPD.exe`.
3. Click **Log in…** and log in once in the browser window that opens (roll number, password, captcha). Your login is saved in this computer's password store (Keychain on macOS, Credential Manager on Windows), never in a file.
4. Click **Load sessions**, type part of your doctor's name, and select a session.
5. Click **Test (no booking)** once to check everything works, then **Book when it opens**.

Keep the window open until it says *Booked*. It stops the computer from idle-sleeping while it waits. Keep it plugged in with the lid open. You can start it hours early.

You need Google Chrome or Microsoft Edge (every Windows PC has Edge).

## The terminal way

You need **Python 3.11 or newer** ([python.org](https://www.python.org/downloads/); on Windows tick *"Add python.exe to PATH"*).

```bash
# macOS (Terminal)
python3 -m pip install --user https://github.com/MSR327/kgp-opd/archive/refs/heads/main.zip

# Windows (Command Prompt or PowerShell)
py -m pip install --user https://github.com/MSR327/kgp-opd/archive/refs/heads/main.zip
```

This installs two things:

* `kgp-opd-app` opens the same app window.
* `kgp-opd` is the command line.

If your terminal says `command not found`, use `python3 -m kgp_opd` (macOS) or `py -m kgp_opd` (Windows) instead of `kgp-opd`.

**1. Log in once.**

```bash
kgp-opd setup
```

**2. Find your doctor.** This lists upcoming sessions, places taken, and when booking opens. Add part of a name to filter.

```bash
kgp-opd doctors
kgp-opd doctors rao
```

**3. Check that everything works.** This does everything except the booking itself.

```bash
kgp-opd book --doctor "rao" --dry-run
```

**4. Book.** Use part of the doctor's name (enough to match only one doctor) or their id from step 2.

* **macOS:** schedule it once and walk away. It finds the next session, starts 10 minutes before booking opens, books, and removes itself:
  ```bash
  kgp-opd schedule --doctor "rao"
  ```
  The Mac must be awake then. The command prints a `pmset` line that wakes it up; keep it plugged in. Cancel with `kgp-opd unschedule`.
* **Windows** (or macOS without scheduling): start it any time before booking opens and leave it running. It waits by itself.
  ```bash
  kgp-opd book --doctor "rao"
  ```

You get a desktop notification with the result. Optional filters: `--date 2026-11-02`, `--session FN` (forenoon) or `AN` (afternoon), and `--clinic "medicine"`. By default it picks the doctor's first clinic that isn't Emergency.

## How it works

1. **Before opening:** it logs in through a real browser, finds the doctor's next session that still has places, fetches the clinic id, and measures how far the hospital server's clock is from yours (from the `Date` headers of about 30 quick requests).
2. **From 20 seconds before opening:** two watchers take turns checking the roster's own "booking open" flag every half second, each on a connection that is already open, so no time is lost connecting.
3. **The moment the flag flips:** the watcher that saw it sends the booking on its open connection. If the flag never flips, a backup request is timed from the measured clock.
4. **Never twice:** the server accepts duplicate bookings, so only one booking request is ever in flight. If a reply is unclear, it checks your booking slip before trying again.

Everything is logged to `bot_log.txt`, and every request around the opening second is logged to the millisecond in `trace.txt`. Both are in `~/.kgp-opd` (Windows: `C:\Users\<you>\.kgp-opd`).

To try the timing without booking:

* `--rehearse 120` runs the whole timed sequence as if booking opened in 2 minutes.
* `--observe` watches a real opening.

Both send a harmless request in place of the booking.

## Good to know

* One booking per patient per day: after you book, the hospital blocks other bookings until midnight (or noon, after a morning booking).
* There's no online cancellation for OPD slots. Contact the hospital.
* To delete your saved login: `kgp-opd forget`. To delete everything else: remove the `.kgp-opd` folder.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The tests run against a fake iMediX server on your own machine; they never contact the hospital.

*Unofficial. Not affiliated with IIT Kharagpur or B.C. Roy Technology Hospital. Use it for your own appointments, at your own risk.*
