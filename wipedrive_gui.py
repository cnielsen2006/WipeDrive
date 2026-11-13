#!/usr/bin/env python3
"""wipedrive_gui - a tkinter front end for wipedrive.py.  Standard library only.

Launch:  python wipedrive_gui.py

Everything that lists drives, decides what is safe, or writes goes through wipedrive.py
(imported, not run as a subprocess).  This module adds:

  * plain, widget-free logic that takes the platform object as a parameter, so it can be
    tested with a fake platform and no window:
        DriveInventory / RowInfo      which rows exist and which are selectable (and why not)
        validate_selection()          mode / selection / admin rules, compute_controls()
        WipeRequest, build_request()  a validated, immutable request
        QueueProgress                 a wipedrive.Progress look-alike that posts to a queue and
                                      raises Cancelled when asked to stop
        WipeWorker                    re-checks safety, runs the wipe, posts Msg objects
  * the widgets (App, ConfirmWipeDialog), which only talk to the worker through a queue.

Threading rule: the worker thread never touches tkinter.  It puts Msg objects on a
queue.Queue; App._poll() drains that queue from the UI thread with root.after().
"""

import contextlib
import dataclasses
import io
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, List, Optional

import wipedrive
from wipedrive import DiskInfo, RefusedError, VerifyError, VolumeInfo, WipeError

MODE_FREESPACE = "freespace"
MODE_DEVICE = "device"
PASS_CHOICES = ("zero", "random", "one", "dod")
POLL_MS = 100
TERMINAL_KINDS = ("done", "failed", "cancelled", "refused")

_LETTER_RE = re.compile(r"^[A-Za-z]:$")
_PASS_RE = re.compile(r"pass (\d+)/(\d+) \((\w+)\)")
_VERIFY_RE = re.compile(r"verify \((\w+)\)")


class Cancelled(Exception):
    """Raised inside the core (from QueueProgress) to unwind through its finally blocks."""


# --------------------------------------------------------------------------
# drive inventory: which rows exist, which are selectable, and why not
# --------------------------------------------------------------------------

def volume_root(vol: VolumeInfo) -> str:
    """Directory whose free space a free-space wipe would fill, or '' if there is none."""
    if _LETTER_RE.match(vol.letter or ""):
        return vol.letter.upper() + "\\"
    if vol.mounted and (vol.label or "").startswith("/"):       # Linux: label is the mount point
        return vol.label
    return ""


def volume_device_target(vol: VolumeInfo) -> str:
    """Target string for wiping the whole volume (Windows drive letter), or ''."""
    return vol.letter if _LETTER_RE.match(vol.letter or "") else ""


@dataclasses.dataclass(eq=False)
class RowInfo:
    iid: str                        # Treeview item id
    parent: str                     # '' for a disk, the disk's iid for a volume
    kind: str                       # 'disk' | 'volume'
    key: str                        # disk.id, or the volume's letter / guid / positional fallback
    disk: DiskInfo                  # the physical disk (a volume's parent)
    volume: Optional[VolumeInfo]
    target: DiskInfo                # what the core is asked about: the disk, or volume_target()
    reason: str                     # '' when selectable, else the core's full refusal text
    note: str                       # short text for the Note column
    system: bool

    @property
    def selectable(self) -> bool:
        return not self.reason

    @property
    def name(self) -> str:
        if self.kind == "disk":
            return self.disk.id
        return self.volume.letter or "(volume with no drive letter on %s)" % self.disk.id

    @property
    def fs_root(self) -> str:
        return volume_root(self.volume) if self.volume is not None else ""

    @property
    def device_target(self) -> str:
        if self.kind == "disk":
            return self.disk.id
        return volume_device_target(self.volume)

    @property
    def signature(self):
        """Identity of the thing that was selected; compared again at Start."""
        if self.kind == "disk":
            return ("disk", self.disk.id, self.disk.size, self.disk.model)
        v = self.volume
        return ("volume", v.letter, v.guid, v.size, v.label, v.fs)


class DriveInventory:
    """A snapshot of the drives: rows in display order (disk, then its volumes)."""

    def __init__(self, rows: List[RowInfo], warnings: Optional[List[str]] = None):
        self.rows = rows
        self.warnings = list(warnings or [])
        self._by_iid = {r.iid: r for r in rows}

    def get(self, iid) -> Optional[RowInfo]:
        return self._by_iid.get(iid)

    def find(self, kind: str, key: str) -> Optional[RowInfo]:
        for r in self.rows:
            if r.kind == kind and r.key == key:
                return r
        return None

    @classmethod
    def load(cls, platform) -> "DriveInventory":
        """List the disks through the platform and classify every row with the core's own
        refusal_reason().  Raises whatever list_disks raises (e.g. RefusedError)."""
        disks = platform.list_disks()
        warnings = list(getattr(platform, "warnings", None) or [])
        try:
            system = set(platform.system_ids())
            blanket = ""
        except WipeError as exc:
            # Fail safe, like the CLI: if the system disk cannot be identified, nothing is allowed.
            system = set()
            blanket = "cannot identify the system disk (%s); all drives are locked" % exc
        rows: List[RowInfo] = []
        for i, disk in enumerate(disks):
            d_iid = "disk%d" % i
            is_sys = disk.is_system or disk.id in system
            d_reason = blanket or wipedrive.refusal_reason(disk, system) or ""
            rows.append(RowInfo(
                iid=d_iid, parent="", kind="disk", key=disk.id, disk=disk, volume=None,
                target=disk, reason=d_reason,
                note="system disk" if (is_sys and not blanket) else (d_reason or disk.note),
                system=is_sys))
            for j, vol in enumerate(disk.volumes):
                target = wipedrive.volume_target(disk, vol, system)
                if blanket:
                    v_reason = blanket
                elif is_sys:
                    v_reason = d_reason                    # the system reason; volumes of a
                else:                                      # system disk are locked with it
                    v_reason = wipedrive.refusal_reason(target, system) or ""
                rows.append(RowInfo(
                    iid="vol%d_%d" % (i, j), parent=d_iid, kind="volume",
                    key=vol.letter or vol.guid or "%s#%d" % (disk.id, j), disk=disk, volume=vol,
                    target=target, reason=v_reason,
                    note="system disk" if (is_sys and not blanket) else v_reason,
                    system=is_sys))
        return cls(rows, warnings)


