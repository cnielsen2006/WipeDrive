#!/usr/bin/env python3
"""wipedrive - overwrite a whole drive, or the free space of a filesystem.

Subcommands
    list                         show physical disks (no admin needed)
    freespace PATH [options]     fill the free space under PATH, then delete the fill files
    device TARGET  [options]     overwrite an entire physical drive or volume (DESTRUCTIVE)

Standard library only.  See README.md for the safety model and limitations.

Exit codes
    0 ok   1 I/O or other error   2 usage   3 refused by a safety check
    4 confirmation declined or missing   5 verify mismatch   130 Ctrl+C
"""

import argparse
import contextlib
import dataclasses
import errno
import io
import mmap
import os
import random
import re
import secrets
import shutil
import struct
import sys
import time
from typing import Callable, List, Optional

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_NO_CONFIRM = 4
EXIT_VERIFY = 5
EXIT_INTERRUPT = 130

KIB = 1024
MIB = 1024 * KIB
GIB = 1024 * MIB

DEFAULT_CHUNK = 4 * MIB
MIN_CHUNK = 4096                    # smallest write when squeezing the tail of a full disk
CHUNK_ALIGN = 4096                  # chunk sizes must be a multiple of this
DEFAULT_MAX_FILE_SIZE = 1 * GIB     # fits under the FAT32 4 GiB limit
GUARD_SLACK = 64 * MIB              # compression/dedup guard: free space + 1% + this
STALE_PREFIX = "~wipefree."
FILE_ATTRIBUTE_COMPRESSED = 0x800

PRESETS = {"dod": ["zero", "one", "random"]}
PATTERNS = ("zero", "one", "random")


# --------------------------------------------------------------------------
# exceptions
# --------------------------------------------------------------------------

class WipeError(Exception):
    """General failure (exit 1)."""


class RefusedError(WipeError):
    """A safety check refused to continue (exit 3)."""


class VerifyError(WipeError):
    """Read-back did not match what was written (exit 5)."""

    def __init__(self, offset: int, message: Optional[str] = None):
        self.offset = offset
        super().__init__(message or "verify mismatch at byte offset %d" % offset)


# --------------------------------------------------------------------------
# pure core: parsing and formatting
# --------------------------------------------------------------------------

_SIZE_RE = re.compile(r"^(\d+)\s*(?:([KMGT])(?:I?B)?|B)?$", re.IGNORECASE)
_SIZE_SHIFT = {"": 0, "K": 10, "M": 20, "G": 30, "T": 40}


def parse_size(s: str) -> int:
    """Parse '4M', '512K', '1G', '4096', '4MiB' (binary units) into bytes."""
    m = _SIZE_RE.match(str(s).strip())
    if not m:
        raise ValueError("invalid size %r (examples: 4096, 512K, 4M, 1G)" % (s,))
    return int(m.group(1)) << _SIZE_SHIFT[(m.group(2) or "").upper()]


def format_size(n: int) -> str:
    """Human-readable binary size: '512 B', '1.5 KiB', '3.7 TiB'."""
    if n < 0:
        return "-" + format_size(-n)
    if n < KIB:
        return "%d B" % n
    value = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024.0
        if value < 1024.0 or unit == "PiB":
            return "%.1f %s" % (value, unit)
    raise AssertionError("unreachable")


def parse_passes(spec: str) -> List[str]:
    """Expand a comma list of zero/one/random (and the 'dod' preset) into pattern names."""
    if spec is None or not str(spec).strip():
        raise ValueError("empty pass specification")
    out: List[str] = []
    for part in str(spec).split(","):
        name = part.strip().lower()
        if name in PRESETS:
            out.extend(PRESETS[name])
        elif name in PATTERNS:
            out.append(name)
        elif not name:
            raise ValueError("empty entry in pass specification %r" % (spec,))
        else:
            raise ValueError(
                "unknown pass %r (valid: zero, one, random, dod)" % (part.strip(),))
    return out


def pattern_source(name: str, seed: int = 0) -> Callable[[int], bytes]:
    """Return f(n) -> n bytes of the named pattern.

    zero and one slice a cached buffer.  random uses random.Random(seed).randbytes, so the
    same seed and the same sequence of request sizes reproduce the same bytes (randbytes(5)
    followed by randbytes(3) is NOT randbytes(8); callers must request identical sizes).
    """
    if name == "random":
        return random.Random(seed).randbytes
    if name not in ("zero", "one"):
        raise ValueError("unknown pattern %r" % (name,))
    fill = b"\x00" if name == "zero" else b"\xff"
    cache = [fill * MIB]

    def source(n: int) -> bytes:
        if n > len(cache[0]):
            cache[0] = fill * n
        buf = cache[0]
        return buf if n == len(buf) else buf[:n]

    return source


# --------------------------------------------------------------------------
# pure core: progress
# --------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


format_duration = _fmt_duration          # public name for front ends


def progress_stats(done: int, total: int, elapsed: float):
    """(percent, bytes_per_second, eta_seconds or None) -- shared by Progress and the GUI."""
    rate = done / elapsed if elapsed > 0 else 0.0
    if total:
        pct = min(100.0, 100.0 * done / total)
        remaining = max(0, total - done)
        eta = remaining / rate if rate > 0 else None
    else:
        pct, eta = 100.0, 0.0
    return pct, rate, eta


class Progress:
    """A one-line '\\r' progress display: percent, MiB/s and ETA, redrawn at most every 0.5 s."""

    INTERVAL = 0.5

    def __init__(self, total, label, stream=None, clock=time.monotonic):
        self.total = total
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.clock = clock
        self.done = 0
        self._start = clock()
        self._last = self._start
        self._width = 0
        self._finished = False

    def update(self, n: int) -> None:
        self.done += n
        now = self.clock()
        if now - self._last >= self.INTERVAL:
            self._last = now
            self._draw(now)

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._draw(self.clock())
        self.stream.write("\n")
        self._flush()

    def _draw(self, now: float) -> None:
        pct, rate, eta_s = progress_stats(self.done, self.total, now - self._start)
        eta = "--:--:--" if eta_s is None else _fmt_duration(eta_s)
        line = "%s: %5.1f%%  %s  %.1f MiB/s  ETA %s" % (
            self.label, pct, format_size(self.done), rate / MIB, eta)
        self._width = max(self._width, len(line))
        self.stream.write("\r" + line.ljust(self._width))
        self._flush()

    def _flush(self) -> None:
        try:
            self.stream.flush()
        except (AttributeError, ValueError):
            pass


def _progress_factory(total, label):
    return Progress(total, label)


# --------------------------------------------------------------------------
# pure core: write / verify
# --------------------------------------------------------------------------

def _write_all(f, data) -> int:
    """Write every byte of data, looping over short writes."""
    mv = memoryview(data)
    total = len(mv)
    done = 0
    while done < total:
        w = f.write(mv[done:])
        if w is None:
            raise WipeError("write returned None (non-blocking stream?)")
        if w <= 0:
            raise WipeError("write made no progress at offset %d" % done)
        done += w
    return total


