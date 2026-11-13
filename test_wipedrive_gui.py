"""Tests for wipedrive_gui.py.

Safety, same rules as test_wipedrive.py:
  * no test opens a real device, locks a volume, or fills a real volume;
  * device tests use a fake platform with an in-memory device;
  * free-space tests use test_wipedrive.QuotaOpener (a hard 16 MiB cap, ENOSPC at a few
    hundred KiB), a fake disk_usage, and an explicit TemporaryDirectory as the target --
    make_request() refuses to build a free-space request without one;
  * nothing here presses the Start button: the worker and the widgets are exercised
    separately.
The one test that touches the real machine (SmokeTest) only calls list_disks() and
system_ids(), which open disks with access 0, and selects nothing.
"""

import contextlib
import dataclasses
import errno
import gc
import io
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest import mock

import wipedrive as w
import wipedrive_gui as g
from test_wipedrive import (FakeClock, FakeDevice, FakePlatform, QuotaOpener, big_free,
                            make_disk)
from wipedrive import DiskInfo, VolumeInfo

KB = 1024
MB = 1024 * KB
SECTOR = 512


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------

class GuiPlatform(FakePlatform):
    """test_wipedrive.FakePlatform plus volume targets ('E:'), like WinPlatform."""

    name = "windows"

    def resolve_target(self, target):
        if re.match(r"^[A-Za-z]:$", target):
            for d in self.disks:
                for v in d.volumes:
                    if v.letter == target.upper():
                        return w.volume_target(d, v, self.system)
            raise w.WipeError("no such volume: %s" % target)
        return super().resolve_target(target)


def vol(letter="", label="", fs="NTFS", extents=1, size=400 * SECTOR, **kw):
    return VolumeInfo(letter=letter, label=label, fs=fs, size=size, extents=extents,
                      guid="\\\\?\\Volume{%s%s}\\" % (letter or "x", label), **kw)


def fleet():
    return [
        make_disk(id="PhysicalDrive0", model="System SSD", bus="NVMe", is_system=True,
                  volumes=[vol("", "EFI", "FAT32"), vol("C:", "Windows")]),
        make_disk(id="PhysicalDrive2", model="Test Disk", bus="USB",
                  volumes=[vol("E:", "DATA"), vol("", "hidden")]),
        make_disk(id="PhysicalDrive3", model="Dynamic Disk", bus="SATA",
                  volumes=[vol("G:", "SPAN", extents=2), vol("H:", "PLAIN")]),
        make_disk(id="PhysicalDrive4", model="Empty reader", bus="USB", size=0, volumes=[]),
    ]


def platform(**kw):
    kw.setdefault("system", ("PhysicalDrive0", "C:"))
    return GuiPlatform(disks=fleet(), **kw)


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def make_request(plat, mode, kind, key, passes="zero", verify=False, dry_run=False,
                 admin=True, chunk=4096, target_override=None):
    inv = g.DriveInventory.load(plat)
    row = inv.find(kind, key)
    req = g.build_request(mode, row, passes, verify, dry_run, admin, chunk)
    if mode == g.MODE_FREESPACE:
        # Safety: a free-space request must never point at a real volume in a test.
        assert target_override is not None, "free-space tests must pass a temp directory"
    if target_override is not None:
        req = dataclasses.replace(req, target=target_override)
    return req


def run_worker(plat, req, **kw):
    q = queue.Queue()
    worker = g.WipeWorker(plat, req, q, progress_interval=0, **kw)
    worker.run()
    return worker, drain(q)


def terminal(msgs):
    t = [m for m in msgs if m.kind in g.TERMINAL_KINDS]
    assert len(t) == 1, "expected exactly one terminal message, got %r" % [m.kind for m in t]
    assert msgs[-1] is t[0], "terminal message must be last"
    return t[0]


def log_text(msgs):
    return "\n".join(m.text for m in msgs if m.kind == "log")


# --------------------------------------------------------------------------
# inventory: which rows are selectable and why not
# --------------------------------------------------------------------------