def sanitize_selection(inventory: Optional[DriveInventory], selected, previous: Optional[str]):
    """Which iid should be selected after the widget reported `selected`?  A row that is
    not selectable is never kept: fall back to the previous valid selection, else nothing."""
    if inventory is None:
        return None
    for iid in selected:
        row = inventory.get(iid)
        if row is not None and row.selectable:
            return iid
    row = inventory.get(previous) if previous else None
    return previous if (row is not None and row.selectable) else None


def step_selection(order: List[RowInfo], current: Optional[str], direction: int) -> Optional[str]:
    """Up/Down keyboard navigation that skips unselectable rows.  `order` is the visible
    rows top to bottom; returns the iid to select (the current one if nothing is reachable)."""
    ok = [r.selectable for r in order]
    if current is None or all(r.iid != current for r in order):
        rng = range(len(order)) if direction > 0 else range(len(order) - 1, -1, -1)
        for i in rng:
            if ok[i]:
                return order[i].iid
        return None
    pos = next(i for i, r in enumerate(order) if r.iid == current)
    i = pos + direction
    while 0 <= i < len(order):
        if ok[i]:
            return order[i].iid
        i += direction
    return current


# --------------------------------------------------------------------------
# mode / selection validation and the request
# --------------------------------------------------------------------------

def validate_selection(mode: str, row: Optional[RowInfo], admin: bool) -> Optional[str]:
    """None if (mode, row) may be started, else a sentence saying why not."""
    if row is None:
        return "Select a drive or volume above."
    if row.reason:
        return "%s cannot be used: %s" % (row.name, row.reason)
    if mode == MODE_FREESPACE:
        if row.kind != "volume":
            return ("Free-space wipe works on a volume. Select a volume with a drive letter "
                    "(not a whole disk).")
        if not row.fs_root:
            return "This volume has no drive letter, so its free space cannot be wiped."
        return None
    if mode == MODE_DEVICE:
        if not row.device_target:
            return ("This volume has no drive letter and cannot be targeted. "
                    "Select the physical disk instead.")
        if not admin:
            return ("Wiping an entire drive needs administrator rights, and this program is "
                    "not elevated. Use 'Restart as administrator'.")
        return None
    return "Unknown mode %r." % (mode,)


@dataclasses.dataclass(frozen=True)
class ControlState:
    start_enabled: bool
    message: str
    verify_enabled: bool


def compute_controls(mode: str, row: Optional[RowInfo], admin: bool, running: bool) -> ControlState:
    if running:
        return ControlState(False, "A wipe is running.", False)
    problem = validate_selection(mode, row, admin)
    return ControlState(problem is None, problem or "", mode == MODE_DEVICE)


@dataclasses.dataclass(frozen=True)
class WipeRequest:
    mode: str
    row_kind: str
    key: str                  # inventory key, used to find the row again at Start
    signature: tuple          # RowInfo.signature at selection time
    target: str               # directory to fill (free space) or device target string
    passes: tuple
    verify: bool
    dry_run: bool
    chunk: int
    label: str


def build_request(mode: str, row: Optional[RowInfo], passes_spec: str, verify: bool,
                  dry_run: bool, admin: bool, chunk: int = wipedrive.DEFAULT_CHUNK) -> WipeRequest:
    """A WipeRequest for a valid selection; ValueError (with the reason) otherwise."""
    problem = validate_selection(mode, row, admin)
    if problem:
        raise ValueError(problem)
    passes = tuple(wipedrive.parse_passes(passes_spec))          # ValueError on bad spec
    if mode == MODE_FREESPACE:
        label = "Wipe free space on %s" % row.fs_root
        if row.volume.label:
            label += " (%s)" % row.volume.label
        target, verify = row.fs_root, False                       # verify is whole-drive only
    else:
        label = "Wipe entire %s %s (%s)" % ("drive" if row.kind == "disk" else "volume",
                                            row.target.id, row.disk.model)
        target = row.device_target
    return WipeRequest(mode, row.kind, row.key, row.signature, target, passes, bool(verify),
                       bool(dry_run), chunk, label)


