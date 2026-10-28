"""Everything that differs between macOS and Windows: where files live, the password store,
notifications, keeping the computer awake, and (macOS only) scheduling a run."""

import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = "bcroy-booker"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"
KEYRING_SERVICE = APP
KEYRING_USER = "login"  # one entry holding {"roll": ..., "password": ...}
JOB_PREFIX = "com.bcroy-booker."


def home():
    """Folder for the browser profile and logs (override with BCROY_BOOKER_HOME)."""
    d = Path(os.environ.get("BCROY_BOOKER_HOME") or Path.home() / f".{APP}")
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Login details: macOS Keychain / Windows Credential Manager, via `keyring` ──

def load_creds():
    """(roll, password) from the system password store, or None."""
    import keyring
    raw = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
    if not raw:
        return None
    try:
        d = json.loads(raw)
        return d["roll"], d["password"]
    except (ValueError, KeyError, TypeError):
        return None


def save_creds(roll, password):
    import keyring
    keyring.set_password(KEYRING_SERVICE, KEYRING_USER, json.dumps({"roll": roll, "password": password}))


def forget_creds():
    import keyring
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


# ── Notifications ──

_TOAST = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:BB_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:BB_MSG)) > $null
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(msg, title="BC Roy booker"):
    """Best effort: a desktop notification. Never raises."""
    try:
        if IS_MAC:
            safe = lambda s: s.replace("\\", "/").replace('"', "'")
            subprocess.run(["osascript", "-e", f'display notification "{safe(msg)}" '
                            f'with title "{safe(title)}" sound name "Glass"'],
                           check=False, capture_output=True, timeout=10)
        elif IS_WIN:
            import winsound
            winsound.MessageBeep()
            # Text goes in through the environment, so nothing in it is parsed as a command.
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST],
                             env=dict(os.environ, BB_TITLE=title, BB_MSG=msg),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass


# ── Keep the computer awake while running ──

class KeepAwake:
    """Stops idle sleep while the `with` block runs (closing the lid still sleeps)."""

    def __enter__(self):
        self._proc = None
        try:
            if IS_MAC:
                self._proc = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            elif IS_WIN:
                import ctypes
                # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
        except Exception:
            pass
        return self

    def __exit__(self, *exc):
        try:
            if self._proc:
                self._proc.terminate()
            elif IS_WIN:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            pass


def wait_for_network(url, max_wait=1200, every=15, log=print, sleep=time.sleep):
    """After waking from sleep, Wi-Fi can take a while. True once `url` answers."""
    deadline = time.time() + max_wait
    while True:
        try:
            urllib.request.urlopen(url, timeout=5).close()
            return True
        except Exception as e:
            if time.time() + every > deadline:
                log(f"No network after {max_wait // 60} min ({type(e).__name__}); trying anyway.")
                return False
            log(f"No network yet ({type(e).__name__}); retrying in {every} s")
            sleep(every)


# ── Scheduling a run (macOS launchd) ──

def _launchctl(*args):
    uid = str(os.getuid())
    return subprocess.run(["launchctl", *[a.replace("{uid}", uid) for a in args]],
                          capture_output=True, text=True)


def agents_dir():
    return Path.home() / "Library" / "LaunchAgents"


def schedule(when, book_args, launchctl=_launchctl):
    """One-shot launchd job that runs `book` at `when` (local time) and then removes itself.
    Returns the job label."""
    if not IS_MAC:
        raise RuntimeError("scheduling is macOS-only; on Windows start `book` before opening")
    label = f"{JOB_PREFIX}{when:%Y%m%d-%H%M}"
    plist = agents_dir() / f"{label}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": label,
        "ProgramArguments": [sys.executable, "-m", "bcroy_booker", "book", "--headless",
                             "--scheduled-job", label, *book_args],
        "StartCalendarInterval": {"Month": when.month, "Day": when.day,
                                  "Hour": when.hour, "Minute": when.minute},
        # Without this, macOS treats the job as background work and its timers wake late
        # (~60 ms per sleep, measured), which blurs the watchers' timing at the opening second.
        "ProcessType": "Interactive",
        "StandardOutPath": str(home() / "scheduled.log"),
        "StandardErrorPath": str(home() / "scheduled.log"),
    }))
    launchctl("bootout", f"gui/{{uid}}/{label}")
    r = launchctl("bootstrap", "gui/{uid}", str(plist))
    if r.returncode != 0:
        plist.unlink(missing_ok=True)
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
    return label


def scheduled_jobs():
    if not IS_MAC or not agents_dir().exists():
        return []
    return sorted(p.stem for p in agents_dir().glob(f"{JOB_PREFIX}*.plist"))


def unschedule(label, launchctl=_launchctl):
    """Remove a scheduled job. Called by the job itself as its very last step: unloading the
    running job ends this process."""
    if not label.startswith(JOB_PREFIX):
        raise ValueError(f"not one of ours: {label}")
    (agents_dir() / f"{label}.plist").unlink(missing_ok=True)
    launchctl("bootout", f"gui/{{uid}}/{label}")
