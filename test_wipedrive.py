"""Unit tests for wipedrive.py.

No test opens a real device, needs admin, or fills a real volume.  Free-space tests use a
TemporaryDirectory with an injected opener that raises ENOSPC after N bytes and a fake
disk_usage, so writes are capped at a few MB.  Platform-dependent CLI tests use a fake platform.
"""

import contextlib
import errno
import io
import os
import struct
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest import mock

import wipedrive as w
from wipedrive import DiskInfo, VolumeInfo

Usage = namedtuple("Usage", "total used free")
KB = 1024
MB = 1024 * 1024


def quiet_progress(total, label):
    return w.Progress(total, label, stream=io.StringIO())


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

class ParseSizeTests(unittest.TestCase):
    def test_units_are_binary(self):
        self.assertEqual(w.parse_size("4M"), 4 * MB)
        self.assertEqual(w.parse_size("512K"), 512 * KB)
        self.assertEqual(w.parse_size("1G"), 1 << 30)
        self.assertEqual(w.parse_size("1T"), 1 << 40)

    def test_plain_bytes_case_and_suffixes(self):
        self.assertEqual(w.parse_size("4096"), 4096)
        self.assertEqual(w.parse_size("4m"), 4 * MB)
        self.assertEqual(w.parse_size(" 4 MiB "), 4 * MB)
        self.assertEqual(w.parse_size("2KB"), 2048)
        self.assertEqual(w.parse_size("7B"), 7)

    def test_bad_input(self):
        for bad in ("", "abc", "-1M", "1.5M", "4X", "M", "4MM", "0x10", "4 4"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                w.parse_size(bad)


class FormatSizeTests(unittest.TestCase):
    def test_values(self):
        self.assertEqual(w.format_size(0), "0 B")
        self.assertEqual(w.format_size(1023), "1023 B")
        self.assertEqual(w.format_size(1024), "1.0 KiB")
        self.assertEqual(w.format_size(1536), "1.5 KiB")
        self.assertEqual(w.format_size(4 * MB), "4.0 MiB")
        self.assertEqual(w.format_size(1 << 30), "1.0 GiB")
        self.assertEqual(w.format_size(4000787030016), "3.6 TiB")

    def test_roundtrip_is_close(self):
        self.assertEqual(w.format_size(w.parse_size("512K")), "512.0 KiB")


class ParsePassesTests(unittest.TestCase):
    def test_lists_and_presets(self):
        self.assertEqual(w.parse_passes("zero"), ["zero"])
        self.assertEqual(w.parse_passes("zero,one,random"), ["zero", "one", "random"])
        self.assertEqual(w.parse_passes("dod"), ["zero", "one", "random"])
        self.assertEqual(w.parse_passes(" Random , ZERO "), ["random", "zero"])
        self.assertEqual(w.parse_passes("one,dod"), ["one", "zero", "one", "random"])

    def test_bad_input(self):
        for bad in ("", "  ", "foo", "zero,foo", "zero,,one", "zero,", ",", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                w.parse_passes(bad)


# --------------------------------------------------------------------------
# patterns
# --------------------------------------------------------------------------

class PatternTests(unittest.TestCase):
    def test_fixed_patterns(self):
        zero, one = w.pattern_source("zero", 1), w.pattern_source("one", 1)
        for n in (1, 5, 4096, MB, 3 * MB + 7):      # includes sizes beyond the first cache
            self.assertEqual(zero(n), b"\x00" * n)
            self.assertEqual(one(n), b"\xff" * n)
            self.assertIsInstance(zero(n), bytes)

    def test_random_reproducible_with_same_seed_and_sizes(self):
        a, b = w.pattern_source("random", 42), w.pattern_source("random", 42)
        self.assertEqual([a(100), a(37), a(4096)], [b(100), b(37), b(4096)])

    def test_random_differs_between_seeds(self):
        self.assertNotEqual(w.pattern_source("random", 1)(4096),
                            w.pattern_source("random", 2)(4096))

    def test_random_chunk_sizes_matter(self):
        """Documents why verify must ask for the same chunk sizes as the write."""
        a, b = w.pattern_source("random", 7), w.pattern_source("random", 7)
        self.assertNotEqual(a(5) + a(3), b(8))

    def test_unknown_pattern(self):
        with self.assertRaises(ValueError):
            w.pattern_source("nope", 0)


# --------------------------------------------------------------------------
# progress
# --------------------------------------------------------------------------

class ProgressTests(unittest.TestCase):
    def test_redraws_at_most_every_half_second(self):
        clock, out = FakeClock(), io.StringIO()
        p = w.Progress(100 * MB, "label", stream=out, clock=clock)
        clock.t = 0.2
        p.update(10 * MB)
        self.assertEqual(out.getvalue(), "")                  # too soon
        clock.t = 0.6
        p.update(10 * MB)
        first = out.getvalue()
        self.assertTrue(first.startswith("\r"))
        self.assertIn("label", first)
        self.assertIn("20.0%", first)
        self.assertIn("MiB/s", first)
        self.assertIn("ETA", first)
        clock.t = 0.9
        p.update(10 * MB)
        self.assertEqual(out.getvalue(), first)               # still within 0.5 s of last draw

    def test_rate_and_eta(self):
        clock, out = FakeClock(), io.StringIO()
        p = w.Progress(100 * MB, "x", stream=out, clock=clock)
        clock.t = 10.0
        p.update(50 * MB)                                     # 5 MiB/s, 10 s to go
        text = out.getvalue()
        self.assertIn("50.0%", text)
        self.assertIn("5.0 MiB/s", text)
        self.assertIn("ETA 0:00:10", text)

    def test_finish_draws_once_and_ends_line(self):
        clock, out = FakeClock(), io.StringIO()
        p = w.Progress(10, "x", stream=out, clock=clock)
        p.update(10)
        clock.t = 1.0
        p.finish()
        p.finish()
        self.assertEqual(out.getvalue().count("\n"), 1)
        self.assertIn("100.0%", out.getvalue())

    def test_zero_total_and_overrun_do_not_crash(self):
        clock, out = FakeClock(), io.StringIO()
        p = w.Progress(0, "x", stream=out, clock=clock)
        clock.t = 1
        p.update(5)
        p.finish()
        q = w.Progress(10, "y", stream=out, clock=clock)
        clock.t = 2
        q.update(50)
        q.finish()
        self.assertIn("100.0%", out.getvalue())


# --------------------------------------------------------------------------
# write / verify against BytesIO
# --------------------------------------------------------------------------

class ShortWriter(io.BytesIO):
    """Accepts at most 7 bytes per write call."""

    def write(self, data):
        return super().write(bytes(data[:7]))


class WriteVerifyTests(unittest.TestCase):
    def test_write_pass_size_not_multiple_of_chunk(self):
        f = io.BytesIO()
        n = w.write_pass(f, 10000, w.pattern_source("one", 0), 4096)
        self.assertEqual(n, 10000)
        self.assertEqual(f.getvalue(), b"\xff" * 10000)

    def test_write_pass_seeks_to_start(self):
        f = io.BytesIO(b"\x11" * 100)
        f.seek(77)
        w.write_pass(f, 100, w.pattern_source("zero", 0), 64)
        self.assertEqual(f.getvalue(), b"\x00" * 100)

    def test_write_pass_handles_short_writes(self):
        f = ShortWriter()
        w.write_pass(f, 100, w.pattern_source("one", 0), 30)
        self.assertEqual(f.getvalue(), b"\xff" * 100)

    def test_write_pass_zero_progress_write_raises(self):
        class Stuck(io.BytesIO):
            def write(self, data):
                return 0
        with self.assertRaises(w.WipeError):
            w.write_pass(Stuck(), 10, w.pattern_source("zero", 0), 4)

    def test_progress_receives_bytes(self):
        p = w.Progress(10000, "x", stream=io.StringIO())
        w.write_pass(io.BytesIO(), 10000, w.pattern_source("zero", 0), 4096, p)
        self.assertEqual(p.done, 10000)

    def test_verify_pass_ok_for_every_pattern(self):
        for name in ("zero", "one", "random"):
            with self.subTest(name=name):
                f = io.BytesIO()
                w.write_pass(f, 10000, w.pattern_source(name, 99), 4096)
                w.verify_pass(f, 10000, w.pattern_source(name, 99), 4096)

    def test_flipped_byte_reports_offset(self):
        for offset in (0, 4095, 4096, 5000, 9999):
            with self.subTest(offset=offset):
                f = io.BytesIO()
                w.write_pass(f, 10000, w.pattern_source("random", 5), 4096)
                data = bytearray(f.getvalue())
                data[offset] ^= 0x01
                with self.assertRaises(w.VerifyError) as cm:
                    w.verify_pass(io.BytesIO(bytes(data)), 10000,
                                  w.pattern_source("random", 5), 4096)
                self.assertEqual(cm.exception.offset, offset)

    def test_wrong_seed_fails_at_zero(self):
        f = io.BytesIO()
        w.write_pass(f, 5000, w.pattern_source("random", 1), 4096)
        with self.assertRaises(w.VerifyError) as cm:
            w.verify_pass(f, 5000, w.pattern_source("random", 2), 4096)
        self.assertEqual(cm.exception.offset, 0)

    def test_truncated_device_is_a_verify_error(self):
        f = io.BytesIO(b"\x00" * 6000)
        with self.assertRaises(w.VerifyError) as cm:
            w.verify_pass(f, 10000, w.pattern_source("zero", 0), 4096)
        self.assertEqual(cm.exception.offset, 6000)

    def test_verify_handles_short_reads(self):
        class Dribble(io.BytesIO):
            def readinto(self, b):
                return super().readinto(memoryview(b)[:100])
        f = io.BytesIO()
        w.write_pass(f, 1000, w.pattern_source("one", 0), 512)
        w.verify_pass(Dribble(f.getvalue()), 1000, w.pattern_source("one", 0), 512)


class WipeStreamTests(unittest.TestCase):
    def test_multipass_leaves_last_pattern(self):
        for passes, expected in ((["zero", "one"], b"\xff"), (["one", "zero"], b"\x00"),
                                 (["random", "zero"], b"\x00"), (["zero", "one", "zero"], b"\x00")):
            with self.subTest(passes=passes):
                f = io.BytesIO(b"\x5a" * 10000)
                w.wipe_stream(f, 10000, passes, 4096, verify=True,
                              progress_factory=quiet_progress)
                self.assertEqual(f.getvalue(), expected * 10000)

    def test_last_random_pass_is_what_remains_and_verifies(self):
        f = io.BytesIO(b"\x00" * 10000)
        w.wipe_stream(f, 10000, ["zero", "one", "random"], 4096, verify=True,
                      progress_factory=quiet_progress)
        data = f.getvalue()
        self.assertEqual(len(data), 10000)
        self.assertNotIn(data, (b"\x00" * 10000, b"\xff" * 10000))

    def test_each_random_pass_gets_a_new_seed(self):
        class Snap(io.BytesIO):
            snaps = []

            def flush(self):
                Snap.snaps.append(self.getvalue())
                super().flush()
        Snap.snaps = []
        w.wipe_stream(Snap(), 4096, ["random", "random"], 4096)
        self.assertEqual(len(Snap.snaps), 2)
        self.assertNotEqual(Snap.snaps[0], Snap.snaps[1])

    def test_only_last_pass_is_verified(self):
        class Counting(io.BytesIO):
            reads = 0

            def readinto(self, b):
                Counting.reads += 1
                return super().readinto(b)
        Counting.reads = 0
        w.wipe_stream(Counting(), 10000, ["zero", "one", "random"], 4096, verify=True)
        self.assertEqual(Counting.reads, 3)                   # 10000 bytes / 4096 = 3 chunks
        Counting.reads = 0
        w.wipe_stream(Counting(), 10000, ["zero", "one", "random"], 4096, verify=False)
        self.assertEqual(Counting.reads, 0)

    def test_corruption_during_verify_raises(self):
        class Bad(io.BytesIO):
            def readinto(self, b):
                n = super().readinto(b)
                if n:
                    b[0] ^= 0xFF
                return n
        with self.assertRaises(w.VerifyError) as cm:
            w.wipe_stream(Bad(), 5000, ["zero"], 4096, verify=True)
        self.assertEqual(cm.exception.offset, 0)

    def test_progress_label_and_finish(self):
        labels = []

        def factory(total, label):
            labels.append(label)
            return quiet_progress(total, label)
        w.wipe_stream(io.BytesIO(), 100, ["zero", "one"], 64, verify=True, progress_factory=factory)
        self.assertEqual(len(labels), 3)                      # 2 passes + verify
        self.assertIn("1/2", labels[0])
        self.assertIn("verify", labels[2])

    def test_no_passes_rejected(self):
        with self.assertRaises(ValueError):
            w.wipe_stream(io.BytesIO(), 10, [], 4096)


# --------------------------------------------------------------------------
# disk-full detection
# --------------------------------------------------------------------------

class IsDiskFullTests(unittest.TestCase):
    def test_enospc(self):
        self.assertTrue(w.is_disk_full(OSError(errno.ENOSPC, "full")))

    def test_windows_codes(self):
        class E39(OSError):
            winerror = 39

        class E112(OSError):
            winerror = 112
        self.assertTrue(w.is_disk_full(E39()))
        self.assertTrue(w.is_disk_full(E112()))

    def test_other_errors(self):
        self.assertFalse(w.is_disk_full(OSError(errno.EACCES, "denied")))
        self.assertFalse(w.is_disk_full(ValueError("x")))

    @unittest.skipUnless(sys.platform == "win32", "Windows errno mapping")
    def test_windows_constructor_mapping(self):
        # Risk 6: Python maps 112 to ENOSPC but 39 to EINVAL, so the winerror check is needed.
        self.assertTrue(w.is_disk_full(OSError(0, "x", None, 112)))
        self.assertTrue(w.is_disk_full(OSError(0, "x", None, 39)))
        self.assertFalse(w.is_disk_full(OSError(0, "x", None, 5)))


# --------------------------------------------------------------------------
# free-space fill, with fake opener and fake disk_usage
# --------------------------------------------------------------------------

class QuotaFile:
    """Wraps a real unbuffered file.  Allows `owner.remaining` more new bytes (None = unlimited),
    then raises ENOSPC.  partial=True writes the part that still fits and returns a short count
    (like a nearly-full disk); partial=False refuses the whole write."""

    def __init__(self, owner, path, mode):
        self.owner, self.path, self.mode = owner, path, mode
        self._f = open(path, mode, buffering=0)

    def write(self, data):
        o = self.owner
        n = len(data)
        counted = "w" in self.mode                 # rewriting in place never needs new space
        if counted and o.total + n > o.HARD_CAP:
            # Safety net: if production code ever loses its own limit (guard, quota), fail the
            # test instead of filling the real temp volume.
            raise AssertionError("runaway test write: %d bytes exceeds the %d-byte hard cap"
                                 % (o.total + n, o.HARD_CAP))
        if counted and o.remaining is not None:
            if o.remaining <= 0 or (n > o.remaining and not o.partial):
                raise OSError(errno.ENOSPC, "No space left on device")
            n = min(n, o.remaining)
        if o.interrupt_after is not None and counted and o.total >= o.interrupt_after:
            raise KeyboardInterrupt
        w_ = self._f.write(memoryview(data)[:n])
        if counted:
            o.total += w_
            if o.remaining is not None:
                o.remaining -= w_
        return w_

    def seek(self, *a):
        return self._f.seek(*a)

    def flush(self):
        pass

    def fileno(self):
        return self._f.fileno()

    def close(self):
        self.owner.closed_sizes[self.path] = os.path.getsize(self.path)
        self._f.flush()
        with open(self.path, "rb") as fh:
            self.owner.last_content[self.path] = fh.read()
        self._f.close()


class QuotaOpener:
    HARD_CAP = 16 * MB

    def __init__(self, quota=None, partial=True, interrupt_after=None, fail_open_after_files=None):
        self.remaining, self.partial, self.interrupt_after = quota, partial, interrupt_after
        self.fail_open_after_files = fail_open_after_files
        self.total = 0
        self.opened = []                    # (path, mode)
        self.closed_sizes = {}
        self.last_content = {}

    def __call__(self, path, mode="rb", buffering=-1):
        assert buffering == 0
        if "w" in mode:
            new_files = sum(1 for _, m in self.opened if "w" in m)
            if self.fail_open_after_files is not None and new_files >= self.fail_open_after_files:
                raise OSError(errno.ENOSPC, "No space left on device")
        self.opened.append((path, mode))
        return QuotaFile(self, path, mode)


def big_free(_path):
    return Usage(10 ** 12, 0, 10 ** 12)


class FillFreeSpaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def assertClean(self):
        self.assertEqual(os.listdir(self.dir), [])

    def test_fills_to_quota_and_cleans_up(self):
        quota = 3 * MB + 123                                   # not a multiple of any chunk
        op = QuotaOpener(quota)
        n = w.fill_free_space(self.dir, ["zero"], 256 * KB, max_file_size=1 * MB, opener=op,
                              disk_usage=big_free)
        self.assertEqual(n, quota)
        self.assertEqual(op.total, quota)
        self.assertEqual(sum(op.closed_sizes.values()), quota)
        self.assertClean()

    def test_all_or_nothing_writes_squeeze_the_tail(self):
        quota = 3 * MB + 5 * 4096                              # multiple of 4096, not of chunk
        op = QuotaOpener(quota, partial=False)
        n = w.fill_free_space(self.dir, ["zero"], 1 * MB, max_file_size=4 * MB, opener=op,
                              disk_usage=big_free)
        self.assertEqual(n, quota)                             # halved 1M -> ... -> 4096
        self.assertClean()

    def test_small_max_file_size_splits_files(self):
        op = QuotaOpener(1 * MB)                               # exactly fills 4 x 256K files...
        n = w.fill_free_space(self.dir, ["zero"], 64 * KB, max_file_size=256 * KB, opener=op,
                              disk_usage=big_free)
        self.assertEqual(n, 1 * MB)
        sizes = list(op.closed_sizes.values())
        self.assertTrue(all(s <= 256 * KB for s in sizes))
        self.assertEqual(sorted(sizes, reverse=True)[:4], [256 * KB] * 4)
        names = sorted(os.path.basename(p) for p in op.closed_sizes)
        self.assertEqual(names[:2], ["fill_00000.bin", "fill_00001.bin"])
        self.assertClean()

    def test_disk_full_when_creating_a_file_means_done(self):
        op = QuotaOpener(None, fail_open_after_files=2)        # third create fails with ENOSPC
        n = w.fill_free_space(self.dir, ["zero"], 64 * KB, max_file_size=128 * KB, opener=op,
                              disk_usage=big_free)
        self.assertEqual(n, 256 * KB)
        self.assertEqual(len(op.closed_sizes), 2)
        self.assertClean()

    def test_files_live_in_a_wipefree_subdirectory(self):
        seen = []
        real = QuotaOpener(64 * KB)

        def spy(path, mode, buffering=0):
            seen.append(os.path.dirname(path))
            return real(path, mode, buffering)
        w.fill_free_space(self.dir, ["zero"], 16 * KB, opener=spy, disk_usage=big_free)
        sub = os.path.basename(seen[0])
        self.assertRegex(sub, r"^~wipefree\.[0-9a-f]{8}$")
        self.assertEqual(os.path.dirname(seen[0]), self.dir)

    def test_extra_passes_rewrite_same_files_in_place(self):
        op = QuotaOpener(1 * MB)
        n = w.fill_free_space(self.dir, ["zero", "one"], 64 * KB, max_file_size=256 * KB,
                              opener=op, disk_usage=big_free)
        self.assertEqual(n, 1 * MB)
        created = [p for p, m in op.opened if "w" in m]
        rewritten = [p for p, m in op.opened if m == "r+b"]
        self.assertEqual(sorted(rewritten), sorted(p for p in created
                                                   if op.closed_sizes[p] > 0))
        for p in rewritten:                                     # last pass left 0xFF behind
            self.assertEqual(op.last_content[p], b"\xff" * op.closed_sizes[p])
        self.assertClean()

    def test_progress_is_reported(self):
        labels = []

        def factory(total, label):
            labels.append((total, label))
            return quiet_progress(total, label)
        w.fill_free_space(self.dir, ["zero", "one"], 64 * KB, opener=QuotaOpener(256 * KB),
                          disk_usage=lambda p: Usage(10 ** 9, 0, 10 ** 9), progress_factory=factory)
        self.assertEqual(labels[0][0], 10 ** 9)
        self.assertEqual(len(labels), 2)

    def test_keyboard_interrupt_cleans_up_and_reraises(self):
        op = QuotaOpener(None, interrupt_after=300 * KB)
        with self.assertRaises(KeyboardInterrupt):
            w.fill_free_space(self.dir, ["zero"], 64 * KB, max_file_size=128 * KB, opener=op,
                              disk_usage=big_free)
        self.assertGreater(op.total, 0)                        # it did write something first
        self.assertClean()

    def test_keyboard_interrupt_in_second_pass_also_cleans_up(self):
        class InterruptOnRewrite(QuotaOpener):
            def __call__(self, path, mode="rb", buffering=-1):
                if mode == "r+b":
                    raise KeyboardInterrupt
                return super().__call__(path, mode, buffering)
        with self.assertRaises(KeyboardInterrupt):
            w.fill_free_space(self.dir, ["zero", "one"], 64 * KB, opener=InterruptOnRewrite(128 * KB),
                              disk_usage=big_free)
        self.assertClean()

    def test_other_os_error_propagates_and_cleans_up(self):
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
        with self.assertRaises(OSError) as cm:
            w.fill_free_space(self.dir, ["zero"], 16 * KB, opener=Broken(None), disk_usage=big_free)
        self.assertEqual(cm.exception.errno, errno.EIO)
        self.assertClean()

    def test_overwrite_guard_trips_on_hidden_free_space(self):
        # Pretend 1 MiB is free and never run out: a compressing/deduping/thin volume.
        op = QuotaOpener(None)
        with self.assertRaises(w.WipeError) as cm:
            w.fill_free_space(self.dir, ["zero"], 64 * KB, max_file_size=256 * KB, opener=op,
                              disk_usage=lambda p: Usage(10 * MB, 9 * MB, 1 * MB), guard_slack=0)
        self.assertRegex(str(cm.exception), r"compression|dedup")
        limit = 1 * MB + (1 * MB) // 100
        self.assertGreater(op.total, limit)
        self.assertLessEqual(op.total, limit + 64 * KB)        # stopped within one chunk
        self.assertClean()

    def test_guard_allows_free_space_plus_one_percent_plus_slack(self):
        free = 1 * MB
        op = QuotaOpener(free + 10 * KB)                       # real disks report slightly off
        n = w.fill_free_space(self.dir, ["zero"], 64 * KB, opener=op,
                              disk_usage=lambda p: Usage(10 * MB, 0, free), guard_slack=64 * KB)
        self.assertEqual(n, free + 10 * KB)
        self.assertClean()

    def test_compressed_directory_is_refused(self):
        op = QuotaOpener(1 * MB)
        with mock.patch.object(w, "_is_compressed", return_value=True):
            with self.assertRaises(w.RefusedError) as cm:
                w.fill_free_space(self.dir, ["zero"], 64 * KB, opener=op, disk_usage=big_free)
        self.assertIn("compress", str(cm.exception))
        self.assertEqual(op.opened, [])
        self.assertClean()

    def test_stale_directories_warned_about_but_kept(self):
        stale = os.path.join(self.dir, "~wipefree.deadbeef")
        os.mkdir(stale)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            w.fill_free_space(self.dir, ["zero"], 16 * KB, opener=QuotaOpener(32 * KB),
                              disk_usage=big_free)
        self.assertIn("~wipefree.deadbeef", err.getvalue())
        self.assertEqual(os.listdir(self.dir), ["~wipefree.deadbeef"])

    def test_not_a_directory(self):
        with self.assertRaises(w.RefusedError):
            w.fill_free_space(os.path.join(self.dir, "missing"), ["zero"], 4096,
                              opener=QuotaOpener(0), disk_usage=big_free)

    def test_cleanup_failure_prints_path_for_manual_removal(self):
        err = io.StringIO()
        with mock.patch("os.remove", side_effect=PermissionError("locked")), \
                contextlib.redirect_stderr(err):
            w.fill_free_space(self.dir, ["zero"], 16 * KB, opener=QuotaOpener(32 * KB),
                              disk_usage=big_free)
        left = os.listdir(self.dir)
        self.assertEqual(len(left), 1)
        self.assertIn(os.path.join(self.dir, left[0]), err.getvalue())
        self.assertIn("manually", err.getvalue())
        # (TemporaryDirectory cleanup removes the leftovers now that os.remove is unpatched.)


# --------------------------------------------------------------------------
# safety and confirmation
# --------------------------------------------------------------------------

def make_disk(**kw):
    base = dict(id="PhysicalDrive2", path=r"\\.\PhysicalDrive2", size=1000 * 512,
                sector_size=512, model="Test Disk", bus="USB",
                volumes=[VolumeInfo(letter="E:", label="DATA", fs="NTFS", size=400 * 512)])
    base.update(kw)
    return DiskInfo(**base)


class RefusalReasonTests(unittest.TestCase):
    def test_ordinary_disk_is_allowed(self):
        self.assertIsNone(w.refusal_reason(make_disk(), {"PhysicalDrive0"}))

    def test_system_flag(self):
        self.assertIn("system", w.refusal_reason(make_disk(is_system=True), set()))

    def test_id_in_system_ids(self):
        self.assertIsNotNone(w.refusal_reason(make_disk(), {"PhysicalDrive2"}))
        self.assertIsNotNone(w.refusal_reason(make_disk(id="C:"), {"C:"}))

    def test_no_media(self):
        self.assertIn("no size", w.refusal_reason(make_disk(size=0), set()))

    def test_unaligned_size(self):
        self.assertIsNotNone(w.refusal_reason(make_disk(size=1000 * 512 + 1), set()))

    def test_multi_extent_volume(self):
        d = make_disk(volumes=[VolumeInfo(letter="E:", extents=1),
                               VolumeInfo(guid="\\\\?\\Volume{x}\\", extents=2)])
        self.assertIn("extents", w.refusal_reason(d, set()))

    def test_mounted_partition(self):
        d = make_disk(volumes=[VolumeInfo(letter="/dev/sdb1", label="/mnt/x", mounted=True)])
        self.assertIn("mounted", w.refusal_reason(d, set()))

    def test_disk_without_volumes_is_allowed(self):
        self.assertIsNone(w.refusal_reason(make_disk(volumes=[]), set()))


class ConfirmTests(unittest.TestCase):
    def no_input(self, prompt=""):
        raise AssertionError("input() must not be called")

    def test_matching_confirm_arg(self):
        self.assertTrue(w.confirm("PhysicalDrive2", "PhysicalDrive2", self.no_input, lambda: False))

    def test_mismatched_confirm_arg(self):
        for wrong in ("PhysicalDrive3", "physicaldrive2", "", " PhysicalDrive2"):
            with self.subTest(wrong=wrong):
                self.assertFalse(w.confirm("PhysicalDrive2", wrong, self.no_input, lambda: True))

    def test_non_tty_without_flag_is_declined_without_prompting(self):
        self.assertFalse(w.confirm("PhysicalDrive2", None, self.no_input, lambda: False))

    def test_tty_typed_correctly(self):
        prompts = []

        def fake(prompt):
            prompts.append(prompt)
            return "PhysicalDrive2"
        self.assertTrue(w.confirm("PhysicalDrive2", None, fake, lambda: True))
        self.assertIn("PhysicalDrive2", prompts[0])

    def test_tty_typed_wrong_or_eof(self):
        self.assertFalse(w.confirm("PhysicalDrive2", None, lambda p: "yes", lambda: True))
        self.assertFalse(w.confirm("PhysicalDrive2", None, lambda p: "physicaldrive2", lambda: True))

        def eof(prompt):
            raise EOFError
        self.assertFalse(w.confirm("PhysicalDrive2", None, eof, lambda: True))

    def test_no_generic_yes_flag_exists(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = w.main(["device", "PhysicalDrive2", "--yes"])
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------
# CLI with a fake platform
# --------------------------------------------------------------------------

class FakeDevice(io.BytesIO):
    """In-memory 'device' that remembers whether it was closed and what passes did to it."""


class FakePlatform:
    name = "fake"

    def __init__(self, disks=None, admin=True, system=("PhysicalDrive0",), device_size=10000):
        self.disks = disks if disks is not None else [
            make_disk(id="PhysicalDrive0", model="System SSD", is_system=True),
            make_disk(id="PhysicalDrive2", size=device_size - device_size % 512 or 512)]
        self.admin = admin
        self.system = set(system)
        self.device_size = device_size - device_size % 512 or 512
        self.open_calls = []
        self.cleaned_up = False
        self.warnings = []
        self.device = FakeDevice(b"\xaa" * self.device_size)
        self.body_hook = None

    def list_disks(self):
        return self.disks

    def system_ids(self):
        return set(self.system)

    def is_admin(self):
        return self.admin

    def resolve_target(self, target):
        if target.isdigit():
            raise ValueError("bare number")
        for d in self.disks:
            if d.id.lower() == target.lower():
                return d
        raise w.WipeError("no such disk: %s" % target)

    @contextlib.contextmanager
    def open_target(self, target):
        self.open_calls.append(target)
        try:
            yield self.device, self.device_size, self.resolve_target(target)
        finally:
            self.cleaned_up = True


def run_main(argv, platform=None, stdin_tty=False):
    out, err = io.StringIO(), io.StringIO()
    stdin = mock.Mock()
    stdin.isatty.return_value = stdin_tty
    patches = [mock.patch.object(sys, "stdin", stdin)]
    if platform is not None:
        patches.append(mock.patch.object(w, "get_platform", return_value=platform))
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        code = w.main(argv)
    return code, out.getvalue(), err.getvalue()


class CliDryRunTests(unittest.TestCase):
    def test_device_dry_run_exits_zero_and_never_opens(self):
        p = FakePlatform()
        code, out, err = run_main(["device", "PhysicalDrive2", "--dry-run"], p)
        self.assertEqual(code, 0, err)
        self.assertEqual(p.open_calls, [])
        self.assertIn("ALL DATA ON PhysicalDrive2 WILL BE DESTROYED", out)
        self.assertIn("Test Disk", out)
        self.assertIn("E:", out)
        self.assertIn("DATA", out)
        self.assertIn("NTFS", out)
        self.assertIn("USB", out)
        self.assertEqual(p.device.getvalue(), b"\xaa" * p.device_size)

    def test_dry_run_with_all_options_never_opens(self):
        p = FakePlatform()
        code, out, _ = run_main(["device", "PhysicalDrive2", "--dry-run", "--passes", "dod",
                                 "--verify", "--chunk-size", "1M", "--confirm", "PhysicalDrive2"], p)
        self.assertEqual(code, 0)
        self.assertEqual(p.open_calls, [])
        self.assertIn("zero, one, random", out)


class CliSafetyTests(unittest.TestCase):
    def test_system_disk_refused_exit_3(self):
        p = FakePlatform()
        code, _, err = run_main(["device", "PhysicalDrive0", "--dry-run"], p)
        self.assertEqual(code, 3)
        self.assertIn("system", err)
        code, _, _ = run_main(["device", "PhysicalDrive0", "--confirm", "PhysicalDrive0"], p)
        self.assertEqual(code, 3)
        self.assertEqual(p.open_calls, [])

    def test_system_volume_refused_by_system_ids_even_if_flag_unset(self):
        p = FakePlatform(disks=[make_disk(id="C:", is_system=False)], system=("C:",))
        code, _, _ = run_main(["device", "C:", "--dry-run"], p)
        self.assertEqual(code, 3)

    def test_not_admin_exit_3(self):
        p = FakePlatform(admin=False)
        code, _, err = run_main(["device", "PhysicalDrive2", "--dry-run"], p)
        self.assertEqual(code, 3)
        self.assertIn("administrator", err)
        self.assertEqual(p.open_calls, [])

    def test_safety_check_runs_before_admin_and_confirmation(self):
        p = FakePlatform(admin=False)
        code, _, err = run_main(["device", "PhysicalDrive0", "--dry-run"], p)
        self.assertEqual(code, 3)
        self.assertIn("system", err)

    def test_bare_number_is_usage_error(self):
        code, _, err = run_main(["device", "2", "--dry-run"], FakePlatform())
        self.assertEqual(code, 2)
        self.assertIn("bare", err)

    def test_unknown_disk_is_exit_1(self):
        code, _, _ = run_main(["device", "PhysicalDrive9", "--dry-run"], FakePlatform())
        self.assertEqual(code, 1)

    def test_unsupported_platform_exits_3(self):
        code, _, err = run_main(["device", "PhysicalDrive2", "--dry-run"], w.UnsupportedPlatform())
        self.assertEqual(code, 3)
        self.assertIn("unsupported platform", err)

    def test_no_confirmation_non_tty_exits_4_and_does_not_open(self):
        p = FakePlatform()
        code, _, err = run_main(["device", "PhysicalDrive2"], p, stdin_tty=False)
        self.assertEqual(code, 4)
        self.assertEqual(p.open_calls, [])
        self.assertIn("confirm", err)

    def test_mismatched_confirm_exits_4(self):
        p = FakePlatform()
        code, _, _ = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive1"], p)
        self.assertEqual(code, 4)
        self.assertEqual(p.open_calls, [])

    def test_interactive_wrong_text_exits_4(self):
        p = FakePlatform()
        with mock.patch("builtins.input", return_value="yes"):
            code, _, _ = run_main(["device", "PhysicalDrive2"], p, stdin_tty=True)
        self.assertEqual(code, 4)
        self.assertEqual(p.open_calls, [])

    def test_interactive_correct_text_wipes(self):
        p = FakePlatform()
        with mock.patch("builtins.input", return_value="PhysicalDrive2"):
            code, _, _ = run_main(["device", "PhysicalDrive2"], p, stdin_tty=True)
        self.assertEqual(code, 0)
        self.assertEqual(p.device.getvalue(), b"\x00" * p.device_size)


class CliWipeTests(unittest.TestCase):
    def test_confirmed_wipe_default_zero_pass(self):
        p = FakePlatform()
        code, out, _ = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive2"], p)
        self.assertEqual(code, 0)
        self.assertEqual(p.open_calls, ["PhysicalDrive2"])
        self.assertEqual(p.device.getvalue(), b"\x00" * p.device_size)
        self.assertTrue(p.cleaned_up)

    def test_multipass_verify(self):
        p = FakePlatform()
        code, out, _ = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive2",
                                 "--passes", "zero,one", "--verify", "--chunk-size", "4096"], p)
        self.assertEqual(code, 0)
        self.assertEqual(p.device.getvalue(), b"\xff" * p.device_size)
        self.assertIn("verified", out)

    def test_verify_mismatch_exits_5_and_cleans_up(self):
        p = FakePlatform()

        class Bad(FakeDevice):
            def readinto(self, b):
                n = super().readinto(b)
                if n:
                    b[0] ^= 1
                return n
        p.device = Bad(b"\xaa" * p.device_size)
        code, _, err = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive2",
                                 "--verify"], p)
        self.assertEqual(code, 5)
        self.assertIn("VERIFY FAILED", err)
        self.assertTrue(p.cleaned_up)

    def test_ctrl_c_during_wipe_exits_130_and_runs_cleanup(self):
        p = FakePlatform()

        class Interrupting(FakeDevice):
            def write(self, data):
                raise KeyboardInterrupt
        p.device = Interrupting(b"\xaa" * p.device_size)
        code, _, err = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive2"], p)
        self.assertEqual(code, 130)
        self.assertTrue(p.cleaned_up)                 # context manager's finally ran
        self.assertIn("interrupted", err)

    def test_ctrl_c_at_confirmation_prompt_exits_130_without_opening(self):
        p = FakePlatform()
        with mock.patch("builtins.input", side_effect=KeyboardInterrupt):
            code, _, _ = run_main(["device", "PhysicalDrive2"], p, stdin_tty=True)
        self.assertEqual(code, 130)
        self.assertEqual(p.open_calls, [])

    def test_open_failure_exit_1_for_wipe_error(self):
        p = FakePlatform()

        @contextlib.contextmanager
        def failing(target):
            raise w.WipeError("volume in use, close Explorer/AV handles")
            yield
        p.open_target = failing
        code, _, err = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive2"], p)
        self.assertEqual(code, 1)
        self.assertIn("volume in use", err)

    def test_io_error_during_wipe_exit_1(self):
        p = FakePlatform()

        class Failing(FakeDevice):
            def write(self, data):
                raise OSError(errno.EIO, "I/O error")
        p.device = Failing()
        code, _, _ = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive2"], p)
        self.assertEqual(code, 1)
        self.assertTrue(p.cleaned_up)

    def test_device_changed_between_resolve_and_open_is_refused(self):
        p = FakePlatform()

        @contextlib.contextmanager
        def swapped(target):
            yield p.device, p.device_size, make_disk(id="PhysicalDrive7")
        p.open_target = swapped
        code, _, _ = run_main(["device", "PhysicalDrive2", "--confirm", "PhysicalDrive2"], p)
        self.assertEqual(code, 3)
        self.assertEqual(p.device.getvalue(), b"\xaa" * p.device_size)    # nothing written


