#!/usr/bin/env python3
"""Recover GoPro .360 / .MP4 / .LRV files from a raw card image, undoing the
fragmentation caused by the camera writing two files at once.

GoPro cameras record the main file and its low-res .LRV proxy simultaneously,
so on FAT32/exFAT the two files' clusters interleave (on a GoPro MAX: runs of the
.360 in 512 KB allocation units, separated by 512 KB LRV chunks). Once the card is
formatted, the cluster chains are gone and a contiguous carve splices proxy data
into the video: flashes, magenta/checkerboard blocks, freezes, "moov atom not
found", and untrunc merging the two lenses into one track.

If the camera finalised the recording, the mdat header still holds the true mdat
size and the original moov (with every track's sample table) sits right after the
mdat, shifted by however many foreign units precede it. This tool finds that moov,
then walks the file one allocation unit at a time: a unit belongs to the file only
if every video sample crossing it keeps an exact length-prefixed NAL chain summing
to the sample size the moov records. Foreign units are skipped. The result is the
original file, byte for byte (validated by md5 against known-good copies).

usage:
  gopro_recover.py scan    <image>
  gopro_recover.py recover <image> [--offset N ...] [--out DIR] [--verify] [--expect FILE]
                                   [--unit BYTES] [--include-lrv] [--max-shift BYTES]
  gopro_recover.py verify  <file>

Work on an image of the card (e.g. ddrescue), never on the card itself.
"""
import argparse
import bisect
import hashlib
import mmap
import os
import shutil
import struct
import subprocess
import sys

import mp4meta

KiB = 1024
MiB = 1024 * KiB
EXT = {"360": ".360", "LRV": ".LRV", "MP4": ".MP4"}
HEADER_BOXES = {b"ftyp", b"free", b"skip", b"wide", b"uuid", b"udta", b"meta", b"pdin"}
TAIL_PAD_BOXES = {b"free", b"skip", b"wide", b"uuid"}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# filesystem geometry (cluster size + data region), so scanning and units line up
# --------------------------------------------------------------------------
def _boot_sector(m, off):
    if off + 512 > len(m):
        return None
    b = m[off:off + 512]
    if b[3:11] == b"EXFAT   ":
        bps, spc = 1 << b[108], 1 << b[109]
        heap = struct.unpack("<I", b[88:92])[0]
        return dict(fs="exFAT", cluster=bps * spc, data_start=off + heap * bps)
    if b[510:512] != b"\x55\xaa":
        return None
    fstype = None
    if b[82:87] == b"FAT32":
        fstype = "FAT32"
    elif b[54:59] in (b"FAT16", b"FAT12", b"FAT  "):
        fstype = b[54:59].decode().strip()
    if not fstype:
        return None
    bps = struct.unpack("<H", b[11:13])[0]
    spc = b[13]
    if bps not in (512, 1024, 2048, 4096) or spc == 0:
        return None
    rsv = struct.unpack("<H", b[14:16])[0]
    nfats = b[16]
    root_entries = struct.unpack("<H", b[17:19])[0]
    fatsz = struct.unpack("<H", b[22:24])[0] or struct.unpack("<I", b[36:40])[0]
    root_secs = (root_entries * 32 + bps - 1) // bps
    return dict(fs=fstype, cluster=bps * spc, data_start=off + (rsv + nfats * fatsz + root_secs) * bps)