def write_pass(f, total: int, source, chunk: int, progress=None) -> int:
    """Seek to 0 and write `total` bytes from `source`, in `chunk`-sized requests."""
    if chunk <= 0:
        raise ValueError("chunk must be positive")
    f.seek(0)
    offset = 0
    while offset < total:
        n = min(chunk, total - offset)
        _write_all(f, source(n))
        offset += n
        if progress is not None:
            progress.update(n)
    return offset


def _read_exact(f, buf: bytearray, n: int) -> int:
    """Fill buf[:n] from f, looping over short reads.  Returns the bytes actually read."""
    view = memoryview(buf)[:n]
    got = 0
    while got < n:
        r = f.readinto(view[got:])
        if not r:
            break
        got += r
    return got


def _first_difference(a, b) -> int:
    n = min(len(a), len(b))
    step = 4096
    for start in range(0, n, step):
        if a[start:start + step] != b[start:start + step]:
            for i in range(start, min(start + step, n)):
                if a[i] != b[i]:
                    return i
    return n


def verify_pass(f, total: int, source, chunk: int, progress=None) -> None:
    """Read back `total` bytes and compare with `source`; raise VerifyError(offset) on mismatch.

    `source` must be a fresh generator for the pass and is asked for exactly the same chunk
    sizes as write_pass asked for.
    """
    if chunk <= 0:
        raise ValueError("chunk must be positive")
    f.seek(0)
    buf = bytearray(min(chunk, max(total, 1)))
    offset = 0
    while offset < total:
        n = min(chunk, total - offset)
        expected = source(n)
        got = _read_exact(f, buf, n)
        actual = bytes(memoryview(buf)[:got])
        if got < n:
            diff = _first_difference(actual, expected[:got])
            if diff < got:
                raise VerifyError(offset + diff)
            raise VerifyError(offset + got, "short read: device ended at offset %d, expected "
                              "%d more bytes" % (offset + got, n - got))
        if actual != expected:
            raise VerifyError(offset + _first_difference(actual, expected))
        offset += n
        if progress is not None:
            progress.update(n)


def _sync(f) -> None:
    """Flush Python buffers and ask the OS to push data to the device, where supported."""
    f.flush()
    try:
        fd = f.fileno()
    except (AttributeError, OSError, ValueError):   # BytesIO, WinRawFile: nothing more to do
        return
    os.fsync(fd)


def _drop_cache(f) -> None:
    """Best effort: discard cached pages so a read-back comes from the device (Linux)."""
    advise = getattr(os, "posix_fadvise", None)
    if advise is None:
        return
    try:
        advise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
    except (AttributeError, OSError, ValueError):
        pass


def wipe_stream(f, total: int, passes: List[str], chunk: int, verify: bool = False,
                progress_factory=None) -> None:
    """Run every pass over f.  A fresh seed is used per pass; only the last pass is verified."""
    if not passes:
        raise ValueError("no passes given")
    count = len(passes)
    for index, name in enumerate(passes, 1):
        seed = secrets.randbits(64)
        progress = (progress_factory(total, "pass %d/%d (%s)" % (index, count, name))
                    if progress_factory else None)
        try:
            write_pass(f, total, pattern_source(name, seed), chunk, progress)
        finally:
            if progress is not None:
                progress.finish()
        _sync(f)
        if verify and index == count:
            _drop_cache(f)
            vprogress = progress_factory(total, "verify (%s)" % name) if progress_factory else None
            try:
                verify_pass(f, total, pattern_source(name, seed), chunk, vprogress)
            finally:
                if vprogress is not None:
                    vprogress.finish()


# --------------------------------------------------------------------------
# pure core: disk-full detection, safety rule, confirmation
# --------------------------------------------------------------------------

def is_disk_full(exc: BaseException) -> bool:
    """True for ENOSPC, or Windows ERROR_HANDLE_DISK_FULL (39) / ERROR_DISK_FULL (112)."""
    if getattr(exc, "errno", None) == errno.ENOSPC:
        return True
    return getattr(exc, "winerror", None) in (39, 112)


@dataclasses.dataclass
class VolumeInfo:
    """One volume on a disk.  On Linux `letter` holds the partition path and `label` the mount point."""
    letter: str = ""          # 'E:' on Windows, '' if the volume has no drive letter
    label: str = ""
    fs: str = ""
    size: int = 0
    guid: str = ""            # Windows volume GUID path
    extents: int = 1          # >1: spanned/dynamic/Storage Spaces
    mounted: bool = False     # Linux: partition is in /proc/mounts


@dataclasses.dataclass
class DiskInfo:
    id: str                   # 'PhysicalDrive2', 'E:' or '/dev/sdb'
    path: str                 # path used to open it
    size: int
    sector_size: int = 512
    model: str = ""
    bus: str = ""
    volumes: List[VolumeInfo] = dataclasses.field(default_factory=list)
    is_system: bool = False
    note: str = ""


def refusal_reason(disk: DiskInfo, system_ids) -> Optional[str]:
    """The pure safety rule.  Returns why `disk` must not be wiped, or None if it may be."""
    if disk.is_system or disk.id in system_ids:
        return ("%s holds the running Windows installation, this Python, or the current "
                "directory (system disk)" % disk.id)
    if disk.size <= 0:
        return "%s reports no size (no media in the drive?)" % disk.id
    if disk.sector_size <= 0 or disk.size % disk.sector_size:
        return "%s size %d is not a multiple of its sector size %d" % (
            disk.id, disk.size, disk.sector_size)
    for v in disk.volumes:
        if v.extents > 1:
            return ("volume %s spans %d extents (spanned, dynamic or Storage Spaces volume)"
                    % (v.letter or v.guid or "?", v.extents))
        if v.mounted:
            return "%s is mounted (%s); unmount it first" % (v.letter or "?", v.label or "?")
    return None


def volume_target(disk: DiskInfo, vol: VolumeInfo, system_ids) -> DiskInfo:
    """The DiskInfo that stands for one volume (Windows 'E:') as a wipe target."""
    return DiskInfo(id=vol.letter, path="\\\\.\\" + vol.letter, size=vol.size,
                    sector_size=disk.sector_size, model=disk.model, bus=disk.bus,
                    volumes=[vol], is_system=vol.letter in system_ids)


def resolve_wipe_target(platform, target: str) -> DiskInfo:
    """Resolve `target`, apply the safety rule, then require admin.  Shared by CLI and GUI.

    ValueError (unparsable target) and WipeError (no such disk) come from resolve_target;
    RefusedError means the safety rule or the privilege check said no."""
    disk = platform.resolve_target(target)
    reason = refusal_reason(disk, platform.system_ids())
    if reason:
        raise RefusedError(reason)
    if not platform.is_admin():
        raise RefusedError("administrator/root rights are required")
    return disk