# --------------------------------------------------------------------------
# queue protocol and the queue-posting progress object
# --------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Msg:
    """One message from a background thread to the UI thread.

    kind: 'start' | 'log' | 'phase' | 'progress'                    (non-terminal)
          'done' | 'failed' | 'cancelled' | 'refused'               (exactly one, always last)
          'inventory' | 'inventory_error'                           (drive refresh)
    """
    kind: str
    text: str = ""
    done: int = 0
    total: int = 0
    percent: float = 0.0
    rate: float = 0.0
    eta: Optional[float] = None
    pass_index: int = 0
    pass_count: int = 0
    phase: str = ""               # 'pass' | 'verify' | 'other'
    reason: str = ""              # for 'failed': 'verify' | 'io' | 'error' | 'internal'
    payload: Any = None


@dataclasses.dataclass(frozen=True)
class Phase:
    kind: str
    index: int
    count: int
    pattern: str
    title: str


def parse_phase(label: str) -> Phase:
    """Read the labels wipedrive gives its progress factory ('pass 2/3 (one)',
    'freespace pass 1/2 (zero)', 'verify (zero)')."""
    m = _PASS_RE.search(label)
    if m:
        i, n, pat = int(m.group(1)), int(m.group(2)), m.group(3)
        return Phase("pass", i, n, pat, "Pass %d of %d (%s)" % (i, n, pat))
    m = _VERIFY_RE.search(label)
    if m:
        return Phase("verify", 0, 0, m.group(1), "Verify (%s)" % m.group(1))
    return Phase("other", 0, 0, "", label)


class QueueProgress:
    """Same interface as wipedrive.Progress (update, finish, done, total, label), but it
    posts Msg objects instead of drawing, and update() raises Cancelled once the cancel
    event is set so the core unwinds through its own try/finally cleanup."""

    INTERVAL = 0.2

    def __init__(self, total, label, post, cancel_event, clock=time.monotonic, interval=INTERVAL):
        if cancel_event.is_set():
            raise Cancelled()
        self.total, self.label = total, label
        self.done = 0
        self.phase = parse_phase(label)
        self._post, self._cancel, self._clock, self._interval = post, cancel_event, clock, interval
        self._start = self._last = clock()
        self._finished = False
        post(Msg("phase", self.phase.title, total=total, pass_index=self.phase.index,
                 pass_count=self.phase.count, phase=self.phase.kind))

    def update(self, n: int) -> None:
        if self._cancel.is_set():
            raise Cancelled()
        self.done += n
        now = self._clock()
        if now - self._last >= self._interval:
            self._last = now
            self._emit(now)

    def finish(self) -> None:          # runs in finally blocks: must never raise
        if self._finished:
            return
        self._finished = True
        self._emit(self._clock())

    def _emit(self, now: float) -> None:
        pct, rate, eta = wipedrive.progress_stats(self.done, self.total, now - self._start)
        self._post(Msg("progress", done=self.done, total=self.total, percent=pct, rate=rate,
                       eta=eta, pass_index=self.phase.index, pass_count=self.phase.count,
                       phase=self.phase.kind))


# --------------------------------------------------------------------------
# the worker
# --------------------------------------------------------------------------