def fs_geometry(m):
    """Find the first FAT/exFAT volume (superfloppy, MBR or GPT). None if not found."""
    g = _boot_sector(m, 0)
    if g:
        return g
    mbr = m[:512]
    if mbr[510:512] != b"\x55\xaa":
        return None
    for i in range(4):
        e = mbr[446 + 16 * i:462 + 16 * i]
        ptype, lba = e[4], struct.unpack("<I", e[8:12])[0]
        if ptype == 0xEE and m[512:520] == b"EFI PART":
            hdr = m[512:1024]
            ent_lba = struct.unpack("<Q", hdr[72:80])[0]
            n, esz = struct.unpack("<II", hdr[80:88])
            for k in range(min(n, 128)):
                ent = m[ent_lba * 512 + k * esz:ent_lba * 512 + (k + 1) * esz]
                first = struct.unpack("<Q", ent[32:40])[0]
                if first:
                    g = _boot_sector(m, first * 512)
                    if g:
                        return g
        elif ptype and lba:
            g = _boot_sector(m, lba * 512)
            if g:
                return g
    return None


# --------------------------------------------------------------------------
# candidate files
# --------------------------------------------------------------------------
def find_starts(m, align, base):
    """Offsets of every 'ftyp' box that starts on an allocation boundary."""
    out, i = [], m.find(b"ftyp", 4)
    while i != -1:
        s = i - 4
        if (s - base) % align == 0:
            size = struct.unpack(">I", m[s:s + 4])[0]
            if 8 <= size <= 256:
                nxt = m[s + size + 4:s + size + 8]
                if nxt in (b"mdat", b"moov", b"free", b"skip", b"wide", b"uuid"):
                    out.append(s)
        i = m.find(b"ftyp", i + 1)
    return out


def read_header(m, start):
    """Walk the top-level boxes at the start of a file up to its mdat.
    Returns dict(mdat_payload=(a, b) logical, front_moov=(off, size) or None, finalised)."""
    i, front_moov = 0, None
    for _ in range(16):
        p = start + i
        size = struct.unpack(">I", m[p:p + 4])[0]
        typ = m[p + 4:p + 8]
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", m[p + 8:p + 16])[0]
            hdr = 16
        if typ == b"mdat":
            if size == 0:
                return dict(mdat_payload=(i + hdr, None), front_moov=front_moov, finalised=False)
            return dict(mdat_payload=(i + hdr, i + size), front_moov=front_moov, finalised=True)
        if typ == b"moov":
            front_moov = (i, size)
        elif typ not in HEADER_BOXES or size < 8:
            return None
        i += size
    return None


def find_tail_moovs(m, start, mdat_end, align, max_shift):
    """Yield places the moov may sit after the mdat, nearest first: at mdat_end + k*align,
    optionally behind a free/skip pad that starts there. Yields (shift, phys_moov, pad_len).
    Searches in growing windows so a nearby moov is found without reading gigabytes."""
    lo = start + mdat_end
    end = min(len(m), lo + max_shift)
    a = lo
    for w in (64 * MiB, 512 * MiB, max_shift):
        b = min(end, lo + w)
        if b <= a:
            continue
        j = m.find(b"moov", max(a, lo + 4), b + 8)
        while j != -1:
            box = j - 4
            if m[box + 12:box + 16] == b"mvhd":         # every real moov opens with mvhd
                r = (box - lo) % align
                if r == 0:
                    yield (box - lo, box, 0)
                else:
                    p = box - r                          # aligned start before it: a pad box?
                    psz = struct.unpack(">I", m[p:p + 4])[0]
                    if m[p + 4:p + 8] in TAIL_PAD_BOXES and p + psz == box:
                        yield (p - lo, box, psz)
            j = m.find(b"moov", j + 1, b + 8)
        a = b