class CliArgumentTests(unittest.TestCase):
    def test_usage_errors_exit_2(self):
        for argv in ([], ["nope"], ["freespace"], ["device"], ["device", "X:", "--passes", "bad"],
                     ["device", "X:", "--chunk-size", "1000"], ["device", "X:", "--chunk-size", "0"],
                     ["device", "X:", "--chunk-size", "junk"], ["freespace", ".", "--verify"],
                     ["freespace", ".", "--confirm", "x"]):
            with self.subTest(argv=argv):
                code, _, _ = run_main(argv)
                self.assertEqual(code, 2)

    def test_help_exits_zero(self):
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("freespace", out)

    def test_defaults(self):
        args = w.build_parser().parse_args(["device", "E:"])
        self.assertEqual(args.passes, ["zero"])
        self.assertEqual(args.chunk_size, 4 * MB)
        self.assertFalse(args.verify)
        self.assertFalse(args.dry_run)
        self.assertIsNone(args.confirm)

    def test_list_command(self):
        p = FakePlatform()
        code, out, _ = run_main(["list"], p)
        self.assertEqual(code, 0)
        self.assertIn("PhysicalDrive0", out)
        self.assertEqual(out.count("SYSTEM DISK"), 1)

    def test_list_tolerates_odd_disks(self):
        weird = DiskInfo(id="PhysicalDrive5", path="x", size=0, model="(unknown model)", bus="",
                         note="no media")
        code, out, _ = run_main(["list"], FakePlatform(disks=[weird]))
        self.assertEqual(code, 0)
        self.assertIn("no media", out)


class CliFreespaceTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(w, "fill_free_space") as fill:
            code, out, _ = run_main(["freespace", d, "--dry-run"])
            self.assertEqual(code, 0)
            fill.assert_not_called()
            self.assertEqual(os.listdir(d), [])
            self.assertIn("dry run", out)

    def test_not_a_directory_exit_3(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_main(["freespace", os.path.join(d, "nope")])
        self.assertEqual(code, 3)
        self.assertIn("not a directory", err)

    def test_arguments_are_passed_through_without_touching_a_real_volume(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(w, "fill_free_space", return_value=12345) as fill:
            code, out, _ = run_main(["freespace", d, "--passes", "dod", "--chunk-size", "1M"])
        self.assertEqual(code, 0)
        args, kwargs = fill.call_args
        self.assertEqual(args[0], d)
        self.assertEqual(args[1], ["zero", "one", "random"])
        self.assertEqual(args[2], 1 * MB)
        self.assertIn("12.1 KiB", out)

    def test_keyboard_interrupt_exits_130(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(w, "fill_free_space", side_effect=KeyboardInterrupt):
            code, _, _ = run_main(["freespace", d])
        self.assertEqual(code, 130)

    def test_compressed_refusal_exits_3(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(w, "fill_free_space", side_effect=w.RefusedError("compressed")):
            code, _, err = run_main(["freespace", d])
        self.assertEqual(code, 3)

    def test_guard_failure_exits_1(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(w, "fill_free_space", side_effect=w.WipeError("guard")):
            code, _, _ = run_main(["freespace", d])
        self.assertEqual(code, 1)


# --------------------------------------------------------------------------
# Windows structures and target parsing (pure; run everywhere)
# --------------------------------------------------------------------------

class WindowsParsingTests(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(w.parse_windows_target("PhysicalDrive2"), ("disk", 2))
        self.assertEqual(w.parse_windows_target(r"\\.\PhysicalDrive12"), ("disk", 12))
        self.assertEqual(w.parse_windows_target("physicaldrive0"), ("disk", 0))
        self.assertEqual(w.parse_windows_target("E:"), ("volume", "E:"))
        self.assertEqual(w.parse_windows_target("e:"), ("volume", "E:"))
        self.assertEqual(w.parse_windows_target("E:\\"), ("volume", "E:"))

    def test_bad_targets(self):
        for bad in ("2", "02", "", "/dev/sdb", "EE:", "C:\\Users", "PhysicalDrive", "PhysicalDrive-1",
                    r"\\.\E:", "PhysicalDrive2x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                w.parse_windows_target(bad)

    def test_geometry_ex_layout(self):
        # DISK_GEOMETRY: Cylinders 8, MediaType 4, Tracks 4, Sectors 4, BytesPerSector 4; DiskSize 8
        buf = struct.pack("<qIIII", 1234, 12, 255, 63, 4096) + struct.pack("<q", 4000787030016)
        self.assertEqual(w.parse_geometry_ex(buf + b"\0" * 40), (4000787030016, 4096))

    def test_storage_descriptor_layout(self):
        header_len = 36
        strings = b"SAMSUNG \x00" + b"SSD 980 PRO 1TB  \x00" + b"REV1\x00"
        v_off = header_len
        p_off = v_off + len(b"SAMSUNG \x00")
        r_off = p_off + len(b"SSD 980 PRO 1TB  \x00")
        head = struct.pack("<IIBBBBIIIIII", 1, header_len + len(strings), 0, 0, 0, 0,
                           v_off, p_off, r_off, 0, 17, 0)
        self.assertEqual(len(head), header_len)
        self.assertEqual(w.parse_storage_descriptor(head + strings),
                         ("SAMSUNG", "SSD 980 PRO 1TB", "NVMe"))

    def test_storage_descriptor_missing_strings_and_short_buffer(self):
        head = struct.pack("<IIBBBBIIIIII", 1, 36, 0, 0, 0, 0, 0, 0, 0, 0, 7, 0)
        self.assertEqual(w.parse_storage_descriptor(head), ("", "", "USB"))
        self.assertEqual(w.parse_storage_descriptor(b"\0" * 10), ("", "", ""))

    def test_storage_descriptor_garbage_offsets_do_not_crash(self):
        head = struct.pack("<IIBBBBIIIIII", 1, 36, 0, 0, 0, 0, 9999, 0xFFFFFFFF, 0, 0, 99, 0)
        self.assertEqual(w.parse_storage_descriptor(head), ("", "", "Bus99"))

    def test_volume_extents_layout(self):
        buf = struct.pack("<I4x", 2)
        buf += struct.pack("<I4xqq", 3, 1048576, 100 * MB)
        buf += struct.pack("<I4xqq", 5, 0, 200 * MB)
        self.assertEqual(w.parse_volume_extents(buf),
                         [(3, 1048576, 100 * MB), (5, 0, 200 * MB)])

    def test_volume_extents_truncated(self):
        buf = struct.pack("<I4x", 3) + struct.pack("<I4xqq", 1, 0, 10)
        self.assertEqual(w.parse_volume_extents(buf), [(1, 0, 10)])
        self.assertEqual(w.parse_volume_extents(b""), [])

    def test_length_info(self):
        self.assertEqual(w.parse_length_info(struct.pack("<q", 123456789)), 123456789)


# --------------------------------------------------------------------------
# Windows-only
# --------------------------------------------------------------------------

@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WindowsSmokeTests(unittest.TestCase):
    def test_list_disks_flags_a_system_disk(self):
        """Read-only: opens \\\\.\\PhysicalDriveN and volumes with access 0, no admin needed."""
        disks = w.WinPlatform().list_disks()
        self.assertTrue(disks, "no physical disks found")
        system = [d for d in disks if d.is_system]
        self.assertGreaterEqual(len(system), 1)
        windir = os.environ.get("SystemDrive", "C:").upper()
        self.assertTrue(any(v.letter == windir for d in system for v in d.volumes),
                        "system disk should contain the Windows drive %s" % windir)
        for d in disks:
            self.assertRegex(d.id, r"^PhysicalDrive\d+$")
            self.assertGreaterEqual(d.size, 0)
            self.assertIn(d.sector_size, (512, 4096))

    def test_system_ids_contain_this_python_and_cwd(self):
        ids = w.WinPlatform().system_ids()
        drive = os.path.splitdrive(os.path.abspath(sys.executable))[0].upper()
        self.assertIn(drive, ids)
        self.assertTrue(any(i.startswith("PhysicalDrive") for i in ids))

    def test_resolve_system_volume_is_refused_by_the_rule(self):
        p = w.WinPlatform()
        drive = os.path.splitdrive(os.path.abspath(sys.executable))[0].upper()
        info = p.resolve_target(drive)
        self.assertIsNotNone(w.refusal_reason(info, p.system_ids()))

    def test_resolve_target_rejects_bare_number(self):
        with self.assertRaises(ValueError):
            w.WinPlatform().resolve_target("3")


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WinRawFileScratchFileTests(unittest.TestCase):
    """Exercises WinRawFile (mmap bounce buffer, NO_BUFFERING WriteFile/ReadFile/SetFilePointerEx)
    against a scratch FILE in a temp directory.  Never touches a device."""

    SIZE = 3 * MB + 8 * 512

    def open_scratch(self, path):
        with open(path, "wb") as f:
            f.truncate(self.SIZE)
        h = w._create_file(path, w.GENERIC_READ | w.GENERIC_WRITE,
                           w.FILE_SHARE_READ | w.FILE_SHARE_WRITE,
                           w.FILE_FLAG_NO_BUFFERING | w.FILE_FLAG_WRITE_THROUGH)
        return h

    def test_wipe_and_verify_through_bounce_buffer(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "scratch.bin")
            h = self.open_scratch(path)
            raw = w.WinRawFile(h, self.SIZE, 512, bufsize=1 * MB, owns_handle=True)
            try:
                w.wipe_stream(raw, self.SIZE, ["one", "random"], 1 * MB + 4096, verify=True,
                              progress_factory=quiet_progress)
                raw.seek(0)
                head = bytearray(4096)
                self.assertEqual(raw.readinto(head), 4096)
            finally:
                raw.close()
            with open(path, "rb") as f:
                data = f.read()
            self.assertEqual(len(data), self.SIZE)
            self.assertNotEqual(data, b"\xff" * self.SIZE)
            self.assertNotEqual(data[:4096], b"\x00" * 4096)

    def test_tail_chunk_is_aligned_and_written(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "scratch.bin")
            h = self.open_scratch(path)
            raw = w.WinRawFile(h, self.SIZE, 512, bufsize=1 * MB, owns_handle=True)
            try:
                w.wipe_stream(raw, self.SIZE, ["one"], 4 * MB, verify=True)
            finally:
                raw.close()
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"\xff" * self.SIZE)

    def test_unaligned_io_is_rejected_before_reaching_windows(self):
        with tempfile.TemporaryDirectory() as d:
            h = self.open_scratch(os.path.join(d, "scratch.bin"))
            raw = w.WinRawFile(h, self.SIZE, 512, bufsize=1 * MB, owns_handle=True)
            try:
                with self.assertRaises(ValueError):
                    raw.write(b"x" * 100)
                with self.assertRaises(ValueError):
                    raw.seek(100)
                with self.assertRaises(ValueError):
                    raw.seek(self.SIZE)
                    raw.write(b"\0" * 512)             # past the end
            finally:
                raw.close()

    def test_constructor_validates_alignment(self):
        with self.assertRaises(ValueError):
            w.WinRawFile(0, 1000, 512)
        with self.assertRaises(ValueError):
            w.WinRawFile(0, 4096, 512, bufsize=1000)

    def test_close_is_idempotent_and_releases_handle(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "scratch.bin")
            h = self.open_scratch(path)
            raw = w.WinRawFile(h, self.SIZE, 512, bufsize=1 * MB, owns_handle=True)
            raw.close()
            raw.close()
            os.remove(path)                            # would fail if the handle were still open


# --------------------------------------------------------------------------
# Windows open/lock/cleanup logic with the Win32 calls replaced by fakes
# --------------------------------------------------------------------------

class FakeWin32:
    """Records the Win32 calls made by WinPlatform.open_target.  No real API is called."""

    def __init__(self, lock_fails_on=None):
        self.next = 100
        self.events = []
        self.paths = {}
        self.lock_fails_on = lock_fails_on
        self.open_handles = set()

    def create_file(self, path, access, share, flags):
        self.next += 1
        self.paths[self.next] = path
        self.open_handles.add(self.next)
        self.events.append(("open", path, access, flags))
        return self.next

    def close(self, h):
        self.open_handles.discard(h)
        self.events.append(("close", self.paths[h]))

    def ioctl(self, h, code, inbuf=b"", outsize=0, grow=False):
        path = self.paths[h]
        self.events.append(("ioctl", hex(code), path))
        if code == w.FSCTL_LOCK_VOLUME and self.lock_fails_on and self.lock_fails_on in path:
            raise OSError(5, "Access is denied")
        if code == w.IOCTL_DISK_GET_LENGTH_INFO:
            return struct.pack("<q", 1000 * 512)
        return b""

    def kinds(self, name):
        return [e for e in self.events if e[0] == "ioctl" and e[1] == hex(name)]


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WinOpenTargetLogicTests(unittest.TestCase):
    """Handle-lifetime and ordering of WinPlatform.open_target (UNTESTED against hardware;
    these tests only prove the cleanup structure with fakes)."""

    GUID_A = "\\\\?\\Volume{aaaa}\\"
    GUID_B = "\\\\?\\Volume{bbbb}\\"

    def make(self, fake):
        p = w.WinPlatform()
        disk = make_disk()
        vols = [(VolumeInfo(letter="E:", guid=self.GUID_A), [2]),
                (VolumeInfo(guid=self.GUID_B), [2]),                 # letterless, same disk
                (VolumeInfo(letter="F:", guid="\\\\?\\Volume{cccc}\\"), [4])]
        stack = contextlib.ExitStack()
        for target, repl in (
                (p, ("resolve_target", lambda t: disk)),
                (p, ("system_ids", lambda: set())),
                (p, ("is_admin", lambda: True)),
                (p, ("_volumes", lambda: vols))):
            stack.enter_context(mock.patch.object(target, repl[0], repl[1]))
        stack.enter_context(mock.patch.object(w, "_create_file", fake.create_file))
        stack.enter_context(mock.patch.object(w, "_close_handle", fake.close))
        stack.enter_context(mock.patch.object(w, "_ioctl", fake.ioctl))
        stack.enter_context(mock.patch.object(w.time, "sleep", lambda s: None))
        self.addCleanup(stack.close)
        return p

    def test_disk_open_locks_every_volume_on_that_disk_only(self):
        fake = FakeWin32()
        p = self.make(fake)
        with p.open_target("PhysicalDrive2") as (raw, size, info):
            self.assertEqual(size, 1000 * 512)
            locked = [e[2] for e in fake.kinds(w.FSCTL_LOCK_VOLUME)]
            self.assertEqual(sorted(locked), sorted([self.GUID_A.rstrip("\\"),
                                                     self.GUID_B.rstrip("\\")]))
            self.assertEqual(len(fake.kinds(w.FSCTL_DISMOUNT_VOLUME)), 2)
            self.assertEqual(len(fake.open_handles), 3)                  # 2 volumes + the disk
            # the disk is opened (read/write, NO_BUFFERING|WRITE_THROUGH) after every dismount
            disk_open = fake.events.index(("open", "\\\\.\\PhysicalDrive2",
                                           w.GENERIC_READ | w.GENERIC_WRITE,
                                           w.FILE_FLAG_NO_BUFFERING | w.FILE_FLAG_WRITE_THROUGH))
            last_dismount = max(i for i, e in enumerate(fake.events)
                                if e[:2] == ("ioctl", hex(w.FSCTL_DISMOUNT_VOLUME)))
            self.assertLess(last_dismount, disk_open)
        self.assertEqual(fake.open_handles, set())                       # all closed

    def test_teardown_order_update_properties_then_disk_then_volumes(self):
        fake = FakeWin32()
        p = self.make(fake)
        with p.open_target("PhysicalDrive2"):
            pass
        names = [(e[0], e[1] if e[0] == "ioctl" else e[1]) for e in fake.events]
        upd = names.index(("ioctl", hex(w.IOCTL_DISK_UPDATE_PROPERTIES)))
        disk_close = names.index(("close", "\\\\.\\PhysicalDrive2"))
        vol_closes = [i for i, e in enumerate(names)
                      if e[0] == "close" and "Volume{" in e[1]]
        self.assertLess(upd, disk_close)
        self.assertTrue(all(disk_close < i for i in vol_closes))
        self.assertEqual(len(vol_closes), 2)

    def test_keyboard_interrupt_in_body_still_unlocks_and_closes(self):
        fake = FakeWin32()
        p = self.make(fake)
        with self.assertRaises(KeyboardInterrupt):
            with p.open_target("PhysicalDrive2"):
                raise KeyboardInterrupt
        self.assertEqual(fake.open_handles, set())
        self.assertEqual(len(fake.kinds(w.IOCTL_DISK_UPDATE_PROPERTIES)), 1)

    def test_lock_failure_aborts_without_dismounting_that_volume(self):
        fake = FakeWin32(lock_fails_on="bbbb")
        p = self.make(fake)
        with self.assertRaises(w.WipeError) as cm:
            with p.open_target("PhysicalDrive2"):
                self.fail("must not reach the body")
        self.assertIn("volume in use, close Explorer/AV handles", str(cm.exception))
        dismounted = [e[2] for e in fake.kinds(w.FSCTL_DISMOUNT_VOLUME)]
        self.assertNotIn(self.GUID_B.rstrip("\\"), dismounted)           # never dismount unlocked
        self.assertEqual(fake.open_handles, set())                       # earlier handles closed
        self.assertEqual(len(fake.kinds(w.FSCTL_LOCK_VOLUME)), 1 + w.LOCK_ATTEMPTS)
        self.assertFalse(any(e[0] == "open" and "PhysicalDrive2" in e[1] for e in fake.events))

    def test_volume_target_locks_dismounts_and_enables_extended_io(self):
        fake = FakeWin32()
        p = self.make(fake)
        info = make_disk(id="E:", path="\\\\.\\E:")
        with mock.patch.object(p, "resolve_target", lambda t: info):
            with p.open_target("E:") as (raw, size, got):
                self.assertEqual(size, 1000 * 512)
                self.assertEqual(len(fake.kinds(w.FSCTL_LOCK_VOLUME)), 1)
                self.assertEqual(len(fake.kinds(w.FSCTL_DISMOUNT_VOLUME)), 1)
                self.assertEqual(len(fake.kinds(w.FSCTL_ALLOW_EXTENDED_DASD_IO)), 1)
        self.assertEqual(fake.open_handles, set())
        self.assertEqual(fake.kinds(w.IOCTL_DISK_UPDATE_PROPERTIES), [])  # disks only

    def test_refusal_and_admin_checks_happen_before_any_handle_is_opened(self):
        fake = FakeWin32()
        p = self.make(fake)
        with mock.patch.object(p, "system_ids", lambda: {"PhysicalDrive2"}):
            with self.assertRaises(w.RefusedError):
                with p.open_target("PhysicalDrive2"):
                    pass
        with mock.patch.object(p, "is_admin", lambda: False):
            with self.assertRaises(w.RefusedError):
                with p.open_target("PhysicalDrive2"):
                    pass
        self.assertEqual(fake.events, [])


if __name__ == "__main__":
    unittest.main()