class WipeWorker:
    """Runs one WipeRequest.  run() never raises: it posts exactly one terminal Msg
    (done / failed / cancelled / refused) as its last message.  `opener` and `disk_usage`
    are only used by the free-space path; tests inject capped fakes."""

    def __init__(self, platform, request: WipeRequest, events: "queue.Queue",
                 opener=open, disk_usage=shutil.disk_usage, clock=time.monotonic,
                 progress_interval: float = QueueProgress.INTERVAL):
        self.platform = platform
        self.request = request
        self.events = events
        self.opener = opener
        self.disk_usage = disk_usage
        self.clock = clock
        self.progress_interval = progress_interval
        self.cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- control (called from the UI thread) ----------------------------------
    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="wipe-worker", daemon=False)
        self._thread.start()
        return self._thread

    def cancel(self) -> None:
        self.cancel_event.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- worker thread --------------------------------------------------------
    def _post(self, msg: Msg) -> None:
        self.events.put(msg)

    def _log(self, text: str) -> None:
        self.events.put(Msg("log", text))

    def _factory(self, total, label):
        progress = QueueProgress(total, label, self._post, self.cancel_event, self.clock,
                                 self.progress_interval)
        self._log("%s started" % progress.phase.title)
        return progress

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def run(self) -> None:
        captured = io.StringIO()
        try:
            # The core reports cleanup trouble ("could not remove ...") on stderr; show it.
            with contextlib.redirect_stderr(captured):
                terminal = self._execute()
        except BaseException as exc:             # the thread must never die silently
            terminal = self._terminal_for(exc)
        for line in captured.getvalue().splitlines():
            if line.strip():
                self._log("core: " + line.strip())
        self.events.put(terminal)

    def _terminal_for(self, exc: BaseException) -> Msg:
        req = self.request
        if isinstance(exc, Cancelled):
            if req.mode == MODE_FREESPACE:
                return Msg("cancelled", "Cancelled. The fill files were deleted by the cleanup "
                                        "path (check the log for any warning).")
            return Msg("cancelled", "Cancelled. The device was released (unlocked and closed); "
                                    "it may be partially overwritten.")
        if isinstance(exc, VerifyError):
            return Msg("failed", "VERIFY FAILED: %s" % exc, reason="verify")
        if isinstance(exc, RefusedError):
            return Msg("refused", "Refused: %s" % exc)
        if isinstance(exc, WipeError):
            return Msg("failed", "Error: %s" % exc, reason="error")
        if isinstance(exc, OSError):
            return Msg("failed", "I/O error: %s" % exc, reason="io")
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for sub in line.rstrip().splitlines():
                self._log(sub)
        return Msg("failed", "Internal error: %r" % (exc,), reason="internal")

    def _recheck(self) -> Optional[DiskInfo]:
        """Re-list the drives and re-apply the safety rules before anything is opened."""
        req = self.request
        inventory = DriveInventory.load(self.platform)
        row = inventory.find(req.row_kind, req.key)
        if row is None:
            raise RefusedError("%s is no longer present" % req.key)
        if row.reason:
            raise RefusedError(row.reason)
        if row.signature != req.signature:
            raise RefusedError("%s changed since it was selected (size, model or label "
                               "differ); refresh and select it again" % req.key)
        if req.mode == MODE_DEVICE:
            # same resolve + refusal_reason + admin check as the CLI
            return wipedrive.resolve_wipe_target(self.platform, req.target)
        if not os.path.isdir(req.target):
            raise RefusedError("%s is not a directory" % req.target)
        return None

    def _execute(self) -> Msg:
        req = self.request
        self._post(Msg("start", req.label, pass_count=len(req.passes)))
        self._log(req.label + (" [dry run]" if req.dry_run else ""))
        self._check_cancel()
        self._log("Re-checking the target against a fresh drive listing")
        disk = self._recheck()
        if req.mode == MODE_DEVICE:
            lines = wipedrive.device_summary_lines(disk, list(req.passes), req.verify)
        else:
            lines = wipedrive.freespace_summary_lines(req.target, list(req.passes), req.chunk,
                                                      self.disk_usage)
        for line in lines:
            self._log("  " + line)
        if req.dry_run:
            return Msg("done", "Dry run: nothing was opened, locked or written.")
        self._check_cancel()
        passes = list(req.passes)
        if req.mode == MODE_FREESPACE:
            written = wipedrive.fill_free_space(
                req.target, passes, req.chunk, opener=self.opener, disk_usage=self.disk_usage,
                progress_factory=self._factory)
            return Msg("done", "Filled %s and removed the fill files."
                       % wipedrive.format_size(written))
        size = wipedrive.wipe_device(self.platform, req.target, disk.id, passes, req.chunk,
                                     req.verify, self._factory)
        if req.verify:
            self._log("Verify passed: the last pass reads back correctly")
        return Msg("done", "Wiped %s (%s)%s." % (disk.id, wipedrive.format_size(size),
                                                 ", verified" if req.verify else ""))


# --------------------------------------------------------------------------
# elevation
# --------------------------------------------------------------------------

def _shell_execute_w(hwnd, verb, file, params, cwd, show):
    import ctypes
    from ctypes import wintypes
    fn = ctypes.WinDLL("shell32", use_last_error=True).ShellExecuteW
    fn.restype = ctypes.c_void_p
    fn.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                   wintypes.LPCWSTR, ctypes.c_int]
    return fn(hwnd, verb, file, params, cwd, show)


def relaunch_as_admin(shell_execute=None, executable=None, script=None, argv=None,
                      cwd=None) -> bool:
    """Start this same interpreter and script again with the 'runas' verb (UAC prompt).
    True if Windows accepted the request (the caller should then exit); False if it failed
    or the user declined."""
    if shell_execute is None:
        if sys.platform != "win32":
            return False
        shell_execute = _shell_execute_w
    exe = executable or sys.executable
    script = script or os.path.abspath(__file__)
    params = subprocess.list2cmdline([script] + list(sys.argv[1:] if argv is None else argv))
    try:
        result = shell_execute(None, "runas", exe, params, cwd or os.getcwd(), 1)
    except (OSError, AttributeError):
        return False
    return int(result or 0) > 32


ERROR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wipedrive_gui_error.log")


def relaunch_as_admin_checked(settle: float = 3.0):
    """Start an elevated copy and make sure it stays up.  Returns (problem, has_exited).

    problem is None once the elevated copy has survived `settle` seconds, otherwise a
    message saying why there is no elevated copy (the caller keeps its own window open).
    has_exited is a callable that turns True when the elevated copy has gone, or None if
    it cannot be watched.  The caller must NOT exit while the elevated copy is running:
    Windows makes the elevated process a child of this one, so when this process sits in
    a kill-on-close job (the VS Code debugger's launcher and some terminals create one),
    exiting here kills the elevated copy too.  Hide the window and wait instead."""
    if sys.platform != "win32":
        return "Elevation is only supported on Windows.", None
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040                      # SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = sys.executable
    info.lpParameters = subprocess.list2cmdline([os.path.abspath(__file__)] + sys.argv[1:])
    info.lpDirectory = os.path.dirname(os.path.abspath(__file__))
    info.nShow = 1
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:                          # ERROR_CANCELLED
            return "The administrator prompt was declined.", None
        return ("Windows could not start an elevated copy: %s"
                % ctypes.FormatError(err).strip()), None
    handle = info.hProcess
    if not handle:
        return None, None                        # started, but no handle to watch
    if kernel32.WaitForSingleObject(handle, int(settle * 1000)) == 0:
        code = wintypes.DWORD(0)
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return ("The elevated copy started but exited straight away (exit code %d).\n\n"
                "If it hit an error, the details are in:\n%s" % (code.value, ERROR_LOG)), None

    state = {"open": True}

    def has_exited() -> bool:
        if not state["open"]:
            return True
        if kernel32.WaitForSingleObject(handle, 0) == 0:
            kernel32.CloseHandle(handle)
            state["open"] = False
            return True
        return False

    return None, has_exited