# --------------------------------------------------------------------------
# logical -> physical mapping
# --------------------------------------------------------------------------
class Mapping:
    """gaps[k] = (logical_unit, n_units): n foreign units sit before logical unit `logical_unit`."""

    def __init__(self, m, start, unit):
        self.m, self.start, self.unit = m, start, unit
        self.at, self.cum = [], []

    def add(self, u, n):
        self.at.append(u)
        self.cum.append((self.cum[-1] if self.cum else 0) + n)

    def pop(self):
        self.at.pop()
        self.cum.pop()

    def skipped(self):
        return self.cum[-1] if self.cum else 0

    def phys(self, L):
        k = bisect.bisect_right(self.at, L // self.unit)
        return self.start + L + (self.cum[k - 1] if k else 0) * self.unit

    def read(self, L, n):
        out, U = [], self.unit
        while n > 0:
            take = min(n, (L // U + 1) * U - L)
            p = self.phys(L)
            out.append(self.m[p:p + take])
            L += take
            n -= take
        return b"".join(out)

    def gaps(self):
        prev, out = 0, []
        for u, c in zip(self.at, self.cum):
            out.append((u, c - prev))
            prev = c
        return out


def nal_header_ok(codec, h):
    if h[0] & 0x80:
        return False
    if codec in (b"hvc1", b"hev1"):
        layer = ((h[0] & 1) << 5) | (h[1] >> 3)
        return layer == 0 and (h[1] & 7) != 0 and (h[0] >> 1) <= 40
    return 1 <= (h[0] & 0x1F) <= 23          # H.264


class Checker:
    """Validates the NAL chains of every video sample that overlaps a logical unit."""

    def __init__(self, mapping, info):
        self.map = mapping
        self.samples = []                     # (offset, size, codec, nal_len)
        for t in mp4meta.video_tracks(info):
            if not t["nal_length_size"]:
                continue
            for o, s in zip(t["offsets"], t["sizes"]):
                if s:
                    self.samples.append((o, s, t["codec"], t["nal_length_size"]))
        self.samples.sort()
        self.starts = [s[0] for s in self.samples]
        self.magic = []                       # (offset, bytes) for metadata tracks with a known header
        for t in info["tracks"]:
            if t["codec"] == b"gpmd":
                self.magic += [(o, b"DEVC") for o, s in zip(t["offsets"], t["sizes"]) if s >= 4]
        self.magic.sort()

    def overlapping(self, u):
        U = self.map.unit
        lo, hi = u * U, (u + 1) * U
        k = max(0, bisect.bisect_right(self.starts, lo) - 1)
        while k < len(self.samples) and self.samples[k][0] < hi:
            o, s = self.samples[k][:2]
            if o + s > lo:
                yield self.samples[k]
            k += 1

    def check(self, u):
        """(ok, evidence): ok=False if some sample's chain is contradicted using bytes up to unit u;
        evidence = number of NAL headers / magics read that lie in unit u."""
        U, ev = self.map.unit, 0
        for o, s, codec, L in self.overlapping(u):
            p, end = o, o + s
            while p < end:
                if (p + L + 1) // U > u:      # header not fully readable yet
                    break
                h = self.map.read(p, L + 2)
                if (p + L + 1) // U == u or p // U == u:
                    ev += 1
                n = int.from_bytes(h[:L], "big")
                if n < 1 or p + L + n > end or not nal_header_ok(codec, h[L:L + 2]):
                    return False, ev
                p += L + n
        lo, hi = u * U, (u + 1) * U
        k = bisect.bisect_left(self.magic, (lo, b""))
        while k < len(self.magic) and self.magic[k][0] < hi:
            o, mg = self.magic[k]
            if (o + len(mg) - 1) // U <= u:
                ev += 1
                if self.map.read(o, len(mg)) != mg:
                    return False, ev
            k += 1
        return True, ev


def resolve_gaps(m, start, unit, info, mdat_payload, moov_logical, budget):
    """Place exactly `budget` foreign units before the moov. Returns (Mapping, ambiguous) or None.
    `ambiguous` counts gaps whose exact boundary could not be pinned down because the units
    just before it contained no NAL header to test (verify those files by decoding)."""
    mp = Mapping(m, start, unit)
    chk = Checker(mp, info)
    if not chk.samples:
        return None
    last_unit = (mdat_payload[1] - 1) // unit
    last_ev, ambiguous = -1, 0
    for u in range(last_unit + 1):
        ok, ev = chk.check(u)
        if not ok:
            # the gap starts somewhere after the last unit that held testable data: try the
            # latest boundary first, then earlier ones (a NAL header straddling a boundary
            # pins the gap to that boundary)
            placed = None
            for v in range(u, last_ev, -1):
                for g in range(1, budget - mp.skipped() + 1):
                    mp.add(v, g)
                    if all(chk.check(w)[0] for w in range(v, u + 1)):
                        placed = v
                        break
                    mp.pop()
                if placed is not None:
                    break
            if placed is None:
                return None
            if placed > last_ev + 1:
                ambiguous += 1
            ev = chk.check(u)[1]
        if ev:
            last_ev = u
    rest = budget - mp.skipped()
    if rest:
        # foreign units after the last tested data and before the moov: put them just before it
        at = moov_logical // unit
        if at <= last_ev:
            return None
        mp.add(at, rest)
        if at - last_ev > 1:
            ambiguous += 1
    return mp, ambiguous


def read_moov(m, start, unit, mdat_payload, moov_logical, moov_phys, prior_skip, max_gap=64):
    """Read the moov, allowing foreign units inside it (a moov larger than an allocation unit
    can straddle gaps). Unit by unit, the readable prefix must stay a consistent moov (box
    headers, sample sizes within the mdat, ascending chunk offsets). Returns
    (moov_bytes, [(logical_unit, n)]) or None."""
    size = struct.unpack(">I", m[moov_phys:moov_phys + 4])[0]
    if size == 1:
        size = struct.unpack(">Q", m[moov_phys + 8:moov_phys + 16])[0]
    if size < 16 or size > 512 * MiB:
        return None
    a, b = mdat_payload

    def mapping(extra):
        mp = Mapping(m, start, unit)
        if prior_skip:
            mp.add(0, prior_skip)                # all earlier gaps lie before the moov
        for u, n in extra:
            mp.add(u, n)
        return mp

    def read(extra, upto):
        mp = mapping(extra)
        if mp.phys(moov_logical + upto - 1) >= len(m):
            return None
        return mp.read(moov_logical, upto)

    buf = read([], size)
    if buf is not None and mp4meta.moov_sane(buf, a, b):
        return buf, []
    # greedy: decide the gap (if any) before each unit boundary inside the moov
    extra = []
    for u in range(moov_logical // unit + 1, (moov_logical + size - 1) // unit + 1):
        upto = min(size, (u + 1) * unit - moov_logical)
        window = u * unit - moov_logical
        for g in range(0, max_gap + 1):
            cand = extra + ([(u, g)] if g else [])
            pre = read(cand, upto)
            if pre is not None and mp4meta.moov_prefix_check(pre, size, window, a, b)[0]:
                extra = cand
                break
        else:
            return None
    buf = read(extra, size)
    if buf is not None and mp4meta.moov_sane(buf, a, b):
        return buf, extra
    return None


# --------------------------------------------------------------------------
# one file
# --------------------------------------------------------------------------
def analyse(m, start, align, max_shift, unit_override=None, quick=False):
    """Locate the moov and, unless quick, resolve the mapping. Returns a result dict."""
    hdr = read_header(m, start)
    res = dict(offset=start, status="not an MP4 header")
    if hdr is None:
        return res
    a, b = hdr["mdat_payload"]
    if not hdr["finalised"]:
        res["status"] = "not finalised (mdat size 0): no moov; try untrunc"
        return res
    def candidates():
        if hdr["front_moov"]:
            o, _ = hdr["front_moov"]
            yield 0, start + o, 0, o
        for shift, phys, pad in find_tail_moovs(m, start, b, align, max_shift):
            yield shift, phys, pad, b + pad
    res["status"] = "finalised, but no moov found within --max-shift"
    for shift, phys, pad, moov_logical in candidates():
        units = [unit_override] if unit_override else \
            [u for u in (4 * MiB, 2 * MiB, MiB, 512 * KiB, 256 * KiB, 128 * KiB, 64 * KiB, 32 * KiB,
                         16 * KiB, 8 * KiB, 4 * KiB, 2 * KiB, 1 * KiB, 512) if u >= align and u % align == 0]
        for unit in units:
            if shift % unit:
                continue
            budget = shift // unit
            got = read_moov(m, start, unit, (a, b), moov_logical, phys, budget)
            if not got:
                continue
            moov, moov_gaps = got
            info = mp4meta.parse_moov(moov)
            res.update(status="moov found", info=info, kind=mp4meta.describe(info), unit=unit,
                       shift=shift, moov_logical=moov_logical, moov_size=len(moov),
                       moov_gaps=moov_gaps, size=moov_logical + len(moov))
            if quick:
                return res
            r = resolve_gaps(m, start, unit, info, (a, b), moov_logical, budget)
            if r is None:
                res["status"] = "moov found, mdat mapping failed at this unit"
                continue
            mp, ambiguous = r
            for u, n in moov_gaps:
                mp.add(u, n)
            res.update(status="ok", mapping=mp, ambiguous=ambiguous)
            return res
    return res


def write_file(res, path):
    mp, size, U = res["mapping"], res["size"], res["unit"]
    h = hashlib.md5()
    with open(path, "wb") as f:
        L = 0
        while L < size:
            take = min(U - L % U, size - L)
            p = mp.phys(L)
            chunk = mp.m[p:p + take]
            h.update(chunk)
            f.write(chunk)
            L += take
    return h.hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 * MiB), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(path):
    """Decode every video stream with ffmpeg; return [(stream, frames_decoded, error_lines)]."""
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        log("  (ffmpeg/ffprobe not found: skipping decode check)")
        return None
    q = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
                        "stream=index", "-of", "csv=p=0", path], capture_output=True, text=True)
    out = []
    for idx in [int(x.strip(",")) for x in q.stdout.split() if x.strip(",").isdigit()]:
        r = subprocess.run(["ffmpeg", "-v", "error", "-nostats", "-i", path, "-map", f"0:{idx}",
                            "-f", "null", "-"], capture_output=True, text=True)
        c = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(idx), "-count_frames",
                            "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        frames = c.stdout.strip().strip(",")
        out.append((idx, frames, len([l for l in r.stderr.splitlines() if l.strip()])))
    return out


def summary(res):
    info = res.get("info")
    if not info:
        return res["status"]
    vids = mp4meta.video_tracks(info)
    v = ", ".join(f"{t['codec'].decode(errors='replace')} {t['width']}x{t['height']} "
                  f"{len(t['offsets'])} fr" for t in vids)
    dur = max((t["duration"] / t["timescale"] for t in vids if t["timescale"]), default=0)
    when = info["created"].strftime("%Y-%m-%d %H:%M:%S") if info["created"] else "?"
    return (f"{res['kind']:3s} {when}  {dur:6.1f}s  [{v}]  "
            f"moov {res['shift'] / MiB:.1f} MiB past the mdat end")


def mark_proxies(results):
    """A GoPro .LRV shares its main file's creation time and has a smaller picture."""
    def pixels(r):
        return max((t["width"] * t["height"] for t in mp4meta.video_tracks(r["info"])), default=0)
    found = [r for r in results if r.get("info") and r["info"]["created"]]
    for r in found:
        twins = [o for o in found if o is not r and o["info"]["created"] == r["info"]["created"]]
        if any(pixels(o) > pixels(r) for o in twins):
            r["kind"] = "LRV"


def out_name(res):
    info = res["info"]
    stamp = info["created"].strftime("%Y%m%d_%H%M%S") if info["created"] else "unknown"
    return f"GOPRO_{res['kind']}_{stamp}_at{res['offset']}{EXT[res['kind']]}"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def open_image(path):
    f = open(path, "rb")
    return mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)


