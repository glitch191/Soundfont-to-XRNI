"""Drag-and-drop window (tkinter + tkinterdnd2), dark theme."""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from . import APP_NAME, __version__, engine

# Dark theme
BG = "#1e1f22"
SURFACE = "#2b2d31"
STRIPE = "#303237"
SURFACE_HI = "#35373c"
BORDER = "#43454b"
TEXT = "#e8e8ea"
MUTED = "#9ca0a8"
ACCENT = "#f59e0b"
ACCENT_HI = "#fbbf24"
ERROR = "#f87171"
ZONE_BG, ZONE_HOVER, ZONE_LINE = "#25272b", "#3a2e14", "#3a3d43"

FONT_THEMES = (
    ("Lato", "Lato Semibold", "Lato Semibold"),
    ("Segoe UI Variable Text", "Segoe UI Variable Text Semibold", "Segoe UI Variable Display Semib"),
)
FONT_SIZE, FONT_SMALL, FONT_TITLE = 11, 10, 14

SETTINGS = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME, "settings.json")


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _remaining(seconds: float) -> str:
    if seconds < 10:
        return "less than 10 s left"
    m, s = divmod(int(seconds), 60)
    return f"about {m} min {s:02d} s left" if m else f"about {s} s left"


def _short(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.lower().startswith(home.lower()) else path


def _fonts(root: tk.Misc) -> tuple[str, str, str]:
    installed = set(tkfont.families(root))
    for theme in FONT_THEMES:
        if all(f in installed for f in theme):
            return theme
    return "Segoe UI", "Segoe UI Semibold", "Segoe UI Semibold"


def _load_library() -> str:
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            path = json.load(f).get("library", "")
            if path and os.path.isdir(path):
                return path
    except (OSError, ValueError):
        pass
    return engine.DEFAULT_LIBRARY


def _save_library(path: str) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump({"library": path}, f)
    except OSError:
        pass


class ProgressLine(tk.Canvas):
    """Thin flat progress bar; ``pulse()`` shows a moving segment when the length is unknown."""

    def __init__(self, master: tk.Misc, height: int) -> None:
        super().__init__(master, height=height, bg=BG, highlightthickness=0)
        self._ratio = 0.0
        self._pulse: float | None = None  # position of the moving segment, 0..1
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, done: int, total: int) -> None:
        self._ratio = min(1.0, done / total) if total else 0.0
        self._pulse = None
        self._draw()

    def pulse(self) -> None:
        self._pulse = ((self._pulse or 0.0) + 0.02) % 1.0
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self.create_rectangle(0, 0, w, h, fill=SURFACE, width=0)
        if self._pulse is not None:
            seg = w * 0.2
            x = (w + seg) * self._pulse - seg
            self.create_rectangle(max(0, x), 0, min(w, x + seg), h, fill=ACCENT, width=0)
        elif self._ratio:
            self.create_rectangle(0, 0, round(w * self._ratio), h, fill=ACCENT, width=0)