# --------------------------------------------------------------------------
# widgets
# --------------------------------------------------------------------------

class ConfirmWipeDialog:
    """Modal 'type the target ID' confirmation.  The Wipe button stays disabled until the
    entry equals the ID exactly (compared by wipedrive.confirm, the CLI's own rule)."""

    def __init__(self, parent, target_id: str, summary_lines: List[str]):
        self.target_id = target_id
        self.result = False
        top = self.top = tk.Toplevel(parent)
        top.title("Confirm wipe")
        top.transient(parent)
        top.resizable(False, False)
        frame = ttk.Frame(top, padding=12)
        frame.grid(sticky="nsew")
        ttk.Label(frame, text="This will PERMANENTLY DESTROY all data on:",
                  font=("TkDefaultFont", 10, "bold")).grid(sticky="w")
        text = tk.Text(frame, height=len(summary_lines) + 1,
                       width=max(40, max(len(s) for s in summary_lines) + 2),
                       font="TkFixedFont", wrap="none", relief="sunken", borderwidth=1)
        text.insert("1.0", "\n".join(summary_lines))
        text.configure(state="disabled")
        text.grid(sticky="ew", pady=(6, 10))
        ttk.Label(frame, text="To continue, type  %s  exactly:" % target_id).grid(sticky="w")
        self.var = tk.StringVar()
        self.entry = ttk.Entry(frame, textvariable=self.var, width=30)
        self.entry.grid(sticky="w", pady=(4, 10))
        buttons = ttk.Frame(frame)
        buttons.grid(sticky="e")
        self.wipe_button = ttk.Button(buttons, text="Wipe", command=self._ok, state="disabled")
        self.wipe_button.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=1)
        self.var.trace_add("write", lambda *_: self._sync())
        top.bind("<Return>", lambda _e: self._ok())
        top.bind("<Escape>", lambda _e: self._cancel())
        top.protocol("WM_DELETE_WINDOW", self._cancel)

    def matches(self) -> bool:
        return wipedrive.confirm(self.target_id, self.var.get())

    def _sync(self) -> None:
        self.wipe_button.configure(state="normal" if self.matches() else "disabled")

    def _ok(self) -> None:
        if not self.matches():                    # Enter must not bypass the check
            return
        self.result = True
        self.top.destroy()

    def _cancel(self) -> None:
        self.result = False
        self.top.destroy()

    def show(self) -> bool:
        self.top.update_idletasks()
        parent = self.top.master
        x = parent.winfo_rootx() + (parent.winfo_width() - self.top.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.top.winfo_reqheight()) // 3
        self.top.geometry("+%d+%d" % (max(0, x), max(0, y)))
        self.top.wait_visibility()
        self.top.grab_set()
        self.entry.focus_set()
        self.top.wait_window()
        return self.result


def _load_inventory(platform, events: "queue.Queue") -> None:
    """Body of the drive-refresh thread: list the drives, post the result."""
    try:
        events.put(Msg("inventory", payload=DriveInventory.load(platform)))
    except Exception as exc:
        events.put(Msg("inventory_error", "%s" % exc))