def wipe_device(platform, target: str, expected_id: str, passes: List[str], chunk: int,
                verify: bool = False, progress_factory=None) -> int:
    """Open `target` through the platform (lock/dismount included), check it is still the
    device that was resolved, run every pass.  Returns the device size in bytes."""
    with platform.open_target(target) as (f, size, info):
        if info.id != expected_id or size <= 0:
            raise RefusedError("device changed between resolve and open; aborting")
        wipe_stream(f, size, passes, chunk, verify, progress_factory)
    return size


def confirm(target_id: str, confirm_arg: Optional[str], input_fn=None, isatty=None) -> bool:
    """Typed confirmation.  --confirm must equal the target ID; otherwise a TTY must type it.

    Ctrl+C at the prompt propagates (main turns it into exit 130)."""
    if confirm_arg is not None:
        return confirm_arg == target_id
    if isatty is None:
        isatty = sys.stdin.isatty
    try:
        if not isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if input_fn is None:
        input_fn = input
    try:
        typed = input_fn("Type %s exactly to continue: " % target_id)
    except EOFError:
        return False
    return typed == target_id


# --------------------------------------------------------------------------
# free-space wipe
# --------------------------------------------------------------------------

def _is_compressed(path: str) -> bool:
    """True if `path` carries FILE_ATTRIBUTE_COMPRESSED (Windows only)."""
    if sys.platform != "win32":
        return False
    attrs = _k32.GetFileAttributesW(path)
    return attrs != 0xFFFFFFFF and bool(attrs & FILE_ATTRIBUTE_COMPRESSED)


def _warn_stale(directory: str) -> None:
    try:
        stale = [e.name for e in os.scandir(directory)
                 if e.name.startswith(STALE_PREFIX) and e.is_dir(follow_symlinks=False)]
    except OSError:
        return
    for name in sorted(stale):
        print("warning: stale directory %s left by an earlier run; not deleting it "
              "(remove it by hand if it is yours)" % os.path.join(directory, name),
              file=sys.stderr)


def _cleanup_dir(tmp: str) -> None:
    failed = []
    try:
        names = os.listdir(tmp)
    except OSError as exc:
        print("warning: could not list %s (%s); remove it manually" % (tmp, exc), file=sys.stderr)
        return
    for name in names:
        path = os.path.join(tmp, name)
        try:
            os.remove(path)
        except OSError as exc:
            failed.append((path, exc))
    try:
        os.rmdir(tmp)
    except OSError as exc:
        failed.append((tmp, exc))
    for path, exc in failed:
        print("warning: could not remove %s (%s); delete it manually to reclaim the space"
              % (path, exc), file=sys.stderr)


def fill_free_space(directory, passes, chunk=DEFAULT_CHUNK, max_file_size=DEFAULT_MAX_FILE_SIZE,
                    opener=open, disk_usage=shutil.disk_usage, progress_factory=None,
                    guard_slack=GUARD_SLACK) -> int:
    """Fill the free space under `directory` with fill files, then delete them.

    Returns the number of bytes written in the first pass.  Later passes rewrite the same
    files in place.  The fill files live in PATH/~wipefree.<8 hex>/ and are removed in a
    finally block, so error and KeyboardInterrupt clean up too.
    """
    directory = os.fspath(directory)
    if not os.path.isdir(directory):
        raise RefusedError("%s is not a directory" % directory)
    if chunk <= 0 or max_file_size <= 0:
        raise ValueError("chunk and max_file_size must be positive")
    if not passes:
        raise ValueError("no passes given")

    start_free = disk_usage(directory).free
    limit = start_free + start_free // 100 + guard_slack
    _warn_stale(directory)

    tmp = os.path.join(directory, STALE_PREFIX + secrets.token_hex(4))
    os.mkdir(tmp)
    files: List[List] = []          # [path, size]
    try:
        if _is_compressed(tmp):
            raise RefusedError(
                "%s is NTFS-compressed: zeros would compress and the volume would never fill; "
                "uncompress the directory/volume first" % directory)
        written = _fill_first_pass(tmp, files, passes, chunk, max_file_size, opener,
                                   limit, start_free, progress_factory)
        for index in range(1, len(passes)):
            _rewrite_pass(files, passes, index, chunk, opener, progress_factory, written)
        return written
    finally:
        _cleanup_dir(tmp)