class App:
    def __init__(self, root: TkinterDnD.Tk, initial: list[str]) -> None:
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.runner = engine.Runner(self.events)
        self.library = _load_library()
        self.rows: dict[str, str] = {}      # dst -> tree item
        self.stripe: dict[str, str] = {}
        self.weights: dict[str, int] = {}   # dst -> zones (time estimate)
        self.batch: dict[str, dict] = {}    # output root -> {"before": set, "results": list}
        self.root_of: dict[str, str] = {}   # dst -> output root
        self.total = self.done = self.errors = self.bytes = 0
        self.w_total = self.w_done = 0
        self.t_start = self.deadline = 0.0
        self.last_root = ""
        self._collecting = 0  # file reads in progress (drops)
        self.pending: dict[str, engine.Job] = {}  # scanned, waiting for "Convert" (dst -> job)
        self.pending_paths: list[str] = []         # what was dropped (re-scanned if the destination changes)
        self._notes: list[str] = []                # scan warnings shown in the status line
        self.scan: dict | None = None              # scan progress: text, done, total, t0
        self._scan_threads: list[threading.Thread] = []
        self._closing = False
        self.scale = s = root.winfo_fpixels("1i") / 96
        regular, semibold, display = _fonts(root)
        self.font = (regular, FONT_SIZE)
        self.font_small = (regular, FONT_SMALL)
        self.font_semi = (semibold, FONT_SIZE)
        self.font_title = (display, FONT_TITLE)

        root.title(f"{APP_NAME} {__version__}")
        root.geometry(f"{int(1000 * s)}x{int(760 * s)}")
        root.minsize(int(700 * s), int(520 * s))
        root.configure(bg=BG)
        self._style(ttk.Style(root))

        pad = int(20 * s)
        main = tk.Frame(root, bg=BG)
        main.pack(fill="both", expand=True, padx=pad, pady=pad)

        # Drop zone (a click opens the file picker)
        self.zone = tk.Canvas(main, height=int(130 * s), bg=BG, highlightthickness=0, cursor="hand2")
        self.zone.pack(fill="x")
        self.zone.bind("<Configure>", lambda e: self._draw_zone())
        self.zone.bind("<Button-1>", lambda e: self._pick_files())
        self._hover = False

        # Destination
        dest = tk.Frame(main, bg=BG)
        dest.pack(fill="x", pady=(int(16 * s), 0))
        ttk.Label(dest, text="Destination", style="Semi.TLabel").pack(side="left", padx=(0, int(16 * s)))
        self.dest_label = ttk.Label(dest, text="", style="Muted.TLabel")
        self.dest_label.pack(side="left", fill="x", expand=True)
        self.dest_btn = ttk.Button(dest, text="Change…", command=self._pick_library)
        self.dest_btn.pack(side="right")
        self._show_library()

        # Progress
        self.progress = ProgressLine(main, height=max(3, int(4 * s)))
        self.progress.pack(fill="x", pady=(int(16 * s), int(6 * s)))
        status = tk.Frame(main, bg=BG)
        status.pack(fill="x")
        self.status = ttk.Label(status, text="Ready", style="Muted.TLabel")
        self.status.pack(side="left")
        self.summary = ttk.Label(status, text="", style="Semi.TLabel")
        self.summary.pack(side="right")

        # Buttons (packed before the list so they always stay visible)
        buttons = tk.Frame(main, bg=BG)
        buttons.pack(side="bottom", fill="x", pady=(int(14 * s), 0))
        self.open_btn = ttk.Button(buttons, text="Open instruments folder", command=self._open_out,
                                   state="disabled")
        self.open_btn.pack(side="left")
        self.convert_btn = ttk.Button(buttons, text="Convert", style="Accent.TButton", command=self._convert,
                                      state="disabled")
        self.convert_btn.pack(side="right")
        self.cancel_btn = ttk.Button(buttons, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=(0, int(10 * s)))
        self.clear_btn = ttk.Button(buttons, text="Clear", command=self._clear, state="disabled")
        self.clear_btn.pack(side="right", padx=(0, int(10 * s)))

        # Instrument list
        cols = ("preset", "folder", "samples", "size", "state")
        frame = tk.Frame(main, bg=BORDER, padx=1, pady=1)
        frame.pack(fill="both", expand=True, pady=(int(10 * s), 0))
        self.tree = ttk.Treeview(frame, columns=cols, selectmode="none")
        for col, title, width, anchor in (("#0", "Instrument", 250, "w"), ("preset", "Bank · Prog.", 110, "center"),
                                          ("folder", "Folder", 190, "w"), ("samples", "Samples", 80, "e"),
                                          ("size", "Size", 90, "e"), ("state", "Status", 170, "w")):
            self.tree.heading(col, text=title, anchor=anchor)
            self.tree.column(col, width=int(width * s), anchor=anchor, stretch=col in ("#0", "state"))
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.tag_configure("even", background=SURFACE)
        self.tree.tag_configure("odd", background=STRIPE)
        self.tree.tag_configure("error", foreground=ERROR)
        self.tree.tag_configure("muted", foreground=MUTED)

        for widget in (root, self.zone, self.tree):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._enter)
            widget.dnd_bind("<<DropLeave>>", self._leave)
            widget.dnd_bind("<<Drop>>", self._drop)
        root.protocol("WM_DELETE_WINDOW", self._quit)
        root.bind("<Return>", lambda e: self._convert())
        root.after(100, self._poll)
        if initial:
            root.after(300, lambda: self.add(initial))

    def _style(self, style: ttk.Style) -> None:
        s = self.scale
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=SURFACE, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, troughcolor=SURFACE, focuscolor=BG,
                        selectbackground=SURFACE_HI, selectforeground=TEXT, font=self.font)
        style.configure("TLabel", background=BG, foreground=TEXT, font=self.font)
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Semi.TLabel", font=self.font_semi)
        style.configure("TButton", background=SURFACE, foreground=TEXT, bordercolor=BORDER,
                        lightcolor=SURFACE, darkcolor=SURFACE, focuscolor=SURFACE, font=self.font,
                        padding=(int(14 * s), int(5 * s)))
        style.map("TButton", background=[("disabled", BG), ("pressed", BORDER), ("active", SURFACE_HI)],
                  foreground=[("disabled", MUTED)], bordercolor=[("disabled", SURFACE)],
                  lightcolor=[("active", SURFACE_HI), ("disabled", BG)],
                  darkcolor=[("active", SURFACE_HI), ("disabled", BG)])
        # Primary action (Convert): accent colour, dark text.
        style.configure("Accent.TButton", background=ACCENT, foreground=BG, bordercolor=ACCENT,
                        lightcolor=ACCENT, darkcolor=ACCENT, focuscolor=ACCENT, font=self.font_semi)
        style.map("Accent.TButton", background=[("disabled", BG), ("pressed", ACCENT), ("active", ACCENT_HI)],
                  foreground=[("disabled", MUTED)], bordercolor=[("disabled", SURFACE), ("active", ACCENT_HI)],
                  lightcolor=[("active", ACCENT_HI), ("disabled", BG)],
                  darkcolor=[("active", ACCENT_HI), ("disabled", BG)])
        style.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=TEXT,
                        borderwidth=0, rowheight=int(30 * s), font=self.font_small)
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Treeview.Heading", background=SURFACE_HI, foreground=MUTED, bordercolor=SURFACE_HI,
                        lightcolor=SURFACE_HI, darkcolor=SURFACE_HI, relief="flat",
                        padding=(int(6 * s), int(5 * s)), font=(self.font_semi[0], FONT_SMALL))
        style.map("Treeview.Heading", background=[("active", SURFACE_HI)])
        style.configure("Vertical.TScrollbar", background=SURFACE_HI, troughcolor=SURFACE, bordercolor=SURFACE,
                        arrowcolor=MUTED, lightcolor=SURFACE_HI, darkcolor=SURFACE_HI, gripcount=0)
        style.map("Vertical.TScrollbar", background=[("active", BORDER)])

    # ---------------------------------------------------------------- drop zone
    def _draw_zone(self) -> None:
        z, s = self.zone, self.scale
        z.delete("all")
        w, h = z.winfo_width(), z.winfo_height()
        z.create_rectangle(1, 1, w - 2, h - 2, fill=ZONE_HOVER if self._hover else ZONE_BG,
                           outline=ACCENT if self._hover else ZONE_LINE, width=1)
        # Line icon: arrow down into a tray, above the title.
        cx, top = w / 2, h / 2 - 36 * s
        stroke = dict(fill=ACCENT, width=max(2, round(2 * s)), capstyle="round", joinstyle="round")
        z.create_line(cx, top, cx, top + 17 * s, **stroke)
        z.create_line(cx - 6 * s, top + 11 * s, cx, top + 17 * s, cx + 6 * s, top + 11 * s, **stroke)
        z.create_line(cx - 12 * s, top + 16 * s, cx - 12 * s, top + 23 * s, cx + 12 * s, top + 23 * s,
                      cx + 12 * s, top + 16 * s, **stroke)
        z.create_text(cx, h / 2 + 18 * s, text="Drop .sf2 or .sfz files, folders, "
                      ".zip or .7z archives here", fill=TEXT, font=self.font_title)

    def _enter(self, event):
        self._hover = True
        self._draw_zone()
        return event.action

    def _leave(self, event):
        self._hover = False
        self._draw_zone()
        return event.action

    def _drop(self, event):
        self._hover = False
        self._draw_zone()
        self.add(list(self.root.tk.splitlist(event.data)))
        return event.action

    def _pick_files(self) -> None:
        files = filedialog.askopenfilenames(title="Choose SF2 / SFZ instruments or archives",
                                            filetypes=[("SF2, SFZ, ZIP, 7z", "*.sf2 *.sfz *.zip *.7z"),
                                                       ("All files", "*.*")])
        if files:
            self.add(list(files))

    def _pick_library(self) -> None:
        path = filedialog.askdirectory(title="Renoise library Instruments folder",
                                       initialdir=self.library if os.path.isdir(self.library) else None)
        if path:
            self.library = os.path.normpath(path)
            _save_library(self.library)
            self._show_library()
            if self.pending_paths and not self.runner.running:  # destinations changed: scan again
                paths = self.pending_paths
                self._clear()
                self.add(paths)

    def _show_library(self) -> None:
        self.dest_label.configure(text=_short(self.library) + os.sep + "<.sf2, folder or archive name>")

    # ---------------------------------------------------------------- batch
    # Dropping only scans: the list shows what will be written, nothing is written until
    # "Convert" is clicked (or Enter pressed).

    def add(self, paths: list[str]) -> None:
        """Scan the dropped files in a thread (archives can take a while to unpack)."""
        self._collecting += 1
        if self.scan is None:
            self.scan = {"text": "Preparing", "done": None, "total": None, "t0": time.monotonic()}
        self._show_scan_progress()
        library = self.library
        last = [0.0, ""]

        def progress(text: str, done: int | None = None, total: int | None = None) -> None:
            # Called from the scanning thread: stop if the window is closing, and throttle
            # the updates to ~10 per second.
            if self._closing:
                raise engine.ScanCancelled()
            now = time.monotonic()
            if text != last[1] or now - last[0] >= 0.1 or (total and done == total):
                last[:] = [now, text]
                self.events.put(("scan", (text, done, total)))

        def work() -> None:
            try:
                self.events.put(("collected", (*engine.collect_jobs(paths, library, progress), paths)))
            except engine.ScanCancelled:
                pass
            except Exception as e:  # never leave the window waiting
                self.events.put(("collected", ([], [(", ".join(paths), str(e))], 0, paths)))

        thread = threading.Thread(target=work, daemon=True)
        self._scan_threads.append(thread)
        thread.start()

    def _show_scan_progress(self) -> None:
        """Status line + bar while scanning (the conversion owns them while it runs)."""
        if self.scan is None or self.runner.running:
            return
        sc = self.scan
        parts = [sc["text"]]
        if sc["total"]:
            parts.append(f"{100 * sc['done'] // sc['total']} %  ({_size(sc['done'])} / {_size(sc['total'])})")
            self.progress.set(sc["done"], sc["total"])
        else:
            self.progress.pulse()
        parts.append(f"{time.monotonic() - sc['t0']:.0f} s")
        self.status.configure(text="Scanning  ·  " + "  ·  ".join(parts))
        self.summary.configure(text="")

    def _collected(self, jobs: list, errors: list, ignored: int, paths: list[str]) -> None:
        self._collecting -= 1
        if not self._collecting:
            self.scan = None
            if not self.runner.running:
                self.progress.set(0, 1)
        if not self.runner.running and not self.pending:
            self._reset_list()  # a new drop after a finished run starts a fresh list
        converting = {dst for dst, iid in self.rows.items() if dst not in self.pending}
        jobs = [j for j in jobs if j.dst not in self.pending and not (self.runner.running and j.dst in converting)]
        notes = []
        if errors:
            notes.append(f"{len(errors)} unreadable file(s): "
                         + ", ".join(f"{os.path.basename(p)} ({e})" for p, e in errors))
        if ignored:
            notes.append(f"{ignored} unsupported file(s) skipped")
        self._notes = notes
        if jobs:
            self.pending_paths += [p for p in paths if p not in self.pending_paths]
        for j in jobs:
            if j.dst in self.rows and self.tree.exists(self.rows[j.dst]):
                self.tree.delete(self.rows[j.dst])
            stripe = "odd" if len(self.tree.get_children()) % 2 else "even"
            iid = self.tree.insert("", "end", text=j.name,
                                   values=(f"{j.bank} · {j.program}" if j.kind == "sf2"
                                           else "Audio" if j.kind == "copy"
                                           else f"SFZ · {j.program}" if j.program is not None else "SFZ",
                                           re.sub(r"^\d\d ", "", j.folder), "", "",
                                           "Replace" if os.path.exists(j.dst) else "New"),
                                   tags=(stripe,))
            self.rows[j.dst], self.stripe[iid] = iid, stripe
            self.pending[j.dst] = j
        if not self.pending and not self.runner.running:
            self.status.configure(text="  ·  ".join(notes) or "No .sf2 or .sfz file found")
            if not self._collecting:
                engine.cleanup_temp()
            return
        if not self.runner.running:
            self._show_scan()

    def _show_scan(self) -> None:
        """Status line for the scanned list, waiting for confirmation."""
        jobs = list(self.pending.values())
        instruments = sum(j.kind != "copy" for j in jobs)
        copies = len(jobs) - instruments
        folders = len({j.root for j in jobs})
        replace = sum(os.path.exists(j.dst) for j in jobs)
        parts = [f"Scanned: {instruments} instrument(s)"]
        if copies:
            parts.append(f"{copies} unused audio file(s) to copy")
        parts.append(f"into {folders} library folder(s)")
        if replace:
            parts.append(f"{replace} existing file(s) will be replaced")
        parts += self._notes
        self.progress.set(0, 1)
        self.status.configure(text="  ·  ".join(parts))
        self.summary.configure(text="Click Convert to start")
        self.convert_btn.state(["!disabled"])
        self.clear_btn.state(["!disabled"])
        self.convert_btn.focus_set()

    def _reset_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.stripe.clear()
        self.pending.clear()
        self.pending_paths = []

    def _clear(self) -> None:
        """Forget the scanned list (only while nothing is converting)."""
        if self.runner.running:
            return
        self._reset_list()
        if not self._collecting:
            engine.cleanup_temp()  # archives unpacked for the scan
        self.convert_btn.state(["disabled"])
        self.clear_btn.state(["disabled"])
        self.progress.set(0, 1)
        self.status.configure(text="Ready")
        self.summary.configure(text="")

    def _convert(self) -> None:
        """Start converting everything scanned so far."""
        if self.runner.running or not self.pending or self._collecting:
            return
        jobs = list(self.pending.values())
        self.pending.clear()
        self.pending_paths = []
        self.total, self.done, self.errors, self.bytes = len(jobs), 0, 0, 0
        self.weights = {j.dst: j.weight for j in jobs}
        self.w_total, self.w_done = sum(self.weights.values()), 0
        self.batch.clear()
        self.root_of = {j.dst: j.root for j in jobs}
        for root in dict.fromkeys(j.root for j in jobs):
            self.batch[root] = {"before": engine.existing_outputs(root), "results": []}
        self.last_root = jobs[-1].root
        for j in jobs:
            self.tree.set(self.rows[j.dst], "state", "Queued")
        self.t_start, self.deadline = time.monotonic(), 0.0
        self.runner.submit(jobs)
        self.open_btn.state(["!disabled"])
        for btn in (self.convert_btn, self.clear_btn, self.dest_btn):
            btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.summary.configure(text="")
        self._update_status()

    def _update_status(self, final: str | None = None) -> None:
        self.progress.set(self.done, self.total)
        if final:
            parts = [final]
        else:
            parts = [f"{self.done} / {self.total} item(s)", f"{self.runner.processes} worker process(es)"]
            if self.deadline:
                parts.append(_remaining(max(0.0, self.deadline - time.monotonic())))
        if self.errors:
            parts.append(f"{self.errors} error(s)")
        parts += getattr(self, "_notes", [])
        self.status.configure(text="  ·  ".join(parts))
        self.summary.configure(text=_size(self.bytes) if self.bytes else "")

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "scan":
                    if self.scan is not None:
                        self.scan.update(text=payload[0], done=payload[1], total=payload[2])
                elif kind == "collected":
                    self._collected(*payload)
                elif kind == "result":
                    self._show(payload)
                elif kind == "done" and not self.runner.running:
                    self._finish(complete=True)
                elif kind == "cancelled":
                    self._finish(complete=False)
        except queue.Empty:
            pass
        self._ticks = getattr(self, "_ticks", 0) + 1
        if self.runner.running and self.deadline and self._ticks % 10 == 0:
            self._update_status()
        if self._collecting:
            self._show_scan_progress()  # every tick: moving bar + elapsed time
        self.root.after(100, self._poll)

    def _show(self, r: dict) -> None:
        self.done += 1
        self.w_done += self.weights.get(r["dst"], 0)
        if 0 < self.w_done < self.w_total:
            elapsed = time.monotonic() - self.t_start
            self.deadline = time.monotonic() + elapsed * (self.w_total - self.w_done) / self.w_done
        else:
            self.deadline = 0.0
        out = self.root_of.get(r["dst"])
        if out in self.batch:
            self.batch[out]["results"].append(r)
        iid = self.rows.get(r["dst"])
        if r["status"] == "error":
            self.errors += 1
            samples, size, tag = "—", "—", "error"
        elif r["status"] == "empty":
            samples, size, tag = "0", "—", "muted"
        else:
            self.bytes += r["size"]
            samples, size, tag = str(r["samples"]) if r["samples"] else "—", _size(r["size"]), ""
        if iid and self.tree.exists(iid):
            self.tree.set(iid, "samples", samples)
            self.tree.set(iid, "size", size)
            self.tree.set(iid, "state", r["message"])
            self.tree.item(iid, tags=tuple(t for t in (self.stripe.get(iid, "even"), tag) if t))
        self._update_status()

    def _finish(self, complete: bool) -> None:
        self.deadline = 0.0
        # Replace: on completion, drop .xrni from an earlier conversion that no longer exist.
        removed = sum(engine.finalize(out, b["before"], b["results"], complete) for out, b in self.batch.items())
        if not complete:
            for iid in self.tree.get_children():
                if self.tree.set(iid, "state") == "Queued":
                    self.tree.set(iid, "state", "Cancelled")
        if not self._collecting and not self.pending:
            engine.cleanup_temp()  # unpacked archives (kept while a scanned list waits)
        self.cancel_btn.state(["disabled"])
        self.dest_btn.state(["!disabled"])
        elapsed = time.monotonic() - self.t_start
        text = (f"Done: {self.done - self.errors} item(s) in {elapsed:.0f} s" if complete
                else f"Cancelled after {self.done} item(s)")
        if removed:
            text += f"  ·  {removed} outdated file(s) removed"
        self._update_status(text)
        if self.pending:  # dropped while converting: waiting for their own confirmation
            self._show_scan()

    def _cancel(self) -> None:
        self.status.configure(text="Cancelling…")
        self.root.update_idletasks()
        self.runner.cancel()

    def _open_out(self) -> None:
        for folder in (self.last_root, self.library):
            if folder and os.path.isdir(folder):
                os.startfile(folder)
                return

    def _quit(self) -> None:
        self._closing = True  # a running scan stops at its next progress report
        if self.runner.running:
            self.runner.cancel()
        self.status.configure(text="Closing…")
        self.root.update_idletasks()
        for thread in self._scan_threads:
            thread.join(timeout=10)  # let it stop before its unpacked files are removed
        engine.cleanup_temp()
        self.root.destroy()


def main(initial: list[str]) -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    threading.Thread(target=engine.cleanup_stale_temp, daemon=True).start()  # leftovers of a killed session
    root = TkinterDnD.Tk()
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    icon = os.path.join(base, "icon.ico")
    try:
        root.iconbitmap(icon)
    except tk.TclError:
        pass
    App(root, initial)
    _dark_title_bar(root)
    _window_icons(root, icon)
    root.mainloop()


def _window_icons(root: tk.Tk, path: str) -> None:
    """Icon frames that match the current DPI (taskbar + title bar)."""
    try:
        user32 = ctypes.windll.user32
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_uint]
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        hwnd = user32.GetParent(root.winfo_id())
        for kind, metric in ((1, 11), (0, 49)):
            size = user32.GetSystemMetrics(metric)
            handle = user32.LoadImageW(None, path, 1, size, size, 0x10)
            if handle:
                user32.SendMessageW(hwnd, 0x80, kind, handle)
    except (AttributeError, OSError):
        pass


def _dark_title_bar(root: tk.Tk) -> None:
    try:
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), ctypes.sizeof(on))
    except (AttributeError, OSError):
        pass