class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.inv = g.DriveInventory.load(platform())

    def row(self, kind, key):
        r = self.inv.find(kind, key)
        self.assertIsNotNone(r, "%s %s missing" % (kind, key))
        return r

    def test_rows_are_disks_with_their_volumes_as_children(self):
        disks = [r for r in self.inv.rows if r.kind == "disk"]
        self.assertEqual([r.key for r in disks],
                         ["PhysicalDrive0", "PhysicalDrive2", "PhysicalDrive3", "PhysicalDrive4"])
        e = self.row("volume", "E:")
        self.assertEqual(e.parent, self.row("disk", "PhysicalDrive2").iid)

    def test_system_disk_and_all_its_volumes_are_unselectable_and_marked(self):
        for kind, key in (("disk", "PhysicalDrive0"), ("volume", "C:")):
            r = self.row(kind, key)
            self.assertFalse(r.selectable)
            self.assertTrue(r.system)
            self.assertEqual(r.note, "system disk")
            self.assertIn("system disk", r.reason)
        efi = next(r for r in self.inv.rows if r.volume and r.volume.label == "EFI")
        self.assertFalse(efi.selectable)                 # letterless volume of the system disk
        self.assertEqual(efi.note, "system disk")

    def test_system_by_id_even_if_the_flag_is_unset(self):
        p = platform(system=("PhysicalDrive0", "C:", "PhysicalDrive2"))
        inv = g.DriveInventory.load(p)
        self.assertFalse(inv.find("disk", "PhysicalDrive2").selectable)
        self.assertFalse(inv.find("volume", "E:").selectable)

    def test_ordinary_rows_are_selectable(self):
        self.assertTrue(self.row("disk", "PhysicalDrive2").selectable)
        self.assertTrue(self.row("volume", "E:").selectable)
        self.assertEqual(self.row("volume", "E:").reason, "")

    def test_multi_extent_disk_and_volume_refused_with_reason_but_sibling_ok(self):
        d = self.row("disk", "PhysicalDrive3")
        self.assertFalse(d.selectable)
        self.assertIn("extents", d.reason)
        self.assertEqual(d.note, d.reason)               # reason visible in the Note column
        self.assertFalse(self.row("volume", "G:").selectable)
        self.assertIn("extents", self.row("volume", "G:").reason)
        self.assertTrue(self.row("volume", "H:").selectable)   # same as `device H:` in the CLI

    def test_no_media_disk_refused_with_reason(self):
        r = self.row("disk", "PhysicalDrive4")
        self.assertFalse(r.selectable)
        self.assertIn("no size", r.reason)

    def test_mounted_linux_style_partition_refused(self):
        d = make_disk(id="/dev/sdb", volumes=[VolumeInfo(letter="/dev/sdb1", label="/mnt/x",
                                                         mounted=True, size=SECTOR * 10)])
        inv = g.DriveInventory.load(FakePlatform(disks=[d], system=()))
        self.assertFalse(inv.find("disk", "/dev/sdb").selectable)
        self.assertIn("mounted", inv.find("volume", "/dev/sdb1").reason)

    def test_unidentifiable_system_disk_locks_everything(self):
        p = platform()
        p.system_ids = mock.Mock(side_effect=w.RefusedError("cannot map C:"))
        inv = g.DriveInventory.load(p)
        self.assertTrue(inv.rows)
        for r in inv.rows:
            self.assertFalse(r.selectable, r.key)
            self.assertIn("cannot identify the system disk", r.reason)

    def test_platform_warnings_are_carried(self):
        p = platform()
        p.warnings = ["something odd"]
        self.assertEqual(g.DriveInventory.load(p).warnings, ["something odd"])

    def test_list_failure_propagates(self):
        with self.assertRaises(w.RefusedError):
            g.DriveInventory.load(w.UnsupportedPlatform())

    def test_signature_changes_when_the_thing_changes(self):
        before = self.row("disk", "PhysicalDrive2").signature
        p = platform()
        p.disks[1].model = "Other Disk"
        after = g.DriveInventory.load(p).find("disk", "PhysicalDrive2").signature
        self.assertNotEqual(before, after)

    def test_volume_roots(self):
        self.assertEqual(g.volume_root(VolumeInfo(letter="e:")), "E:\\")
        self.assertEqual(g.volume_root(VolumeInfo(letter="")), "")
        self.assertEqual(g.volume_root(VolumeInfo(letter="/dev/sdb1", label="/mnt/x",
                                                  mounted=True)), "/mnt/x")
        self.assertEqual(g.volume_root(VolumeInfo(letter="/dev/sdb1")), "")


class SelectionGuardTests(unittest.TestCase):
    def setUp(self):
        self.inv = g.DriveInventory.load(platform())
        self.sys_iid = self.inv.find("disk", "PhysicalDrive0").iid
        self.c_iid = self.inv.find("volume", "C:").iid
        self.e_iid = self.inv.find("volume", "E:").iid
        self.d2_iid = self.inv.find("disk", "PhysicalDrive2").iid

    def test_disabled_row_is_never_kept(self):
        self.assertIsNone(g.sanitize_selection(self.inv, (self.sys_iid,), None))
        self.assertIsNone(g.sanitize_selection(self.inv, (self.c_iid,), None))

    def test_falls_back_to_previous_valid_selection(self):
        self.assertEqual(g.sanitize_selection(self.inv, (self.sys_iid,), self.e_iid), self.e_iid)

    def test_previous_is_dropped_if_it_is_no_longer_valid(self):
        self.assertIsNone(g.sanitize_selection(self.inv, (), self.sys_iid))
        self.assertIsNone(g.sanitize_selection(self.inv, (), "gone"))

    def test_valid_row_wins(self):
        self.assertEqual(g.sanitize_selection(self.inv, (self.e_iid,), self.d2_iid), self.e_iid)

    def test_no_inventory(self):
        self.assertIsNone(g.sanitize_selection(None, ("disk0",), None))

    def test_arrow_keys_skip_disabled_rows(self):
        order = self.inv.rows
        first = g.step_selection(order, None, +1)
        self.assertEqual(first, self.d2_iid)             # disk0 and its volumes are skipped
        self.assertEqual(g.step_selection(order, first, +1), self.e_iid)
        self.assertEqual(g.step_selection(order, first, -1), first)    # nothing selectable above
        last = g.step_selection(order, None, -1)
        self.assertTrue(self.inv.get(last).selectable)
        self.assertEqual(g.step_selection(order, last, +1), last)