def _fill_first_pass(tmp, files, passes, chunk, max_file_size, opener, limit, start_free,
                     progress_factory) -> int:
    count = len(passes)
    source = pattern_source(passes[0], secrets.randbits(64))
    progress = (progress_factory(start_free, "freespace pass 1/%d (%s)" % (count, passes[0]))
                if progress_factory else None)
    total = 0
    current = chunk
    full = False
    index = 0
    try:
        while not full:
            path = os.path.join(tmp, "fill_%05d.bin" % index)
            index += 1
            try:
                f = opener(path, "wb", buffering=0)
            except OSError as exc:
                if is_disk_full(exc):
                    break
                raise
            entry = [path, 0]
            files.append(entry)
            try:
                size = 0
                while size < max_file_size:
                    n = min(current, max_file_size - size)
                    try:
                        w = f.write(source(n))
                    except OSError as exc:
                        if not is_disk_full(exc):
                            raise
                        w = 0
                    if w is None:
                        raise WipeError("write returned None (non-blocking stream?)")
                    if w == 0:
                        if current > MIN_CHUNK:        # squeeze the tail of the disk
                            current = max(MIN_CHUNK, current // 2)
                            continue
                        full = True
                        break
                    size += w
                    total += w
                    entry[1] = size
                    if progress is not None:
                        progress.update(w)
                    if total > limit:
                        raise WipeError(
                            "wrote %s but only %s was free: compression, deduplication or thin "
                            "provisioning is hiding the real free space; aborting"
                            % (format_size(total), format_size(start_free)))
                try:
                    _sync(f)
                except OSError as exc:
                    if not is_disk_full(exc):
                        raise
                    full = True
            finally:
                f.close()
    finally:
        if progress is not None:
            progress.finish()
    return total


def _rewrite_pass(files, passes, index, chunk, opener, progress_factory, total) -> None:
    source = pattern_source(passes[index], secrets.randbits(64))
    progress = (progress_factory(total, "freespace pass %d/%d (%s)" % (
        index + 1, len(passes), passes[index])) if progress_factory else None)
    try:
        for path, size in files:
            if size <= 0:
                continue
            f = opener(path, "r+b", buffering=0)
            try:
                write_pass(f, size, source, chunk, progress)
                _sync(f)
            finally:
                f.close()
    finally:
        if progress is not None:
            progress.finish()


# --------------------------------------------------------------------------
# Windows layer (ctypes)
# --------------------------------------------------------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x1
FILE_SHARE_WRITE = 0x2
OPEN_EXISTING = 3
FILE_FLAG_NO_BUFFERING = 0x20000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
SEM_FAILCRITICALERRORS = 1
DRIVE_REMOTE = 4
ERROR_MORE_DATA = 234
ERROR_INVALID_FUNCTION = 1

IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x700A0
IOCTL_STORAGE_QUERY_PROPERTY = 0x2D1400
IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x560000
IOCTL_DISK_GET_LENGTH_INFO = 0x7405C
IOCTL_DISK_UPDATE_PROPERTIES = 0x70140
FSCTL_LOCK_VOLUME = 0x90018
FSCTL_DISMOUNT_VOLUME = 0x90020
FSCTL_ALLOW_EXTENDED_DASD_IO = 0x90083

MAX_PHYSICAL_DRIVES = 64
LOCK_ATTEMPTS = 5
LOCK_RETRY_DELAY = 0.5

BUS_TYPES = {
    0: "Unknown", 1: "SCSI", 2: "ATAPI", 3: "ATA", 4: "1394", 5: "SSA", 6: "Fibre",
    7: "USB", 8: "RAID", 9: "iSCSI", 10: "SAS", 11: "SATA", 12: "SD", 13: "MMC",
    14: "Virtual", 15: "FileBackedVirtual", 16: "Spaces", 17: "NVMe", 18: "SCM", 19: "UFS",
}

_WIN_PHYSICAL_RE = re.compile(r"^(?:\\\\\.\\)?PhysicalDrive(\d+)$", re.IGNORECASE)
_WIN_VOLUME_RE = re.compile(r"^([A-Za-z]):\\?$")


def parse_windows_target(target: str):
    """('disk', N) for PhysicalDriveN / \\\\.\\PhysicalDriveN, ('volume', 'E:') for E:.

    Bare numbers and everything else are rejected (ValueError)."""
    t = target.strip()
    m = _WIN_PHYSICAL_RE.match(t)
    if m:
        return "disk", int(m.group(1))
    m = _WIN_VOLUME_RE.match(t)
    if m:
        return "volume", m.group(1).upper() + ":"
    if t.isdigit():
        raise ValueError("bare number %r is ambiguous; write PhysicalDrive%s or a drive letter "
                         "like E:" % (t, t))
    raise ValueError("unrecognised target %r (use PhysicalDriveN, \\\\.\\PhysicalDriveN or E:)" % t)


# --- pure parsers for the Windows structures (tested with synthetic buffers) ---

def parse_geometry_ex(buf: bytes):
    """DISK_GEOMETRY_EX -> (disk_size, bytes_per_sector).

    DISK_GEOMETRY is 24 bytes (Cylinders 8, MediaType 4, TracksPerCylinder 4,
    SectorsPerTrack 4, BytesPerSector 4); DiskSize (LARGE_INTEGER) follows at offset 24."""
    bytes_per_sector = struct.unpack_from("<I", buf, 20)[0]
    disk_size = struct.unpack_from("<q", buf, 24)[0]
    return disk_size, bytes_per_sector


def _cstr(buf: bytes, offset: int) -> str:
    if offset <= 0 or offset >= len(buf):
        return ""
    end = buf.find(b"\x00", offset)
    if end < 0:
        end = len(buf)
    return " ".join(buf[offset:end].decode("ascii", "replace").split())


def parse_storage_descriptor(buf: bytes):
    """STORAGE_DEVICE_DESCRIPTOR -> (vendor, product, bus_name).

    Layout: Version 0, Size 4, DeviceType 8, DeviceTypeModifier 9, RemovableMedia 10,
    CommandQueueing 11, VendorIdOffset 12, ProductIdOffset 16, ProductRevisionOffset 20,
    SerialNumberOffset 24, BusType 28.  Offsets are from the start of the structure."""
    if len(buf) < 32:
        return "", "", ""
    vendor_off, product_off = struct.unpack_from("<II", buf, 12)
    bus = struct.unpack_from("<I", buf, 28)[0]
    return _cstr(buf, vendor_off), _cstr(buf, product_off), BUS_TYPES.get(bus, "Bus%d" % bus)


def parse_volume_extents(buf: bytes):
    """VOLUME_DISK_EXTENTS -> [(disk_number, starting_offset, length)].

    8-byte header (NumberOfDiskExtents + padding), then 24-byte DISK_EXTENTs:
    DiskNumber 4, padding 4, StartingOffset 8, ExtentLength 8."""
    if len(buf) < 8:
        return []
    count = struct.unpack_from("<I", buf, 0)[0]
    out = []
    for i in range(count):
        base = 8 + 24 * i
        if base + 24 > len(buf):
            break
        disk, start, length = struct.unpack_from("<I4xqq", buf, base)
        out.append((disk, start, length))
    return out


def parse_length_info(buf: bytes) -> int:
    return struct.unpack_from("<q", buf, 0)[0]


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    def _proto(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)

    _H = wintypes.HANDLE
    _proto(_k32.CreateFileW, _H, wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, _H)
    _proto(_k32.DeviceIoControl, wintypes.BOOL, _H, wintypes.DWORD, wintypes.LPVOID,
           wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
           wintypes.LPVOID)
    _proto(_k32.ReadFile, wintypes.BOOL, _H, wintypes.LPVOID, wintypes.DWORD,
           ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    _proto(_k32.WriteFile, wintypes.BOOL, _H, wintypes.LPVOID, wintypes.DWORD,
           ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    _proto(_k32.SetFilePointerEx, wintypes.BOOL, _H, ctypes.c_longlong,
           ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD)
    _proto(_k32.FlushFileBuffers, wintypes.BOOL, _H)
    _proto(_k32.CloseHandle, wintypes.BOOL, _H)
    _proto(_k32.FindFirstVolumeW, _H, wintypes.LPWSTR, wintypes.DWORD)
    _proto(_k32.FindNextVolumeW, wintypes.BOOL, _H, wintypes.LPWSTR, wintypes.DWORD)
    _proto(_k32.FindVolumeClose, wintypes.BOOL, _H)
    _proto(_k32.GetVolumeInformationW, wintypes.BOOL, wintypes.LPCWSTR, wintypes.LPWSTR,
           wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
           ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD)
    _proto(_k32.GetVolumePathNamesForVolumeNameW, wintypes.BOOL, wintypes.LPCWSTR,
           wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    _proto(_k32.GetSystemWindowsDirectoryW, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
    _proto(_k32.GetFileAttributesW, wintypes.DWORD, wintypes.LPCWSTR)
    _proto(_k32.GetDriveTypeW, wintypes.UINT, wintypes.LPCWSTR)
    _proto(_k32.SetErrorMode, wintypes.UINT, wintypes.UINT)
    _proto(_shell32.IsUserAnAdmin, wintypes.BOOL)

    def _winerror() -> OSError:
        return ctypes.WinError(ctypes.get_last_error())

    def _create_file(path: str, access: int, share: int, flags: int) -> int:
        h = _k32.CreateFileW(path, access, share, None, OPEN_EXISTING, flags, None)
        if h is None or h == _INVALID_HANDLE:
            raise _winerror()
        return h

    def _close_handle(h) -> None:
        _k32.CloseHandle(h)

    def _ioctl(h, code: int, inbuf: bytes = b"", outsize: int = 0, grow: bool = False) -> bytes:
        """DeviceIoControl wrapper returning the output bytes.  Raises OSError on failure.
        With grow=True the output buffer is enlarged on ERROR_MORE_DATA."""
        while True:
            src = ctypes.create_string_buffer(bytes(inbuf), len(inbuf)) if inbuf else None
            out = ctypes.create_string_buffer(outsize) if outsize else None
            returned = wintypes.DWORD(0)
            ok = _k32.DeviceIoControl(h, code, src, len(inbuf), out, outsize,
                                      ctypes.byref(returned), None)
            if ok:
                return out.raw[:returned.value] if out is not None else b""
            err = ctypes.get_last_error()
            if grow and err == ERROR_MORE_DATA and outsize < (1 << 20):
                outsize *= 8
                continue
            raise ctypes.WinError(err)

    def _is_admin_win() -> bool:
        return bool(_shell32.IsUserAnAdmin())

    def _system_windows_dir() -> str:
        buf = ctypes.create_unicode_buffer(520)
        if not _k32.GetSystemWindowsDirectoryW(buf, 520):
            raise _winerror()
        return buf.value

    def _drive_type(root: str) -> int:
        return _k32.GetDriveTypeW(root)

    def _volume_guids() -> List[str]:
        """Every volume GUID path, e.g. '\\\\?\\Volume{...}\\'."""
        buf = ctypes.create_unicode_buffer(260)
        h = _k32.FindFirstVolumeW(buf, 260)
        if h is None or h == _INVALID_HANDLE:
            return []
        out = []
        try:
            while True:
                out.append(buf.value)
                if not _k32.FindNextVolumeW(h, buf, 260):
                    break
        finally:
            _k32.FindVolumeClose(h)
        return out

    def _volume_letter(guid: str) -> str:
        size = 512
        while True:
            buf = ctypes.create_unicode_buffer(size)
            needed = wintypes.DWORD(0)
            if _k32.GetVolumePathNamesForVolumeNameW(guid, buf, size, ctypes.byref(needed)):
                break
            if ctypes.get_last_error() == ERROR_MORE_DATA and needed.value > size:
                size = needed.value
                continue
            return ""
        for path in buf[:].split("\x00"):
            if re.match(r"^[A-Za-z]:\\$", path):
                return path[:2].upper()
        return ""

    def _volume_label_fs(root: str):
        label = ctypes.create_unicode_buffer(261)
        fs = ctypes.create_unicode_buffer(261)
        ok = _k32.GetVolumeInformationW(root, label, 261, None, None, None, fs, 261)
        return (label.value, fs.value) if ok else ("", "")

    def _win_extents(path: str):
        """Open `path` (volume GUID without trailing slash, or \\\\.\\E:) with access 0."""
        h = _create_file(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, 0)
        try:
            return parse_volume_extents(
                _ioctl(h, IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS, outsize=1024, grow=True))
        finally:
            _close_handle(h)


class WinRawFile:
    """Sector-aligned raw I/O on a Windows handle opened with FILE_FLAG_NO_BUFFERING.

    NO_BUFFERING needs page-aligned buffers and sector-multiple sizes and offsets, so data
    is copied through an anonymous mmap (page-aligned) bounce buffer.  Offsets and lengths
    must be multiples of the sector size; the caller never passes anything else because
    `size` is checked to be sector-aligned and the final chunk is `size - offset`.
    """

    def __init__(self, handle, size: int, sector_size: int, bufsize: int = DEFAULT_CHUNK,
                 owns_handle: bool = False):
        if sector_size <= 0 or size % sector_size:
            raise ValueError("size %d is not a multiple of sector size %d" % (size, sector_size))
        if bufsize <= 0 or bufsize % CHUNK_ALIGN or bufsize % sector_size:
            raise ValueError("bounce buffer must be a multiple of 4096 and of the sector size")
        self._h = handle
        self.size = size
        self.sector_size = sector_size
        self._bufsize = bufsize
        self._owns = owns_handle
        self._pos = 0
        self.closed = False
        self._mm = mmap.mmap(-1, bufsize)
        self._cbuf = (ctypes.c_char * bufsize).from_buffer(self._mm)
        self._addr = ctypes.addressof(self._cbuf)

    # -- io protocol ---------------------------------------------------------
    def readable(self): return True
    def writable(self): return True
    def seekable(self): return True
    def tell(self): return self._pos

    def fileno(self):
        raise io.UnsupportedOperation("raw Windows handle has no C file descriptor")

    def _check_open(self):
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def seek(self, offset: int, whence: int = 0) -> int:
        self._check_open()
        base = {0: 0, 1: self._pos, 2: self.size}[whence]
        new = base + offset
        if new < 0 or new % self.sector_size:
            raise ValueError("seek offset %d is not sector aligned" % new)
        if not _k32.SetFilePointerEx(self._h, new, None, 0):
            raise _winerror()
        self._pos = new
        return new

    def _check_io(self, n: int):
        self._check_open()
        if n % self.sector_size or self._pos % self.sector_size:
            raise ValueError("unaligned raw I/O: offset %d length %d, sector size %d"
                             % (self._pos, n, self.sector_size))
        if self._pos + n > self.size:
            raise ValueError("I/O past end of device")

    def write(self, data) -> int:
        mv = memoryview(data).cast("B")
        n = len(mv)
        self._check_io(n)
        done = 0
        while done < n:
            m = min(self._bufsize, n - done)
            self._mm.seek(0)
            self._mm.write(mv[done:done + m])
            written = wintypes.DWORD(0)
            if not _k32.WriteFile(self._h, self._addr, m, ctypes.byref(written), None):
                raise _winerror()
            if written.value != m:
                raise OSError(errno.EIO, "short raw write at offset %d: %d of %d bytes"
                              % (self._pos, written.value, m))
            done += m
            self._pos += m
        return n

    def readinto(self, b) -> int:
        mv = memoryview(b).cast("B")
        n = len(mv)
        self._check_io(n)
        done = 0
        while done < n:
            m = min(self._bufsize, n - done)
            got = wintypes.DWORD(0)
            if not _k32.ReadFile(self._h, self._addr, m, ctypes.byref(got), None):
                raise _winerror()
            if got.value == 0:
                break
            mv[done:done + got.value] = self._mm[:got.value]
            done += got.value
            self._pos += got.value
            if got.value != m:
                break
        return done

    def flush(self) -> None:
        if self.closed:
            return
        if not _k32.FlushFileBuffers(self._h):
            err = ctypes.get_last_error()
            if err != ERROR_INVALID_FUNCTION:       # some device handles do not support it
                raise ctypes.WinError(err)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._cbuf = None               # release the exported buffer so the mmap can close
        try:
            self._mm.close()
        finally:
            if self._owns:
                _close_handle(self._h)

    def __enter__(self): return self
    def __exit__(self, *exc): self.close()


def _allow_extended_dasd(h, required: bool) -> None:
    try:
        _ioctl(h, FSCTL_ALLOW_EXTENDED_DASD_IO)
    except OSError as exc:
        msg = "FSCTL_ALLOW_EXTENDED_DASD_IO failed (%s); the last sectors may be unreachable" % exc
        if required:
            raise WipeError(msg)
        print("warning: " + msg, file=sys.stderr)


def _open_locked(stack: contextlib.ExitStack, path: str, flags: int):
    """Open a volume read/write, take FSCTL_LOCK_VOLUME, then dismount.  The handle is
    registered with `stack` before anything else can fail.  Never dismounts without a lock."""
    try:
        h = _create_file(path, GENERIC_READ | GENERIC_WRITE,
                         FILE_SHARE_READ | FILE_SHARE_WRITE, flags)
    except OSError as exc:
        raise WipeError("cannot open %s (%s): volume in use, close Explorer/AV handles"
                        % (path, exc))
    stack.callback(_close_handle, h)
    last = None
    for attempt in range(LOCK_ATTEMPTS):
        try:
            _ioctl(h, FSCTL_LOCK_VOLUME)
            last = None
            break
        except OSError as exc:
            last = exc
            if attempt + 1 < LOCK_ATTEMPTS:
                time.sleep(LOCK_RETRY_DELAY)
    if last is not None:
        raise WipeError("could not lock %s (%s): volume in use, close Explorer/AV handles"
                        % (path, last))
    try:
        _ioctl(h, FSCTL_DISMOUNT_VOLUME)
    except OSError as exc:
        raise WipeError("could not dismount %s (%s)" % (path, exc))
    return h


def _update_properties(h) -> None:
    try:
        _ioctl(h, IOCTL_DISK_UPDATE_PROPERTIES)
    except OSError as exc:
        print("warning: IOCTL_DISK_UPDATE_PROPERTIES failed (%s); replug or rescan the disk"
              % exc, file=sys.stderr)


class WinPlatform:
    name = "windows"

    def __init__(self):
        self.warnings: List[str] = []

    def is_admin(self) -> bool:
        return _is_admin_win()

    # -- system disk detection ------------------------------------------------
    def _system_sets(self):
        """(disk_numbers, drive_letters) that hold Windows, this Python, or the cwd.
        Raises RefusedError when a local drive cannot be mapped (fail safe)."""
        paths = [_system_windows_dir(), sys.executable, os.getcwd()]
        letters, disks = set(), set()
        for p in paths:
            drive = os.path.splitdrive(os.path.abspath(p))[0]
            if not re.match(r"^[A-Za-z]:$", drive):
                continue                                    # UNC path: not a local disk
            drive = drive.upper()
            if _drive_type(drive + "\\") == DRIVE_REMOTE:
                continue
            letters.add(drive)
            try:
                extents = _win_extents("\\\\.\\" + drive)
            except OSError as exc:
                raise RefusedError("cannot map %s to a physical disk (%s); refusing to guess "
                                   "which disk is the system disk" % (drive, exc))
            if not extents:
                raise RefusedError("%s has no disk extents; cannot identify the system disk"
                                   % drive)
            disks.update(e[0] for e in extents)
        return disks, letters

    def system_ids(self):
        disks, letters = self._system_sets()
        return {"PhysicalDrive%d" % n for n in disks} | letters

    # -- listing --------------------------------------------------------------
    def _volumes(self):
        """[(VolumeInfo, [disk numbers])] for every volume that maps to a disk."""
        out = []
        for guid in _volume_guids():
            try:
                extents = _win_extents(guid.rstrip("\\"))
            except OSError:
                continue                    # CD-ROM, card reader with no media, ...
            if not extents:
                continue
            letter = _volume_letter(guid)
            label, fs = _volume_label_fs(guid)
            try:
                size = shutil.disk_usage(guid).total
            except OSError:
                size = sum(e[2] for e in extents)
            vol = VolumeInfo(letter=letter, label=label, fs=fs, size=size, guid=guid,
                             extents=len(extents))
            out.append((vol, sorted({e[0] for e in extents})))
        return out

    def _probe_disk(self, n: int):
        path = "\\\\.\\PhysicalDrive%d" % n
        try:
            h = _create_file(path, 0, FILE_SHARE_READ | FILE_SHARE_WRITE, 0)
        except OSError:
            return None
        size, sector, vendor, product, bus, note = 0, 512, "", "", "", ""
        try:
            try:
                size, sector = parse_geometry_ex(
                    _ioctl(h, IOCTL_DISK_GET_DRIVE_GEOMETRY_EX, outsize=512))
            except (OSError, struct.error):
                note = "no media or size unavailable"
            try:
                query = struct.pack("<III", 0, 0, 0)       # StorageDeviceProperty, standard query
                vendor, product, bus = parse_storage_descriptor(
                    _ioctl(h, IOCTL_STORAGE_QUERY_PROPERTY, query, 4096))
            except (OSError, struct.error):
                pass
        finally:
            _close_handle(h)
        if size <= 0:
            size, note = 0, note or "no media"
        model = " ".join((vendor + " " + product).split()) or "(unknown model)"
        return DiskInfo(id="PhysicalDrive%d" % n, path=path, size=size,
                        sector_size=sector or 512, model=model, bus=bus, note=note)

    def list_disks(self) -> List[DiskInfo]:
        self.warnings = []
        old_mode = _k32.SetErrorMode(SEM_FAILCRITICALERRORS)
        try:
            try:
                system = self.system_ids()
            except RefusedError as exc:
                system = set()
                self.warnings.append("could not identify the system disk: %s" % exc)
            volumes = self._volumes()
            disks = []
            for n in range(MAX_PHYSICAL_DRIVES):
                d = self._probe_disk(n)
                if d is None:
                    continue
                d.volumes = [v for v, nums in volumes if n in nums]
                d.is_system = d.id in system
                disks.append(d)
            return disks
        finally:
            _k32.SetErrorMode(old_mode)

    # -- target resolution ----------------------------------------------------
    def resolve_target(self, target: str) -> DiskInfo:
        kind, ident = parse_windows_target(target)     # ValueError -> usage error
        disks = self.list_disks()
        system = self.system_ids()
        if kind == "disk":
            wanted = "PhysicalDrive%d" % ident
            for d in disks:
                if d.id == wanted:
                    d.is_system = d.is_system or d.id in system
                    return d
            raise WipeError("no such disk: %s" % wanted)
        for d in disks:
            for v in d.volumes:
                if v.letter == ident:
                    return volume_target(d, v, system)
        raise WipeError("no such volume: %s" % ident)

    # -- opening for write ----------------------------------------------------
    @contextlib.contextmanager
    def open_target(self, target: str):
        """Lock/dismount and open the target.  UNTESTED against real hardware.

        Every handle is registered on an ExitStack immediately after creation, so an
        exception (including KeyboardInterrupt) closes all handles in reverse order and
        thereby releases every volume lock."""
        info = self.resolve_target(target)
        reason = refusal_reason(info, self.system_ids())
        if reason:
            raise RefusedError(reason)
        if not self.is_admin():
            raise RefusedError("administrator rights are required to open a device for write")
        kind, ident = parse_windows_target(target)
        flags = FILE_FLAG_NO_BUFFERING | FILE_FLAG_WRITE_THROUGH
        with contextlib.ExitStack() as stack:
            if kind == "volume":
                h = _open_locked(stack, info.path, flags)
                _allow_extended_dasd(h, required=False)
            else:
                # Lock + dismount every volume on this disk, letterless ones included, and
                # keep those handles open until the wipe is over.
                for vol, nums in self._volumes():
                    if ident in nums:
                        _open_locked(stack, vol.guid.rstrip("\\"), 0)
                h = _create_file(info.path, GENERIC_READ | GENERIC_WRITE,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE, flags)
                stack.callback(_close_handle, h)
                stack.callback(_update_properties, h)       # runs before the close above
                _allow_extended_dasd(h, required=False)
            size = parse_length_info(_ioctl(h, IOCTL_DISK_GET_LENGTH_INFO, outsize=8))
            if size <= 0 or size % info.sector_size:
                raise WipeError("device length %d is not a positive multiple of the sector "
                                "size %d" % (size, info.sector_size))
            raw = WinRawFile(h, size, info.sector_size, DEFAULT_CHUNK)
            stack.callback(raw.close)                       # mmap released first
            yield raw, size, info


# --------------------------------------------------------------------------
# Linux layer
# --------------------------------------------------------------------------

_LINUX_TARGET_RE = re.compile(r"^/dev/[A-Za-z0-9._+-]+$")
_LINUX_SKIP = ("loop", "ram", "zram", "sr", "fd", "md", "dm-", "nbd")


def _sysread(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="ascii", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return default


def _read_mounts(path: str = "/proc/mounts"):
    """[(device, mountpoint, fstype)]"""
    out = []
    for line in _sysread(path).splitlines():
        parts = line.split()
        if len(parts) >= 3:
            out.append((parts[0], parts[1].replace("\\040", " "), parts[2]))
    return out


class LinuxPlatform:
    name = "linux"

    def __init__(self):
        self.warnings: List[str] = []

    def is_admin(self) -> bool:
        return os.geteuid() == 0

    def _disks_of_device(self, dev: str, depth: int = 0) -> set:
        """Whole-disk names ('sdb', 'nvme0n1') underneath a block device (follows partitions,
        LVM/dm-crypt/md via slaves)."""
        name = os.path.basename(os.path.realpath(dev))
        sysdir = os.path.realpath("/sys/class/block/" + name)
        if not os.path.isdir(sysdir) or depth > 8:
            return set()
        if os.path.exists(os.path.join(sysdir, "partition")):
            return {os.path.basename(os.path.dirname(sysdir))}
        slaves = os.path.join(sysdir, "slaves")
        if os.path.isdir(slaves) and os.listdir(slaves):
            found = set()
            for s in os.listdir(slaves):
                found |= self._disks_of_device("/dev/" + s, depth + 1)
            return found
        return {name}

    def system_ids(self):
        mounts = _read_mounts()
        ids = set()
        for p in ("/", sys.executable, os.getcwd()):
            real = os.path.realpath(p)
            best = None
            for dev, mp, _fs in mounts:
                if dev.startswith("/dev/") and (real == mp or real.startswith(mp.rstrip("/") + "/")
                                                or mp == "/"):
                    if best is None or len(mp) > len(best[1]):
                        best = (dev, mp)
            if best is None:
                if p == "/":
                    raise RefusedError("cannot identify the device holding /; refusing")
                continue
            disks = self._disks_of_device(best[0])
            if not disks and p == "/":
                raise RefusedError("cannot map the root device %s to a disk; refusing" % best[0])
            ids |= {"/dev/" + d for d in disks}
        return ids

    def list_disks(self) -> List[DiskInfo]:
        self.warnings = []
        mounts = _read_mounts()
        try:
            system = self.system_ids()
        except RefusedError as exc:
            system = set()
            self.warnings.append("could not identify the system disk: %s" % exc)
        disks = []
        try:
            names = sorted(os.listdir("/sys/block"))
        except OSError:
            return disks
        for name in names:
            if name.startswith(_LINUX_SKIP):
                continue
            base = "/sys/block/" + name
            size = int(_sysread(base + "/size", "0") or 0) * 512
            sector = int(_sysread(base + "/queue/logical_block_size", "512") or 512)
            model = " ".join(_sysread(base + "/device/model").split()) or "(unknown model)"
            real = os.path.realpath(base)
            if "/usb" in real:
                bus = "USB"
            elif name.startswith("nvme"):
                bus = "NVMe"
            elif name.startswith("mmcblk"):
                bus = "SD/MMC"
            elif "/ata" in real:
                bus = "ATA"
            else:
                bus = ""
            vols = []
            for part in sorted(os.listdir(base)):
                if not os.path.exists("%s/%s/partition" % (base, part)):
                    continue
                dev = "/dev/" + part
                mp = [(m, fs) for d, m, fs in mounts if d == dev]
                vols.append(VolumeInfo(
                    letter=dev, label=mp[0][0] if mp else "", fs=mp[0][1] if mp else "",
                    size=int(_sysread("%s/%s/size" % (base, part), "0") or 0) * 512,
                    mounted=bool(mp)))
            dev = "/dev/" + name
            disks.append(DiskInfo(id=dev, path=dev, size=size, sector_size=sector, model=model,
                                  bus=bus, volumes=vols, is_system=dev in system))
        return disks

    def resolve_target(self, target: str) -> DiskInfo:
        if not _LINUX_TARGET_RE.match(target.strip()):
            raise ValueError("unrecognised target %r (use /dev/sdX)" % target)
        wanted = target.strip()
        for d in self.list_disks():
            if d.id == wanted:
                return d
        raise WipeError("%s is not a whole disk listed in /sys/block" % wanted)

    @contextlib.contextmanager
    def open_target(self, target: str):
        """UNTESTED against real hardware."""
        info = self.resolve_target(target)
        reason = refusal_reason(info, self.system_ids())
        if reason:
            raise RefusedError(reason)
        if not self.is_admin():
            raise RefusedError("root is required to open a device for write")
        try:
            fd = os.open(info.path, os.O_RDWR | os.O_EXCL)      # EBUSY if mounted
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise WipeError("%s is in use (mounted or held open)" % info.path)
            raise
        f = os.fdopen(fd, "r+b", buffering=0)
        try:
            size = f.seek(0, os.SEEK_END)
            f.seek(0)
            yield f, size, info
        finally:
            f.close()


class UnsupportedPlatform:
    name = "unsupported"

    def __init__(self):
        self.warnings: List[str] = []

    def _no(self):
        raise RefusedError("unsupported platform %r: only 'freespace' is available" % sys.platform)

    def list_disks(self): self._no()
    def system_ids(self): self._no()
    def resolve_target(self, target): self._no()
    def open_target(self, target): self._no()
    def is_admin(self): return False


def get_platform():
    if sys.platform == "win32":
        return WinPlatform()
    if sys.platform.startswith("linux"):
        return LinuxPlatform()
    return UnsupportedPlatform()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _passes_arg(s):
    try:
        return parse_passes(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def _chunk_arg(s):
    try:
        n = parse_size(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))
    if n <= 0 or n % CHUNK_ALIGN:
        raise argparse.ArgumentTypeError(
            "chunk size must be a positive multiple of %d bytes" % CHUNK_ALIGN)
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wipedrive.py",
        description="Overwrite free space or an entire drive.  Standard library only.")
    sub = parser.add_subparsers(dest="command", required=True, metavar="{list,freespace,device}")

    def common(p):
        p.add_argument("--passes", type=_passes_arg, default="zero", metavar="SPEC",
                       help="comma list of zero,one,random or the preset 'dod' (default: zero)")
        p.add_argument("--chunk-size", type=_chunk_arg, default=DEFAULT_CHUNK, metavar="SIZE",
                       help="write size, multiple of 4096, e.g. 4M (default: 4M)")
        p.add_argument("--dry-run", action="store_true",
                       help="show what would happen; write nothing")

    sub.add_parser("list", help="list physical disks (no admin needed)")
    fs = sub.add_parser("freespace", help="wipe the free space under PATH")
    fs.add_argument("path", metavar="PATH")
    common(fs)
    dv = sub.add_parser("device", help="overwrite an entire drive or volume (DESTRUCTIVE)")
    dv.add_argument("target", metavar="TARGET",
                    help="PhysicalDrive2, \\\\.\\PhysicalDrive2 or E: (Windows); /dev/sdb (Linux)")
    common(dv)
    dv.add_argument("--verify", action="store_true", help="re-read the last pass and compare")
    dv.add_argument("--confirm", metavar="TARGET",
                    help="non-interactive confirmation; must equal the resolved target ID")
    return parser


def _describe_volume(v: VolumeInfo) -> str:
    name = v.letter or (v.guid and "(no letter)") or "?"
    return "%-14s %-20s %-8s %s" % (name, '"%s"' % v.label if v.label else "", v.fs or "-",
                                    format_size(v.size))


def _print_disk(d: DiskInfo, out) -> None:
    flags = []
    if d.is_system:
        flags.append("SYSTEM DISK")
    if d.note:
        flags.append(d.note)
    print("%-16s %10s  %s  [%s]%s" % (d.id, format_size(d.size), d.model, d.bus or "?",
                                      "  <-- " + "; ".join(flags) if flags else ""), file=out)
    for v in d.volumes:
        print("    " + _describe_volume(v), file=out)


def device_summary_lines(d: DiskInfo, passes, verify) -> List[str]:
    """What a device wipe will destroy, as lines of text (CLI summary and GUI confirmation)."""
    lines = ["Target:   %s  (%s)" % (d.id, d.path),
             "Model:    %s" % d.model,
             "Bus:      %s" % (d.bus or "?"),
             "Size:     %s (%d bytes, %d-byte sectors)" % (format_size(d.size), d.size,
                                                          d.sector_size),
             "Volumes:  %s" % ("" if d.volumes else "(none recognised)")]
    lines.extend("    " + _describe_volume(v) for v in d.volumes)
    lines.append("Passes:   %s%s" % (", ".join(passes), "  + verify" if verify else ""))
    lines.append("ALL DATA ON %s WILL BE DESTROYED" % d.id)
    return lines


def freespace_summary_lines(path: str, passes, chunk: int,
                            disk_usage=shutil.disk_usage) -> List[str]:
    """What a free-space wipe will do, as lines of text (CLI summary and GUI log)."""
    usage = disk_usage(path)
    return ["Path:       %s" % os.path.abspath(path),
            "Free:       %s of %s" % (format_size(usage.free), format_size(usage.total)),
            "Passes:     %s" % ", ".join(passes),
            "Chunk size: %s" % format_size(chunk)]


def _print_device_summary(d: DiskInfo, passes, verify, out) -> None:
    for line in device_summary_lines(d, passes, verify):
        print(line, file=out)


def _cmd_list(args) -> int:
    platform = get_platform()
    disks = platform.list_disks()
    for w in getattr(platform, "warnings", []):
        print("warning: " + w, file=sys.stderr)
    if not disks:
        print("no disks found")
        return EXIT_OK
    for d in disks:
        _print_disk(d, sys.stdout)
    if not platform.is_admin():
        print("\n(not running as administrator/root: 'device' will refuse to open anything)")
    return EXIT_OK


def _cmd_freespace(args) -> int:
    path = args.path
    if not os.path.isdir(path):
        print("refused: %s is not a directory" % path, file=sys.stderr)
        return EXIT_REFUSED
    for line in freespace_summary_lines(path, args.passes, args.chunk_size):
        print(line)
    if args.dry_run:
        print("dry run: nothing written")
        return EXIT_OK
    written = fill_free_space(path, args.passes, args.chunk_size,
                              progress_factory=_progress_factory)
    print("Filled %s and removed the fill files." % format_size(written))
    return EXIT_OK


def _cmd_device(args) -> int:
    platform = get_platform()
    try:
        # 1. resolve  2. safety rule  3. privileges (RefusedError -> "refused: ..." in main)
        disk = resolve_wipe_target(platform, args.target)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_USAGE
    _print_device_summary(disk, args.passes, args.verify, sys.stdout)  # 4. summary
    if args.dry_run:                                                   # 5. dry run stops here
        print("dry run: device not opened, nothing locked or written")
        return EXIT_OK
    if not confirm(disk.id, args.confirm):                             # 6. confirmation
        print("confirmation declined or missing (type %s, or pass --confirm %s)"
              % (disk.id, disk.id), file=sys.stderr)
        return EXIT_NO_CONFIRM
    size = wipe_device(platform, args.target, disk.id, args.passes, args.chunk_size,
                       args.verify, _progress_factory)                 # 7. open, wipe, verify
    print("Wiped %s (%s)%s." % (disk.id, format_size(size), ", verified" if args.verify else ""))
    return EXIT_OK


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (EXIT_OK if code is None else EXIT_USAGE)
    handlers = {"list": _cmd_list, "freespace": _cmd_freespace, "device": _cmd_device}
    try:
        return handlers[args.command](args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_INTERRUPT
    except VerifyError as exc:
        print("VERIFY FAILED: %s" % exc, file=sys.stderr)
        return EXIT_VERIFY
    except RefusedError as exc:
        print("refused: %s" % exc, file=sys.stderr)
        return EXIT_REFUSED
    except (WipeError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