def geometry(m):
    g = fs_geometry(m)
    if g:
        log(f"{g['fs']} volume: {g['cluster'] // KiB} KiB clusters, data region at byte {g['data_start']}")
        return g["cluster"], g["data_start"]
    log("no FAT/exFAT boot sector found: scanning at 512-byte alignment")
    return 512, 0


def cmd_scan(a):
    m = open_image(a.image)
    align, base = geometry(m)
    starts = find_starts(m, align, base)
    log(f"{len(starts)} file start(s) found\n")
    results = [analyse(m, s, align, a.max_shift, a.unit, quick=True) for s in starts]
    mark_proxies(results)
    for res in results:
        print(f"{res['offset']:>14d}  {summary(res)}")


def cmd_recover(a):
    m = open_image(a.image)
    align, base = geometry(m)
    starts = a.offset or find_starts(m, align, base)
    os.makedirs(a.out, exist_ok=True)
    failures = decode_errors = 0
    results = [analyse(m, s, align, a.max_shift, a.unit) for s in starts]
    mark_proxies(results)
    for res in results:
        s = res["offset"]
        if res["status"] != "ok":
            log(f"{s}: {res['status']}")
            failures += res["status"].startswith("moov found")
            continue
        if res["kind"] == "LRV" and not (a.include_lrv or a.offset):
            log(f"{s}: LRV proxy, skipped (use --include-lrv)")
            continue
        path = os.path.join(a.out, out_name(res))
        digest = write_file(res, path)
        gaps = res["mapping"].gaps()
        log(f"{s}: {summary(res)}")
        log(f"  -> {path}  ({res['size']} bytes, md5 {digest})")
        log(f"  {sum(n for _, n in gaps)} foreign unit(s) skipped in {len(gaps)} gap(s)"
            + (f"; {res['ambiguous']} gap boundary(ies) were ambiguous: check with --verify"
               if res["ambiguous"] else ""))
        if a.expect:
            ref = md5_file(a.expect)
            log(f"  expected md5 {ref}: {'MATCH' if ref == digest else 'DIFFERENT'}")
        if res["ambiguous"]:
            log("  WARNING: some gap boundaries fell in stretches with no NAL header to test, so "
                "their exact position is a best guess. Decode-testing the result:")
        if a.verify or res["ambiguous"]:
            checked = verify(path) or []
            for idx, frames, errs in checked:
                log(f"  stream {idx}: {frames} frames decoded, {errs} decoder error line(s)")
            if any(errs for _, _, errs in checked):
                decode_errors += 1
    sys.exit(1 if failures else 2 if decode_errors else 0)