# --------------------------------------------------------------------------
# mode / selection validation
# --------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.inv = g.DriveInventory.load(platform())

    def row(self, kind, key):
        return self.inv.find(kind, key)

    def test_nothing_selected(self):
        self.assertIn("Select", g.validate_selection(g.MODE_FREESPACE, None, True))
        self.assertIn("Select", g.validate_selection(g.MODE_DEVICE, None, True))

    def test_freespace_needs_a_lettered_volume(self):
        self.assertIsNone(g.validate_selection(g.MODE_FREESPACE, self.row("volume", "E:"), False))
        msg = g.validate_selection(g.MODE_FREESPACE, self.row("disk", "PhysicalDrive2"), True)
        self.assertIn("volume", msg)
        letterless = next(r for r in self.inv.rows if r.volume and r.volume.label == "hidden")
        self.assertIn("no drive letter", g.validate_selection(g.MODE_FREESPACE, letterless, True))

    def test_freespace_works_without_admin(self):
        self.assertIsNone(g.validate_selection(g.MODE_FREESPACE, self.row("volume", "E:"), False))

    def test_whole_drive_without_admin_is_blocked_and_explained(self):
        for row in (self.row("disk", "PhysicalDrive2"), self.row("volume", "E:")):
            msg = g.validate_selection(g.MODE_DEVICE, row, False)
            self.assertIn("administrator", msg)
            self.assertIsNone(g.validate_selection(g.MODE_DEVICE, row, True))

    def test_whole_drive_accepts_disk_or_lettered_volume_only(self):
        letterless = next(r for r in self.inv.rows if r.volume and r.volume.label == "hidden")
        self.assertIn("no drive letter", g.validate_selection(g.MODE_DEVICE, letterless, True))

    def test_disabled_rows_are_rejected_in_both_modes_with_their_reason(self):
        for mode in (g.MODE_FREESPACE, g.MODE_DEVICE):
            for kind, key in (("disk", "PhysicalDrive0"), ("volume", "C:"),
                              ("disk", "PhysicalDrive3"), ("disk", "PhysicalDrive4")):
                r = self.row(kind, key)
                msg = g.validate_selection(mode, r, True)
                self.assertIsNotNone(msg)
                self.assertIn(r.reason, msg)

    def test_compute_controls(self):
        e = self.row("volume", "E:")
        c = g.compute_controls(g.MODE_FREESPACE, e, True, running=False)
        self.assertTrue(c.start_enabled)
        self.assertFalse(c.verify_enabled)               # verify is whole-drive only
        c = g.compute_controls(g.MODE_DEVICE, e, True, running=False)
        self.assertTrue(c.start_enabled)
        self.assertTrue(c.verify_enabled)
        c = g.compute_controls(g.MODE_DEVICE, e, False, running=False)
        self.assertFalse(c.start_enabled)
        self.assertIn("administrator", c.message)
        c = g.compute_controls(g.MODE_FREESPACE, e, True, running=True)
        self.assertFalse(c.start_enabled)

    def test_build_request(self):
        e = self.row("volume", "E:")
        req = g.build_request(g.MODE_FREESPACE, e, "dod", True, False, False)
        self.assertEqual(req.passes, ("zero", "one", "random"))
        self.assertFalse(req.verify)                     # dropped in free-space mode
        self.assertEqual(req.target, "E:\\")
        d = self.row("disk", "PhysicalDrive2")
        req = g.build_request(g.MODE_DEVICE, d, "one", True, True, True)
        self.assertEqual((req.target, req.verify, req.dry_run, req.passes),
                         ("PhysicalDrive2", True, True, ("one",)))
        self.assertEqual(g.build_request(g.MODE_DEVICE, e, "zero", False, False, True).target, "E:")

    def test_build_request_refuses_invalid(self):
        with self.assertRaises(ValueError):
            g.build_request(g.MODE_DEVICE, self.row("disk", "PhysicalDrive0"), "zero", False,
                            False, True)
        with self.assertRaises(ValueError):
            g.build_request(g.MODE_DEVICE, self.row("disk", "PhysicalDrive2"), "zero", False,
                            False, False)
        with self.assertRaises(ValueError):
            g.build_request(g.MODE_FREESPACE, self.row("volume", "E:"), "bogus", False, False, True)

    def test_pass_choices_are_all_valid_specs(self):
        for choice in g.PASS_CHOICES:
            self.assertTrue(w.parse_passes(choice))


# --------------------------------------------------------------------------
# QueueProgress
# --------------------------------------------------------------------------

class QueueProgressTests(unittest.TestCase):
    def make(self, total=100, label="pass 1/2 (zero)", interval=0.5):
        self.clock = FakeClock()
        self.cancel = threading.Event()
        self.q = queue.Queue()
        return g.QueueProgress(total, label, self.q.put, self.cancel, self.clock, interval)

    def test_posts_phase_immediately(self):
        self.make()
        (m,) = drain(self.q)
        self.assertEqual((m.kind, m.text, m.pass_index, m.pass_count, m.phase),
                         ("phase", "Pass 1 of 2 (zero)", 1, 2, "pass"))

    def test_updates_are_throttled(self):
        p = self.make()
        drain(self.q)
        self.clock.t = 0.2
        p.update(10)
        self.assertEqual(drain(self.q), [])
        self.clock.t = 0.6
        p.update(10)
        (m,) = drain(self.q)
        self.assertEqual((m.kind, m.done, m.total), ("progress", 20, 100))
        self.assertAlmostEqual(m.percent, 20.0)
        self.clock.t = 0.7
        p.update(10)
        self.assertEqual(drain(self.q), [])
        self.assertEqual(p.done, 30)

    def test_finish_posts_once_with_final_numbers(self):
        p = self.make(total=10)
        drain(self.q)
        p.update(10)
        self.clock.t = 1.0
        p.finish()
        p.finish()
        (m,) = drain(self.q)
        self.assertEqual(m.percent, 100.0)
        self.assertEqual(m.eta, 0.0)

    def test_cancel_raises_from_update(self):
        p = self.make()
        self.cancel.set()
        with self.assertRaises(g.Cancelled):
            p.update(1)

    def test_cancel_does_not_make_finish_raise(self):
        p = self.make()
        self.cancel.set()
        p.finish()                                       # runs in finally blocks

    def test_constructor_raises_when_already_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(g.Cancelled):
            g.QueueProgress(1, "x", lambda m: None, cancel)

    def test_no_progress_total_does_not_crash(self):
        p = self.make(total=0)
        self.clock.t = 1.0
        p.update(5)
        p.finish()

    def test_parse_phase_reads_the_labels_the_core_really_produces(self):
        labels = []

        def factory(total, label):
            labels.append(label)
            return w.Progress(total, label, stream=io.StringIO())
        w.wipe_stream(io.BytesIO(), 100, ["zero", "one"], 64, verify=True,
                      progress_factory=factory)
        with tempfile.TemporaryDirectory() as d:
            w.fill_free_space(d, ["zero", "random"], 16 * KB, opener=QuotaOpener(64 * KB),
                              disk_usage=big_free, progress_factory=factory)
        parsed = [g.parse_phase(label) for label in labels]
        self.assertEqual([p.title for p in parsed],
                         ["Pass 1 of 2 (zero)", "Pass 2 of 2 (one)", "Verify (one)",
                          "Pass 1 of 2 (zero)", "Pass 2 of 2 (random)"])
        self.assertEqual([p.kind for p in parsed], ["pass", "pass", "verify", "pass", "pass"])

    def test_unknown_label_is_shown_verbatim(self):
        p = g.parse_phase("something else")
        self.assertEqual((p.kind, p.title), ("other", "something else"))


# --------------------------------------------------------------------------
# worker: whole-device path (in-memory device, fake platform)
# --------------------------------------------------------------------------