class App:
    def __init__(self, root: tk.Tk, platform, worker_options: Optional[dict] = None):
        self.root = root
        self.platform = platform
        self.worker_options = dict(worker_options or {})
        self.events: "queue.Queue" = queue.Queue()
        self.inventory: Optional[DriveInventory] = None
        self.worker: Optional[WipeWorker] = None
        self._run_active = False
        self._closing = False
        self._refreshing = False
        self._selected: Optional[str] = None
        self._after_id = None
        try:
            self.admin = bool(platform.is_admin())
        except Exception:
            self.admin = False
        root.title("WipeDrive")
        try:
            self._scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except tk.TclError:
            self._scale = 1.0
        root.minsize(int(880 * self._scale), int(700 * self._scale))
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Destroy>", self._on_destroy, add="+")
        self.refresh()
        self._poll()

    # -- construction ---------------------------------------------------------
    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=3)
        root.rowconfigure(3, weight=2)

        drives = ttk.LabelFrame(root, text="Drives", padding=6)
        drives.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        drives.columnconfigure(0, weight=1)
        drives.rowconfigure(0, weight=1)
        cols = ("model", "bus", "size", "label", "fs", "note")
        self.tree = ttk.Treeview(drives, columns=cols, selectmode="browse", height=8)
        # Pixel sizes are fixed in Tk, fonts are not: scale rows and columns to the display.
        scale = self._scale
        import tkinter.font as tkfont
        ttk.Style(root).configure(
            "Treeview", rowheight=tkfont.nametofont("TkDefaultFont").metrics("linespace") + 6)
        for col, title, width, anchor in (
                ("#0", "Drive / volume", 150, "w"), ("model", "Model", 190, "w"),
                ("bus", "Bus", 60, "w"), ("size", "Size", 80, "e"), ("label", "Label", 110, "w"),
                ("fs", "File system", 80, "w"), ("note", "Note", 260, "w")):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=int(width * scale), anchor=anchor,
                             stretch=(col in ("note", "model")))
        self.tree.tag_configure("disabled", foreground="#8c8c8c")
        scroll = ttk.Scrollbar(drives, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.refresh_button = ttk.Button(drives, text="Refresh", command=self.refresh)
        self.refresh_button.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Up>", lambda _e: self._on_arrow(-1))
        self.tree.bind("<Down>", lambda _e: self._on_arrow(+1))

        admin = ttk.Frame(root)
        admin.grid(row=1, column=0, sticky="ew", padx=8)
        self.admin_var = tk.StringVar(value=(
            "Administrator: yes" if self.admin else
            "Administrator: no - wiping an entire drive is disabled; free-space wipe still works"))
        ttk.Label(admin, textvariable=self.admin_var).grid(row=0, column=0, sticky="w")
        self.elevate_button = ttk.Button(admin, text="Restart as administrator",
                                         command=self.on_elevate)
        if not self.admin and sys.platform == "win32":
            self.elevate_button.grid(row=0, column=1, padx=(12, 0))

        op = ttk.LabelFrame(root, text="Operation", padding=6)
        op.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        op.columnconfigure(0, weight=1)
        self.mode_var = tk.StringVar(value=MODE_FREESPACE)
        self.pass_var = tk.StringVar(value="zero")
        self.verify_var = tk.BooleanVar(value=False)
        self.dry_var = tk.BooleanVar(value=False)
        self.mode_buttons = [
            ttk.Radiobutton(op, text="Wipe free space (volume with a drive letter)",
                            variable=self.mode_var, value=MODE_FREESPACE,
                            command=self._update_controls),
            ttk.Radiobutton(op, text="Wipe entire drive / volume (destroys everything on it)",
                            variable=self.mode_var, value=MODE_DEVICE,
                            command=self._update_controls)]
        self.mode_buttons[0].grid(row=0, column=0, sticky="w")
        self.mode_buttons[1].grid(row=1, column=0, sticky="w")
        opts = ttk.Frame(op)
        opts.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(opts, text="Pass pattern:").grid(row=0, column=0)
        self.pass_box = ttk.Combobox(opts, textvariable=self.pass_var, values=PASS_CHOICES,
                                     state="readonly", width=8)
        self.pass_box.grid(row=0, column=1, padx=(4, 16))
        self.verify_check = ttk.Checkbutton(opts, text="Verify (whole drive only)",
                                            variable=self.verify_var)
        self.verify_check.grid(row=0, column=2, padx=(0, 16))
        self.dry_check = ttk.Checkbutton(opts, text="Dry run (show what would happen)",
                                         variable=self.dry_var)
        self.dry_check.grid(row=0, column=3)
        self.msg_var = tk.StringVar()
        ttk.Label(op, textvariable=self.msg_var, wraplength=800, foreground="#a02020") \
            .grid(row=3, column=0, sticky="w", pady=(6, 0))
        btns = ttk.Frame(op)
        btns.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.start_button = ttk.Button(btns, text="Start", command=self.on_start)
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.cancel_button = ttk.Button(btns, text="Cancel", command=self.on_cancel,
                                        state="disabled")
        self.cancel_button.grid(row=0, column=1)

        st = ttk.LabelFrame(root, text="Status", padding=6)
        st.grid(row=3, column=0, sticky="nsew", padx=8, pady=(4, 8))
        st.columnconfigure(0, weight=1)
        st.rowconfigure(4, weight=1)
        self.op_var = tk.StringVar(value="Idle")
        self.pass_label_var = tk.StringVar(value="")
        self.stats_var = tk.StringVar(value="")
        ttk.Label(st, textvariable=self.op_var, font=("TkDefaultFont", 9, "bold")) \
            .grid(row=0, column=0, sticky="w")
        ttk.Label(st, textvariable=self.pass_label_var).grid(row=1, column=0, sticky="w")
        self.bar = ttk.Progressbar(st, mode="determinate", maximum=100)
        self.bar.grid(row=2, column=0, sticky="ew", pady=2)
        ttk.Label(st, textvariable=self.stats_var).grid(row=3, column=0, sticky="w")
        self.log = tk.Text(st, height=10, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(st, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.grid(row=4, column=0, sticky="nsew", pady=(4, 0))
        log_scroll.grid(row=4, column=1, sticky="ns", pady=(4, 0))
        self._update_controls()

    # -- helpers ----------------------------------------------------------------
    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", "[%s] %s\n" % (time.strftime("%H:%M:%S"), text))
        self.log.see("end")
        self.log.configure(state="disabled")

    def _selected_row(self) -> Optional[RowInfo]:
        if self.inventory is None or self._selected is None:
            return None
        return self.inventory.get(self._selected)

    def _update_controls(self) -> None:
        running = self._run_active
        state = compute_controls(self.mode_var.get(), self._selected_row(), self.admin, running)
        self.msg_var.set(state.message)
        self.start_button.configure(state="normal" if state.start_enabled else "disabled")
        self.cancel_button.configure(state="normal" if running and not self._closing
                                     and not self.worker.cancel_event.is_set() else "disabled")
        self.verify_check.configure(state="normal" if state.verify_enabled else "disabled")
        idle = "disabled" if running else "normal"
        for w in self.mode_buttons + [self.dry_check]:
            w.configure(state=idle)
        self.pass_box.configure(state="disabled" if running else "readonly")
        self.refresh_button.configure(state="disabled" if running or self._refreshing
                                      else "normal")
        self.elevate_button.configure(state=idle)

    # -- drive list ---------------------------------------------------------------
    def refresh(self) -> None:
        if self._refreshing or self._run_active:
            return
        self._refreshing = True
        self._update_controls()
        self.op_var.set("Listing drives...")

        # The thread gets only the platform and the queue, never a Tk object.
        threading.Thread(target=_load_inventory, args=(self.platform, self.events),
                         name="drive-refresh", daemon=True).start()

    def _populate(self, inv: DriveInventory) -> None:
        previous = self._selected_row()
        self.inventory = inv
        self.tree.delete(*self.tree.get_children())
        for r in inv.rows:
            tags = () if r.selectable else ("disabled",)
            if r.kind == "disk":
                values = (r.disk.model, r.disk.bus, wipedrive.format_size(r.disk.size),
                          "", "", r.note)
                text = r.disk.id
            else:
                v = r.volume
                values = ("", "", wipedrive.format_size(v.size), v.label, v.fs, r.note)
                text = v.letter or "(no letter)"
            self.tree.insert(r.parent, "end", iid=r.iid, text=text, values=values, tags=tags,
                             open=True)
        self._selected = None
        if previous is not None:
            again = inv.find(previous.kind, previous.key)
            if again is not None and again.selectable:
                self._selected = again.iid
                self.tree.selection_set(again.iid)
        for w in inv.warnings:
            self._log("warning: " + w)
        disks = sum(1 for r in inv.rows if r.kind == "disk")
        self._log("Found %d physical disk(s)" % disks)

    def _visible_rows(self) -> List[RowInfo]:
        out = []
        for iid in self.tree.get_children(""):
            out.append(self.inventory.get(iid))
            if self.tree.item(iid, "open"):
                out.extend(self.inventory.get(c) for c in self.tree.get_children(iid))
        return out

    def _on_click(self, event):
        if self.tree.identify_element(event.x, event.y) == "Treeitem.indicator":
            return None                          # expanding/collapsing selects nothing
        iid = self.tree.identify_row(event.y)
        row = self.inventory.get(iid) if (iid and self.inventory) else None
        if row is not None and not row.selectable:
            self.msg_var.set("%s is disabled: %s" % (row.name, row.reason))
            return "break"                       # the class binding would select it
        return None

    def _on_select(self, _event) -> None:
        selected = self.tree.selection()
        keep = sanitize_selection(self.inventory, selected, self._selected)
        if tuple(selected) != ((keep,) if keep else ()):
            if keep:
                self.tree.selection_set(keep)
            else:
                self.tree.selection_remove(*selected)
        self._selected = keep
        self._update_controls()

    def _on_arrow(self, direction: int):
        if self.inventory is not None:
            new = step_selection(self._visible_rows(), self._selected, direction)
            if new is not None:
                self.tree.selection_set(new)
                self.tree.focus(new)
                self.tree.see(new)
        return "break"

    # -- start / cancel ------------------------------------------------------------
    def on_start(self) -> None:
        if self._run_active:
            return
        row, mode = self._selected_row(), self.mode_var.get()
        try:
            request = build_request(mode, row, self.pass_var.get(), self.verify_var.get(),
                                    self.dry_var.get(), self.admin)
        except ValueError as exc:
            self._log("Not started: %s" % exc)
            messagebox.showwarning("Cannot start", str(exc), parent=self.root)
            return
        if not request.dry_run:
            if mode == MODE_DEVICE:
                lines = wipedrive.device_summary_lines(row.target, list(request.passes),
                                                       request.verify)
                if not ConfirmWipeDialog(self.root, row.target.id, lines).show():
                    self._log("Wipe of %s not confirmed; nothing was done" % row.target.id)
                    return
            elif not messagebox.askokcancel(
                    "Wipe free space",
                    "Free space on %s will be filled with temporary files (%s pass%s) and "
                    "then deleted.\n\nThe volume will be briefly full: programs writing to "
                    "it may fail until the wipe ends.\n\nContinue?"
                    % (request.target, len(request.passes),
                       "" if len(request.passes) == 1 else "es"),
                    icon="warning", parent=self.root):
                self._log("Free-space wipe not confirmed; nothing was done")
                return
        self.bar.configure(value=0)
        self.pass_label_var.set("")
        self.stats_var.set("")
        self.worker = WipeWorker(self.platform, request, self.events, **self.worker_options)
        self._run_active = True
        self.worker.start()
        self._update_controls()

    def on_cancel(self) -> None:
        if self._run_active and self.worker is not None:
            self.worker.cancel()
            self._log("Cancel requested; waiting for the running step to stop and clean up")
            self.op_var.set("Cancelling...")
            self._update_controls()

    def on_elevate(self) -> None:
        if self._run_active:
            return
        self.op_var.set("Starting an elevated copy...")
        self.root.update_idletasks()
        problem, has_exited = relaunch_as_admin_checked()
        if problem is None:
            if has_exited is None:
                self.root.destroy()
                return
            # Stay alive but hidden until the elevated copy closes (see
            # relaunch_as_admin_checked for why exiting now could kill it).
            self.root.withdraw()

            def wait() -> None:
                if has_exited():
                    self.root.destroy()
                else:
                    self.root.after(500, wait)
            wait()
        else:
            self.root.deiconify()
            self.op_var.set("Idle")
            self._log("Restart as administrator failed: %s" % problem.replace("\n", " "))
            messagebox.showwarning("Restart as administrator", problem, parent=self.root)

    def on_close(self) -> None:
        if self._closing:
            self.op_var.set("Still cleaning up; the window closes when that is finished...")
            return
        if self._run_active and self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(
                    "Wipe running",
                    "A wipe is still running.\n\nCancel it and close? The window stays open "
                    "until cleanup (deleting fill files, unlocking volumes) has finished.",
                    icon="warning", parent=self.root):
                return
            if self._run_active:
                self._closing = True
                self.worker.cancel()
                self.op_var.set("Cancelling; closing when cleanup has finished...")
                self._update_controls()
                return
        self.root.destroy()

    def shutdown(self) -> None:
        """After mainloop returns: never leave a wipe running unattended."""
        if self.worker is not None and self.worker.is_alive():
            self.worker.cancel()
            self.worker.join()

    # -- queue ---------------------------------------------------------------------
    def _poll(self) -> None:
        for _ in range(500):
            try:
                msg = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle(msg)
        if self._closing and (self.worker is None or not self.worker.is_alive()) \
                and self.events.empty():
            self.root.destroy()
            return
        self._after_id = self.root.after(POLL_MS, self._poll)

    def _on_destroy(self, event) -> None:
        if event.widget is self.root and self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _handle(self, m: Msg) -> None:
        if m.kind == "inventory":
            self._refreshing = False
            self.op_var.set("Idle")
            self._populate(m.payload)
            self._update_controls()
        elif m.kind == "inventory_error":
            self._refreshing = False
            self.op_var.set("Idle")
            self._log("Could not list drives: %s" % m.text)
            self._update_controls()
        elif m.kind == "start":
            self.op_var.set(m.text)
        elif m.kind == "log":
            self._log(m.text)
        elif m.kind == "phase":
            self.pass_label_var.set(m.text)
            self.bar.configure(value=0)
            self.stats_var.set("")
        elif m.kind == "progress":
            self.bar.configure(value=m.percent)
            eta = "--:--:--" if m.eta is None else wipedrive.format_duration(m.eta)
            self.stats_var.set("%5.1f%%   %s   %s/s   ETA %s" % (
                m.percent, wipedrive.format_size(m.done),
                wipedrive.format_size(int(m.rate)), eta))
        elif m.kind in TERMINAL_KINDS:
            self._finish_run(m)

    def _finish_run(self, m: Msg) -> None:
        if self.worker is not None:
            self.worker.join(2.0)             # the thread is exiting; this is instantaneous
        self._run_active = False
        self._log(m.text)
        self.op_var.set({"done": "Finished", "cancelled": "Cancelled", "refused": "Refused",
                         "failed": "Failed"}[m.kind])
        self.pass_label_var.set("")
        if m.kind == "done" and float(self.bar.cget("value")) > 0:
            self.bar.configure(value=100)        # a dry run writes nothing: leave the bar empty
        self._update_controls()
        if not self._closing:
            if m.kind == "refused":
                messagebox.showerror("Refused", m.text, parent=self.root)
            elif m.kind == "failed":
                messagebox.showerror("Failed", m.text, parent=self.root)
        if m.kind in ("done", "cancelled", "failed", "refused") and not self._closing:
            self.refresh()                    # volumes may have changed


def main(argv=None) -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("cannot open a window: %s" % exc, file=sys.stderr)
        return 1
    app = App(root, wipedrive.get_platform())
    try:
        root.mainloop()
    finally:
        app.shutdown()
    return 0


def _main_logged() -> int:
    """Run main(); a startup failure is written to ERROR_LOG and shown in a message box,
    because an elevated copy runs in its own console that vanishes on exit."""
    try:
        return main()
    except Exception:
        import datetime
        import traceback
        text = "%s  admin-relaunch-or-startup failure\n%s\n" % (
            datetime.datetime.now().isoformat(timespec="seconds"), traceback.format_exc())
        try:
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text[-1500:], "WipeDrive failed to start", 0x10)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(_main_logged())