def cmd_verify(a):
    for idx, frames, errs in verify(a.file) or []:
        print(f"stream {idx}: {frames} frames decoded, {errs} decoder error line(s)")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--unit", type=int, help="force the allocation unit in bytes (default: auto)")
    common.add_argument("--max-shift", type=int, default=2 * 1024 * MiB,
                        help="how far past the mdat to look for the moov (default 2 GiB)")
    s = sub.add_parser("scan", parents=[common], help="list every recoverable file in an image")
    s.add_argument("image")
    s.set_defaults(fn=cmd_scan)
    r = sub.add_parser("recover", parents=[common], help="rebuild files from an image")
    r.add_argument("image")
    r.add_argument("--offset", type=int, action="append", help="byte offset of a file's ftyp box (repeatable)")
    r.add_argument("--out", default="recovered")
    r.add_argument("--include-lrv", action="store_true", help="also rebuild .LRV proxies")
    r.add_argument("--verify", action="store_true", help="decode-test each result with ffmpeg")
    r.add_argument("--expect", help="known-good copy to compare md5 against (method validation)")
    r.set_defaults(fn=cmd_recover)
    v = sub.add_parser("verify", help="decode-test a file with ffmpeg")
    v.add_argument("file")
    v.set_defaults(fn=cmd_verify)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