class WorkerDeviceTests(unittest.TestCase):
    def req(self, plat, passes="zero", verify=False, dry_run=False, key="PhysicalDrive2",
            kind="disk", admin=True):
        return make_request(plat, g.MODE_DEVICE, kind, key, passes, verify, dry_run, admin)

    def test_multipass_with_verify_runs_to_completion_with_ordered_messages(self):
        p = platform()
        worker, msgs = run_worker(p, self.req(p, "zero,one,random", verify=True))
        done = terminal(msgs)
        self.assertEqual(done.kind, "done")
        self.assertIn("verified", done.text)
        self.assertEqual(p.open_calls, ["PhysicalDrive2"])
        self.assertTrue(p.cleaned_up)
        self.assertEqual(len(p.device.getvalue()), p.device_size)
        self.assertNotIn(p.device.getvalue(), (b"\x00" * p.device_size, b"\xff" * p.device_size))

        flow = [m for m in msgs if m.kind in ("start", "phase", "progress", "done")]
        self.assertEqual(flow[0].kind, "start")
        self.assertEqual(flow[0].pass_count, 3)
        self.assertEqual([m.text for m in flow if m.kind == "phase"],
                         ["Pass 1 of 3 (zero)", "Pass 2 of 3 (one)", "Pass 3 of 3 (random)",
                          "Verify (random)"])
        current = None
        for m in flow:                                   # progress always follows its phase
            if m.kind == "phase":
                current, last_pct = m, -1.0
            elif m.kind == "progress":
                self.assertIsNotNone(current)
                self.assertEqual(m.phase, current.phase)
                self.assertEqual(m.pass_index, current.pass_index)
                self.assertGreaterEqual(m.percent, last_pct)
                last_pct = m.percent
        # every phase ended at 100 %
        ends = []
        for a, b in zip(flow, flow[1:] + [None]):
            if a.kind == "progress" and (b is None or b.kind in ("phase", "done")):
                ends.append(a.percent)
        self.assertEqual(ends, [100.0] * 4)

        text = log_text(msgs)
        self.assertLess(text.index("Re-checking"), text.index("Pass 1 of 3 (zero) started"))
        self.assertLess(text.index("Pass 3 of 3 (random) started"),
                        text.index("Verify (random) started"))
        self.assertLess(text.index("Verify (random) started"), text.index("Verify passed"))

    def test_single_zero_pass_leaves_zeros(self):
        p = platform()
        _, msgs = run_worker(p, self.req(p))
        self.assertEqual(terminal(msgs).kind, "done")
        self.assertEqual(p.device.getvalue(), b"\x00" * p.device_size)

    def test_volume_target(self):
        p = platform()
        _, msgs = run_worker(p, self.req(p, key="E:", kind="volume"))
        self.assertEqual(terminal(msgs).kind, "done")
        self.assertEqual(p.open_calls, ["E:"])

    def test_dry_run_shows_summary_and_opens_nothing(self):
        p = platform()
        _, msgs = run_worker(p, self.req(p, "dod", verify=True, dry_run=True))
        self.assertEqual(terminal(msgs).kind, "done")
        self.assertIn("Dry run", terminal(msgs).text)
        self.assertEqual(p.open_calls, [])
        self.assertEqual(p.device.getvalue(), b"\xaa" * p.device_size)
        text = log_text(msgs)
        for needle in ("ALL DATA ON PhysicalDrive2 WILL BE DESTROYED", "Test Disk", "E:", "DATA",
                       "zero, one, random", "+ verify"):
            self.assertIn(needle, text)
        self.assertFalse([m for m in msgs if m.kind in ("phase", "progress")])

    def test_verify_mismatch_is_a_reported_failure(self):
        p = platform()

        class Bad(FakeDevice):
            def readinto(self, b):
                n = super().readinto(b)
                if n:
                    b[0] ^= 1
                return n
        p.device = Bad(b"\xaa" * p.device_size)
        _, msgs = run_worker(p, self.req(p, verify=True))
        t = terminal(msgs)
        self.assertEqual((t.kind, t.reason), ("failed", "verify"))
        self.assertIn("VERIFY FAILED", t.text)
        self.assertIn("offset 0", t.text)
        self.assertTrue(p.cleaned_up)

    def test_io_error_is_a_reported_failure(self):
        p = platform()

        class Failing(FakeDevice):
            def write(self, data):
                raise OSError(errno.EIO, "I/O error")
        p.device = Failing()
        _, msgs = run_worker(p, self.req(p))
        t = terminal(msgs)
        self.assertEqual((t.kind, t.reason), ("failed", "io"))
        self.assertIn("I/O error", t.text)
        self.assertTrue(p.cleaned_up)

    def test_open_failure_is_a_reported_failure(self):
        p = platform()

        @contextlib.contextmanager
        def failing(target):
            raise w.WipeError("volume in use, close Explorer/AV handles")
            yield
        p.open_target = failing
        _, msgs = run_worker(p, self.req(p))
        t = terminal(msgs)
        self.assertEqual((t.kind, t.reason), ("failed", "error"))
        self.assertIn("volume in use", t.text)

    def test_unexpected_exception_does_not_kill_the_thread_silently(self):
        p = platform()
        p.open_target = mock.Mock(side_effect=RuntimeError("boom"))
        _, msgs = run_worker(p, self.req(p))
        t = terminal(msgs)
        self.assertEqual((t.kind, t.reason), ("failed", "internal"))
        self.assertIn("Traceback", log_text(msgs))

    def test_cancel_mid_run_stops_releases_the_device_and_reports_cancelled(self):
        p = platform()
        holder = {}

        class CancelOnSecondWrite(FakeDevice):
            writes = 0

            def write(self, data):
                r = super().write(data)
                CancelOnSecondWrite.writes += 1
                if CancelOnSecondWrite.writes == 2:
                    holder["worker"].cancel()
                return r
        p.device = CancelOnSecondWrite(b"\xaa" * p.device_size)
        q = queue.Queue()
        worker = g.WipeWorker(p, self.req(p, "zero,one"), q, progress_interval=0)
        holder["worker"] = worker
        worker.run()
        msgs = drain(q)
        t = terminal(msgs)
        self.assertEqual(t.kind, "cancelled")
        self.assertEqual(CancelOnSecondWrite.writes, 2)          # stopped right after the cancel
        self.assertTrue(p.cleaned_up)                            # open_target's finally ran
        self.assertEqual([m.text for m in msgs if m.kind == "phase"], ["Pass 1 of 2 (zero)"])
        self.assertIn("released", t.text)

    def test_cancel_before_start_opens_nothing(self):
        p = platform()
        q = queue.Queue()
        worker = g.WipeWorker(p, self.req(p), q)
        worker.cancel()
        worker.run()
        self.assertEqual(terminal(drain(q)).kind, "cancelled")
        self.assertEqual(p.open_calls, [])

    # -- the re-check at Start ------------------------------------------------------
    def test_recheck_refuses_a_target_that_became_a_system_disk(self):
        p = platform()
        req = self.req(p)                                        # PhysicalDrive2 was fine...
        p.system.add("PhysicalDrive2")                           # ...then Windows moved onto it
        _, msgs = run_worker(p, req)
        t = terminal(msgs)
        self.assertEqual(t.kind, "refused")
        self.assertIn("system disk", t.text)
        self.assertEqual(p.open_calls, [])
        self.assertEqual(p.device.getvalue(), b"\xaa" * p.device_size)

    def test_recheck_refuses_when_the_system_flag_flips(self):
        p = platform()
        req = self.req(p)
        p.disks[1].is_system = True
        _, msgs = run_worker(p, req)
        self.assertEqual(terminal(msgs).kind, "refused")
        self.assertEqual(p.open_calls, [])

    def test_recheck_refuses_a_volume_whose_disk_became_a_system_disk(self):
        p = platform()
        req = self.req(p, key="E:", kind="volume")
        p.disks[1].is_system = True
        _, msgs = run_worker(p, req)
        self.assertEqual(terminal(msgs).kind, "refused")
        self.assertEqual(p.open_calls, [])

    def test_recheck_refuses_a_target_that_was_unplugged(self):
        p = platform()
        req = self.req(p)
        del p.disks[1]
        _, msgs = run_worker(p, req)
        t = terminal(msgs)
        self.assertEqual(t.kind, "refused")
        self.assertIn("no longer present", t.text)
        self.assertEqual(p.open_calls, [])

    def test_recheck_refuses_a_different_drive_with_the_same_id(self):
        p = platform()
        req = self.req(p)
        p.disks[1].model = "Somebody else's disk"
        _, msgs = run_worker(p, req)
        t = terminal(msgs)
        self.assertEqual(t.kind, "refused")
        self.assertIn("changed", t.text)
        self.assertEqual(p.open_calls, [])

    def test_recheck_refuses_when_a_refusal_rule_starts_to_apply(self):
        p = platform()
        req = self.req(p)
        p.disks[1].volumes.append(vol("Z:", "SPAN2", extents=3))
        _, msgs = run_worker(p, req)
        t = terminal(msgs)
        self.assertEqual(t.kind, "refused")
        self.assertIn("extents", t.text)
        self.assertEqual(p.open_calls, [])

    def test_worker_requires_admin_even_if_the_gui_was_wrong(self):
        p = platform(admin=False)
        _, msgs = run_worker(p, self.req(p, admin=True))          # request built as if elevated
        t = terminal(msgs)
        self.assertEqual(t.kind, "refused")
        self.assertIn("administrator", t.text)
        self.assertEqual(p.open_calls, [])

    def test_stray_stderr_from_the_core_reaches_the_log(self):
        p = platform()
        orig = w.wipe_device

        def noisy(*a, **k):
            print("warning: noisy core", file=sys.stderr)
            return orig(*a, **k)
        with mock.patch.object(w, "wipe_device", noisy):
            _, msgs = run_worker(p, self.req(p))
        self.assertIn("core: warning: noisy core", log_text(msgs))


