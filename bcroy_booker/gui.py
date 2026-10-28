"""The app window: log in, pick a session from the roster, and book it when booking opens.
Everything slow runs on a worker thread; the window only shows what it reports."""

import argparse
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from bcroy_booker import __version__, booker, system
from bcroy_booker.booker import IST, bookable
from bcroy_booker.cli import cmd_book, cmd_setup, fetch_sessions, status

PAD = 10


class App:
    def __init__(self, root):
        self.root = root
        self.ui = queue.Queue()  # callables to run on the window's thread
        self.busy = False
        self.booking = False
        self.sessions = []
        self.opens = None  # when booking opens (server time) for the running booking
        self.offset = 0.0

        root.title("BC Roy Booker")
        root.minsize(640, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        frame = ttk.Frame(root, padding=PAD)
        frame.pack(fill="both", expand=True)

        # 1. login
        ttk.Label(frame, text="1. Your iMediX login", font=("", 13, "bold")).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(2, PAD))
        self.login_label = ttk.Label(row, text="")
        self.login_label.pack(side="left")
        self.login_btn = ttk.Button(row, text="Log in…", command=self.do_login)
        self.login_btn.pack(side="right")

        # 2. sessions
        ttk.Label(frame, text="2. Choose a session", font=("", 13, "bold")).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="Doctor:").pack(side="left")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.fill_table())
        ttk.Entry(row, textvariable=self.search, width=24).pack(side="left", padx=6)
        self.load_btn = ttk.Button(row, text="Load sessions", command=self.do_load)
        self.load_btn.pack(side="right")
        cols = ("when", "doctor", "booked", "status")
        self.table = ttk.Treeview(frame, columns=cols, show="headings", height=9, selectmode="browse")
        for c, title, w in zip(cols, ("When", "Doctor", "Booked", "Status"), (150, 230, 70, 160)):
            self.table.heading(c, text=title)
            self.table.column(c, width=w, anchor="w")
        self.table.pack(fill="both", expand=True, pady=(2, PAD))
        self.table.bind("<<TreeviewSelect>>", lambda e: self.refresh())

        # 3. book
        ttk.Label(frame, text="3. Book", font=("", 13, "bold")).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=2)
        self.test_btn = ttk.Button(row, text="Test (no booking)", command=lambda: self.do_book(dry_run=True))
        self.test_btn.pack(side="left")
        self.book_btn = ttk.Button(row, text="Book when it opens", command=self.do_book)
        self.book_btn.pack(side="left", padx=6)
        self.status = ttk.Label(frame, text="", font=("", 13))
        self.status.pack(anchor="w", pady=(4, 0))
        ttk.Label(frame, text="Keep this window open and the computer plugged in, with the lid open. "
                              "It stops the computer from sleeping while it waits.",
                  foreground="gray", wraplength=600).pack(anchor="w", pady=(0, PAD))

        # log
        self.log = tk.Text(frame, height=8, wrap="word", state="disabled", font=("Menlo", 10))
        self.log.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"v{__version__} · logs in {system.home()}", foreground="gray").pack(anchor="e")

        booker.LOG_LISTENERS.append(lambda line: self.ui.put(lambda: self.append_log(line)))
        self.refresh()
        self.root.after(100, self.pump)
        self.root.after(500, self.tick)

    # ── plumbing ──

    def pump(self):
        while True:
            try:
                self.ui.get_nowait()()
            except queue.Empty:
                break
        self.root.after(100, self.pump)

    def run(self, work, done=None):
        """Run `work` on a worker thread, then `done(result or exception)` on the window."""
        self.busy = True
        self.refresh()

        def job():
            try:
                result = work()
            except BaseException as e:  # shown in the window, never lost
                result = e
            self.ui.put(lambda: self.finish(done, result))

        threading.Thread(target=job, daemon=True).start()

    def finish(self, done, result):
        self.busy = False
        if isinstance(result, BaseException):
            self.append_log(f"Error: {result}")
            self.set_status(f"Something went wrong: {str(result)[:120]}")
        elif done:
            done(result)
        self.refresh()

    def append_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, text):
        self.status.configure(text=text)

    def selected(self):
        sel = self.table.selection()
        return self.sessions[int(sel[0])] if sel else None

    def refresh(self):
        creds = system.load_creds()
        self.login_label.configure(text=f"Saved on this computer for {creds[0]}" if creds
                                   else "Not set up yet: log in once.")
        self.login_btn.configure(text="Log in again…" if creds else "Log in…")
        s = self.selected()
        idle = not self.busy
        self.login_btn.state(["!disabled"] if idle else ["disabled"])
        self.load_btn.state(["!disabled"] if idle and creds else ["disabled"])
        can_book = idle and s is not None and bookable(s)
        for b in (self.book_btn, self.test_btn):
            b.state(["!disabled"] if can_book else ["disabled"])
        if idle and s is not None and not bookable(s):
            self.set_status(f"This session can't be booked: {status(s, datetime.now(IST))}.")

    def tick(self):
        if self.booking and self.opens:
            left = (self.opens - datetime.now(IST)).total_seconds() - self.offset
            if left > 0:
                h, rem = divmod(int(left), 3600)
                self.set_status(f"Booking opens in {h:02d}:{rem // 60:02d}:{rem % 60:02d}. "
                                "Waiting… keep this window open.")
            else:
                self.set_status("Booking is open: booking now…")
        self.root.after(500, self.tick)

    # ── actions ──

    def do_login(self):
        self.set_status("A browser window opened: log in there as usual (roll number, password, captcha).")
        self.run(lambda: cmd_setup(argparse.Namespace()),
                 lambda rc: self.set_status("Login saved. Now load the sessions." if rc == 0
                                            else "Login was not saved. Try again."))

    def do_load(self):
        self.set_status("Loading the roster…")
        self.run(fetch_sessions, self.loaded)

    def loaded(self, sessions):
        now = datetime.now(IST)
        self.sessions = [s for s in sessions if s["start"] > now]
        self.fill_table()
        self.set_status(f"{len(self.sessions)} upcoming sessions. Pick one, then click Book.")

    def fill_table(self):
        self.table.delete(*self.table.get_children())
        q = self.search.get().strip()
        now = datetime.now(IST)
        for i, s in enumerate(self.sessions):
            if q and not booker.doctor_matches(s, q):
                continue
            self.table.insert("", "end", iid=str(i), values=(
                f"{s['start']:%a %d %b %H:%M}", s["docname"],
                f"{s.get('patients', '?')}/{s.get('max_no_of_patients', '?')}", status(s, now)))

    def do_book(self, dry_run=False):
        s = self.selected()
        if not s:
            return
        if not dry_run and not messagebox.askokcancel(
                "Book this session?",
                f"{s['docname']}\n{s['start']:%A %d %B, %H:%M}\n\nBooking opens "
                f"{s['opens']:%a %d %b %H:%M}. Keep this window open until then.\n\n"
                "The hospital allows one booking per day, with no online cancellation."):
            return
        args = argparse.Namespace(doctor=s["doc_id"], date=f"{s['start']:%Y-%m-%d}", session=s["session"],
                                  clinic=None, headless=True, dry_run=dry_run, rehearse=None, observe=False,
                                  scheduled_job=None, on_target=self.on_target)
        self.booking = not dry_run
        self.set_status("Testing…" if dry_run else "Getting ready…")
        self.run(lambda: cmd_book(args), lambda rc: self.booked(rc, dry_run))

    def on_target(self, target, offset):  # called from the worker thread
        self.ui.put(lambda: (setattr(self, "opens", target["opens"]), setattr(self, "offset", offset)))

    def booked(self, rc, dry_run):
        self.booking, self.opens = False, None
        if dry_run:
            self.set_status("Test passed: everything works. Click Book when you're ready." if rc == 0
                            else "Test failed: see the log below.")
        else:
            self.set_status("✓ Booked! Check your booking slip on the website." if rc == 0
                            else "✗ Not booked: see the log below.")

    def on_close(self):
        if self.booking and not messagebox.askyesno(
                "Stop waiting?", "The booking is still waiting for its time. Closing stops it. Close anyway?"):
            return
        self.root.destroy()
        os._exit(0)  # the browser thread may still be waiting; end everything


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