# --------------------------------------------------------------------------
# worker in a real thread
# --------------------------------------------------------------------------

class WorkerThreadTests(unittest.TestCase):
    def test_runs_in_a_thread_and_ends_with_one_terminal_message(self):
        p = platform()
        q = queue.Queue()
        worker = g.WipeWorker(p, make_request(p, g.MODE_DEVICE, "disk", "PhysicalDrive2",
                                              "zero,one", True), q)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        msgs = drain(q)
        self.assertEqual(terminal(msgs).kind, "done")
        self.assertEqual(p.device.getvalue(), b"\xff" * p.device_size)

    def test_cancel_from_another_thread_stops_a_blocked_write(self):
        p = platform()
        entered, gate = threading.Event(), threading.Event()

        class Gated(FakeDevice):
            def write(self, data):
                entered.set()
                gate.wait(10)                                    # bounded: cannot hang the suite
                return super().write(data)
        p.device = Gated(b"\xaa" * p.device_size)
        q = queue.Queue()
        worker = g.WipeWorker(p, make_request(p, g.MODE_DEVICE, "disk", "PhysicalDrive2",
                                              "zero,one,random"), q)
        worker.start()
        self.assertTrue(entered.wait(10))
        self.assertTrue(worker.is_alive())
        worker.cancel()
        gate.set()
        worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(terminal(drain(q)).kind, "cancelled")
        self.assertTrue(p.cleaned_up)

    def test_no_message_follows_the_terminal_one(self):
        p = platform()
        q = queue.Queue()
        worker = g.WipeWorker(p, make_request(p, g.MODE_DEVICE, "disk", "PhysicalDrive2"), q)
        worker.start()
        worker.join(10)
        msgs = drain(q)
        self.assertIn(msgs[-1].kind, g.TERMINAL_KINDS)
        self.assertEqual(sum(m.kind in g.TERMINAL_KINDS for m in msgs), 1)


# --------------------------------------------------------------------------
# worker: free-space path (capped fake opener, temp directory)
# --------------------------------------------------------------------------

class CancelAfterOpener(QuotaOpener):
    """QuotaOpener that calls `trigger()` once `after` bytes were written in mode `mode`."""

    def __init__(self, quota, trigger, after, mode="wb"):
        super().__init__(quota)
        self.trigger, self.after, self.watch_mode = trigger, after, mode
        self.seen = 0

    def __call__(self, path, mode="rb", buffering=-1):
        f = super().__call__(path, mode, buffering)
        orig = f.write

        def write(data):
            n = orig(data)
            if mode == self.watch_mode:
                self.seen += n
                if self.seen >= self.after:
                    self.trigger()
            return n
        f.write = write
        return f


class WorkerFreeSpaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.plat = platform()

    def request(self, passes="zero", dry_run=False):
        return make_request(self.plat, g.MODE_FREESPACE, "volume", "E:", passes, False, dry_run,
                            chunk=64 * KB, target_override=self.dir)

    def worker(self, opener, passes="zero", dry_run=False):
        q = queue.Queue()
        wk = g.WipeWorker(self.plat, self.request(passes, dry_run), q, opener=opener,
                          disk_usage=big_free, progress_interval=0)
        return wk, q

    def test_two_pass_fill_completes_and_cleans_up(self):
        op = QuotaOpener(1 * MB)
        wk, q = self.worker(op, "zero,one")
        wk.run()
        msgs = drain(q)
        t = terminal(msgs)
        self.assertEqual(t.kind, "done")
        self.assertIn("1.0 MiB", t.text)
        self.assertEqual(op.total, 1 * MB)
        self.assertEqual([m.text for m in msgs if m.kind == "phase"],
                         ["Pass 1 of 2 (zero)", "Pass 2 of 2 (one)"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_cancel_mid_first_pass_deletes_the_fill_directory(self):
        holder = {}
        op = CancelAfterOpener(4 * MB, lambda: holder["wk"].cancel(), after=300 * KB)
        wk, q = self.worker(op, "zero,one")
        holder["wk"] = wk
        wk.run()
        msgs = drain(q)
        self.assertEqual(terminal(msgs).kind, "cancelled")
        self.assertGreater(op.total, 0)
        self.assertLess(op.total, 4 * MB)                        # it really stopped early
        self.assertLessEqual(op.total, 300 * KB + 64 * KB)       # within one chunk of the cancel
        self.assertEqual(os.listdir(self.dir), [])               # ~wipefree.* is gone

    def test_cancel_during_a_rewrite_pass_deletes_the_fill_directory(self):
        holder = {}
        op = CancelAfterOpener(512 * KB, lambda: holder["wk"].cancel(), after=100 * KB,
                               mode="r+b")
        wk, q = self.worker(op, "zero,one")
        holder["wk"] = wk
        wk.run()
        msgs = drain(q)
        self.assertEqual(terminal(msgs).kind, "cancelled")
        self.assertEqual([m.text for m in msgs if m.kind == "phase"],
                         ["Pass 1 of 2 (zero)", "Pass 2 of 2 (one)"])
        self.assertEqual(os.listdir(self.dir), [])

    def test_cancel_from_the_ui_thread_during_a_threaded_run(self):
        entered, gate = threading.Event(), threading.Event()

        class Gated(QuotaOpener):
            def __call__(self, path, mode="rb", buffering=-1):
                f = super().__call__(path, mode, buffering)
                orig = f.write

                def write(data):
                    entered.set()
                    gate.wait(10)
                    return orig(data)
                f.write = write
                return f
        wk, q = self.worker(Gated(4 * MB))
        wk.start()
        self.assertTrue(entered.wait(10))
        wk.cancel()
        gate.set()
        wk.join(10)
        self.assertFalse(wk.is_alive())
        self.assertEqual(terminal(drain(q)).kind, "cancelled")
        self.assertEqual(os.listdir(self.dir), [])

    def test_dry_run_opens_nothing(self):
        op = QuotaOpener(1 * MB)
        wk, q = self.worker(op, dry_run=True)
        wk.run()
        msgs = drain(q)
        self.assertEqual(terminal(msgs).kind, "done")
        self.assertEqual(op.opened, [])
        self.assertEqual(os.listdir(self.dir), [])
        self.assertIn("Free:", log_text(msgs))

    def test_io_error_mid_fill_is_a_reported_failure_and_cleans_up(self):
        class Broken(QuotaOpener):
            def __call__(self, path, mode="rb", buffering=-1):
                f = super().__call__(path, mode, buffering)
                orig = f.write
                count = [0]

                def write(data):
                    count[0] += 1
                    if count[0] > 2:
                        raise OSError(errno.EIO, "I/O error")
                    return orig(data)
                f.write = write
                return f
        wk, q = self.worker(Broken(None))
        wk.run()
        t = terminal(drain(q))
        self.assertEqual((t.kind, t.reason), ("failed", "io"))
        self.assertEqual(os.listdir(self.dir), [])

    def test_recheck_refuses_when_the_volume_became_a_system_volume(self):
        op = QuotaOpener(1 * MB)
        wk, q = self.worker(op)
        self.plat.disks[1].is_system = True
        wk.run()
        t = terminal(drain(q))
        self.assertEqual(t.kind, "refused")
        self.assertEqual(op.opened, [])
        self.assertEqual(os.listdir(self.dir), [])

    def test_recheck_refuses_when_the_volume_disappeared(self):
        op = QuotaOpener(1 * MB)
        wk, q = self.worker(op)
        self.plat.disks[1].volumes[:] = [v for v in self.plat.disks[1].volumes if v.letter != "E:"]
        wk.run()
        self.assertEqual(terminal(drain(q)).kind, "refused")
        self.assertEqual(op.opened, [])

    def test_stale_directory_warning_from_the_core_reaches_the_log(self):
        stale = os.path.join(self.dir, "~wipefree.deadbeef")
        os.mkdir(stale)
        wk, q = self.worker(QuotaOpener(32 * KB))
        wk.run()
        msgs = drain(q)
        self.assertEqual(terminal(msgs).kind, "done")
        self.assertIn("core: warning: stale directory", log_text(msgs))
        self.assertEqual(os.listdir(self.dir), ["~wipefree.deadbeef"])

    def test_cleanup_failure_is_visible_in_the_log(self):
        wk, q = self.worker(QuotaOpener(32 * KB))
        with mock.patch("os.remove", side_effect=PermissionError("locked")):
            wk.run()
        msgs = drain(q)
        self.assertIn("could not remove", log_text(msgs))
        self.assertIn("manually", log_text(msgs))


# --------------------------------------------------------------------------
# elevation (never calls the real ShellExecuteW)
# --------------------------------------------------------------------------

class ElevationTests(unittest.TestCase):
    def test_uses_runas_with_the_same_interpreter_and_script(self):
        calls = []

        def fake(*args):
            calls.append(args)
            return 42
        ok = g.relaunch_as_admin(shell_execute=fake, executable=r"C:\Py 3\python.exe",
                                 script=r"C:\my dir\wipedrive_gui.py", argv=["--x"],
                                 cwd=r"C:\work")
        self.assertTrue(ok)
        hwnd, verb, exe, params, cwd, show = calls[0]
        self.assertEqual((hwnd, verb, exe, cwd, show), (None, "runas", r"C:\Py 3\python.exe",
                                                        r"C:\work", 1))
        self.assertEqual(params, '"C:\\my dir\\wipedrive_gui.py" --x')

    def test_defaults_to_this_interpreter_and_this_script(self):
        calls = []
        g.relaunch_as_admin(shell_execute=lambda *a: calls.append(a) or 33, argv=[])
        _, verb, exe, params, _, _ = calls[0]
        self.assertEqual(verb, "runas")
        self.assertEqual(exe, sys.executable)
        self.assertIn("wipedrive_gui.py", params)

    def test_failure_codes_and_exceptions_mean_not_relaunched(self):
        self.assertFalse(g.relaunch_as_admin(shell_execute=lambda *a: 5))        # UAC declined
        self.assertFalse(g.relaunch_as_admin(shell_execute=lambda *a: 32))
        self.assertFalse(g.relaunch_as_admin(shell_execute=lambda *a: None))

        def boom(*a):
            raise OSError("nope")
        self.assertFalse(g.relaunch_as_admin(shell_execute=boom))


# --------------------------------------------------------------------------
# widgets (need a display; skipped cleanly without one)
# --------------------------------------------------------------------------

def make_root(testcase):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise unittest.SkipTest("no display available: %s" % exc)
    root.withdraw()

    def cleanup():
        try:
            root.destroy()
        except tk.TclError:
            pass
        # Free Tk wrappers (and their reference cycles) on this thread: if the cyclic GC
        # ran inside a worker thread instead, Tcl aborts with "async handler deleted by the
        # wrong thread".
        gc.collect()
    testcase.addCleanup(cleanup)
    return root


def pump_until(root, predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            root.update()
        except tk.TclError:
            return predicate()
        if predicate():
            return True
        time.sleep(0.02)
    return False


class ConfirmDialogTests(unittest.TestCase):
    """Builds the dialog without show(): exercises the enable/disable rule only."""

    def setUp(self):
        self.root = make_root(self)
        lines = w.device_summary_lines(make_disk(), ["zero"], False)
        self.dlg = g.ConfirmWipeDialog(self.root, "PhysicalDrive2", lines)
        self.root.update()

    def state(self):
        return str(self.dlg.wipe_button.cget("state"))

    def type(self, text):
        self.dlg.var.set(text)
        self.root.update()

    def test_wipe_is_disabled_until_the_id_is_typed_exactly(self):
        self.assertEqual(self.state(), "disabled")
        for wrong in ("physicaldrive2", "PhysicalDrive", "PhysicalDrive2 ", " PhysicalDrive2",
                      "PhysicalDrive3", "yes", ""):
            self.type(wrong)
            self.assertEqual(self.state(), "disabled", repr(wrong))
        self.type("PhysicalDrive2")
        self.assertEqual(self.state(), "normal")
        self.type("PhysicalDrive2x")
        self.assertEqual(self.state(), "disabled")

    def test_enter_key_cannot_bypass_the_check(self):
        self.type("nope")
        self.dlg._ok()
        self.assertFalse(self.dlg.result)
        self.assertTrue(self.dlg.top.winfo_exists())

    def test_correct_text_then_ok_confirms(self):
        self.type("PhysicalDrive2")
        self.dlg._ok()
        self.assertTrue(self.dlg.result)

    def test_cancel_declines(self):
        self.type("PhysicalDrive2")
        self.dlg._cancel()
        self.assertFalse(self.dlg.result)

    def test_dialog_shows_the_destruction_summary(self):
        shown = self.dlg.top.nametowidget(
            [c for c in self.dlg.top.winfo_children()[0].winfo_children()
             if isinstance(c, tk.Text)][0])
        text = shown.get("1.0", "end")
        for needle in ("PhysicalDrive2", "Test Disk", "E:", "DATA", "NTFS",
                       "ALL DATA ON PhysicalDrive2 WILL BE DESTROYED"):
            self.assertIn(needle, text)


class AppWithFakePlatformTests(unittest.TestCase):
    """The real widgets on a fake platform.  Start is never invoked."""

    def setUp(self):
        self.root = make_root(self)
        self.plat = platform()
        self.app = g.App(self.root, self.plat)
        self.assertTrue(pump_until(self.root, lambda: self.app.inventory is not None))

    def iid(self, kind, key):
        return self.app.inventory.find(kind, key).iid

    def test_rows_are_in_the_tree_and_disabled_ones_are_tagged(self):
        tree = self.app.tree
        self.assertEqual(len(tree.get_children("")), 4)
        sys_iid = self.iid("disk", "PhysicalDrive0")
        self.assertIn("disabled", tree.item(sys_iid, "tags"))
        self.assertEqual(tree.set(sys_iid, "note"), "system disk")
        self.assertEqual(len(tree.get_children(sys_iid)), 2)
        self.assertNotIn("disabled", tree.item(self.iid("volume", "E:"), "tags"))
        self.assertIn("extents", tree.set(self.iid("disk", "PhysicalDrive3"), "note"))

    def test_selecting_a_system_disk_or_its_volume_does_not_stick(self):
        tree = self.app.tree
        for kind, key in (("disk", "PhysicalDrive0"), ("volume", "C:"),
                          ("disk", "PhysicalDrive3"), ("disk", "PhysicalDrive4")):
            tree.selection_set(self.iid(kind, key))
            self.root.update()
            self.assertEqual(tree.selection(), (), key)
            self.assertIsNone(self.app._selected)
            self.assertEqual(str(self.app.start_button.cget("state")), "disabled")

    def test_disabled_row_reverts_to_the_previous_valid_selection(self):
        tree = self.app.tree
        e = self.iid("volume", "E:")
        tree.selection_set(e)
        self.root.update()
        self.assertEqual(tree.selection(), (e,))
        tree.selection_set(self.iid("volume", "C:"))
        self.root.update()
        self.assertEqual(tree.selection(), (e,))

    def test_mode_and_selection_drive_the_start_button(self):
        app, tree = self.app, self.app.tree

        def start():
            self.root.update()
            return str(app.start_button.cget("state"))
        self.assertEqual(start(), "disabled")                     # nothing selected
        tree.selection_set(self.iid("disk", "PhysicalDrive2"))
        self.root.update()
        self.assertEqual(start(), "disabled")                     # free space needs a volume
        self.assertIn("volume", app.msg_var.get())
        tree.selection_set(self.iid("volume", "E:"))
        self.assertEqual(start(), "normal")
        app.admin = False
        app.mode_var.set(g.MODE_DEVICE)
        app._update_controls()
        self.assertEqual(start(), "disabled")                     # whole drive needs admin
        self.assertIn("administrator", app.msg_var.get())
        app.admin = True
        app._update_controls()
        self.assertEqual(start(), "normal")
        self.assertEqual(str(app.verify_check.cget("state")), "normal")
        app.mode_var.set(g.MODE_FREESPACE)
        app._update_controls()
        self.assertEqual(str(app.verify_check.cget("state")), "disabled")

    def test_refresh_keeps_a_still_valid_selection(self):
        e = self.iid("volume", "E:")
        self.app.tree.selection_set(e)
        self.root.update()
        old = self.app.inventory
        self.app.refresh()
        self.assertTrue(pump_until(self.root, lambda: self.app.inventory is not old))
        self.assertEqual(self.app.tree.selection(), (e,))


class FakeRunningWorker:
    """Stands in for a WipeWorker that is 'busy' until released; nothing is written."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self.alive = True
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self.cancel_event.set()

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        pass


class CloseWhileRunningTests(unittest.TestCase):
    def setUp(self):
        self.root = make_root(self)
        self.app = g.App(self.root, platform())
        self.assertTrue(pump_until(self.root, lambda: self.app.inventory is not None))
        self.fake = FakeRunningWorker()
        self.app.worker = self.fake
        self.app._run_active = True

    def alive(self):
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def test_declining_the_prompt_keeps_running(self):
        with mock.patch.object(g.messagebox, "askyesno", return_value=False) as ask:
            self.app.on_close()
        ask.assert_called_once()
        self.assertFalse(self.fake.cancelled)
        self.assertTrue(self.alive())

    def test_accepting_cancels_then_waits_for_cleanup_before_destroying(self):
        with mock.patch.object(g.messagebox, "askyesno", return_value=True):
            self.app.on_close()
        self.assertTrue(self.fake.cancelled)
        self.assertTrue(self.app._closing)
        self.assertTrue(self.alive())
        for _ in range(10):                                   # worker still cleaning up
            self.root.update()
            time.sleep(0.03)
        self.assertTrue(self.alive())
        self.fake.alive = False                               # cleanup finished
        self.assertTrue(pump_until(self.root, lambda: not self.alive(), seconds=5))

    def test_nothing_running_closes_without_asking(self):
        self.app._run_active = False
        with mock.patch.object(g.messagebox, "askyesno") as ask:
            self.app.on_close()
        ask.assert_not_called()
        self.assertFalse(self.alive())

    def test_terminal_message_while_closing_shows_no_dialog(self):
        with mock.patch.object(g.messagebox, "askyesno", return_value=True):
            self.app.on_close()
        with mock.patch.object(g.messagebox, "showerror") as err:
            self.app._handle(g.Msg("failed", "boom", reason="io"))
        err.assert_not_called()

    def test_cancel_button_requests_cancellation(self):
        self.app.on_cancel()
        self.assertTrue(self.fake.cancelled)
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")

    def test_refusal_is_logged_and_shown_in_a_message_box(self):
        with mock.patch.object(g.messagebox, "showerror") as err, \
                mock.patch.object(self.app, "refresh"):
            self.app._handle(g.Msg("refused", "Refused: PhysicalDrive2 holds Windows"))
        err.assert_called_once()
        self.assertIn("holds Windows", self.app.log.get("1.0", "end"))
        self.assertFalse(self.app._run_active)
        self.assertEqual(str(self.app.start_button.cget("state")), "disabled")   # nothing selected


class SmokeTest(unittest.TestCase):
    """Real window, real (read-only) drive listing.  Never selects anything that could be
    started and never presses Start."""

    def test_window_builds_and_shows_the_system_disk_unselectable(self):
        root = make_root(self)
        real = w.get_platform()
        try:
            system = real.system_ids()
        except w.WipeError as exc:
            self.skipTest("cannot identify the system disk here: %s" % exc)
        app = g.App(root, real)
        self.assertTrue(pump_until(root, lambda: app.inventory is not None, seconds=30),
                        "drive listing never arrived")
        root.update()
        sys_rows = [r for r in app.inventory.rows if r.kind == "disk" and r.disk.id in system]
        self.assertTrue(sys_rows, "system disk missing from the list")
        for r in sys_rows:
            self.assertTrue(app.tree.exists(r.iid))
            self.assertFalse(r.selectable)
            self.assertIn("disabled", app.tree.item(r.iid, "tags"))
            self.assertEqual(app.tree.set(r.iid, "note"), "system disk")
            app.tree.selection_set(r.iid)
            root.update()
            self.assertEqual(app.tree.selection(), ())
            for child in app.tree.get_children(r.iid):
                app.tree.selection_set(child)
                root.update()
                self.assertEqual(app.tree.selection(), ())
        self.assertEqual(str(app.start_button.cget("state")), "disabled")
        root.destroy()


if __name__ == "__main__":
    unittest.main()
