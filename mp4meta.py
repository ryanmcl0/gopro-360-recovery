"""Minimal ISO-BMFF (MP4 / GoPro .360 / .LRV) reader: box walking and the sample
tables needed to know where every video sample lives in the file.

No third-party dependencies. Only what the recovery needs is parsed:
handler, codec fourcc, NAL length size (hvcC / avcC), stsz, stco / co64, stsc,
and the movie creation time.
"""
import datetime
import struct

CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"udta", b"dinf", b"gmhd"}


def boxes(buf, start=0, end=None):
    """Top-level boxes in buf[start:end] as (type, offset, header_len, size).
    Stops at the first malformed header."""
    end = len(buf) if end is None else end
    out, i = [], start
    while i + 8 <= end:
        size = struct.unpack(">I", buf[i:i + 4])[0]
        typ = bytes(buf[i + 4:i + 8])
        hdr = 8
        if size == 1:
            if i + 16 > end:
                break
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - i
        if size < hdr or i + size > end:
            break
        out.append((typ, i, hdr, size))
        i += size
    return out


def walk_ok(buf, start=0, end=None, depth=0):
    """True if buf[start:end] is a tree of well-formed boxes that exactly fills the range."""
    end = len(buf) if end is None else end
    i = start
    while i < end:
        if i + 8 > end:
            return False
        size = struct.unpack(">I", buf[i:i + 4])[0]
        typ = bytes(buf[i + 4:i + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
            hdr = 16
        if size < hdr or i + size > end:
            return False
        if typ in CONTAINERS and depth < 8:
            if not walk_ok(buf, i + hdr, i + size, depth + 1):
                return False
        i += size
    return i == end


def _child(buf, box, typ):
    _, i, hdr, size = box
    for c in boxes(buf, i + hdr, i + size):
        if c[0] == typ:
            return c
    return None


def _path(buf, box, types):
    for t in types:
        box = _child(buf, box, t) if box is not None else None
    return box


def _payload(buf, box):
    _, i, hdr, size = box
    return bytes(buf[i + hdr:i + size])


def _nal_length_size(stsd):
    """NAL length-prefix size from the first sample entry's hvcC / avcC, else None."""
    # stsd: version/flags(4) count(4) then sample entries
    if len(stsd) < 16:
        return None, None
    entry_size = struct.unpack(">I", stsd[8:12])[0]
    fourcc = stsd[12:16]
    entry = stsd[8:8 + entry_size]
    for cfg, off in ((b"hvcC", 21), (b"avcC", 4)):
        j = entry.find(cfg)
        if j >= 4:
            body = entry[j + 4:]
            if len(body) > off:
                return fourcc, (body[off] & 3) + 1
    return fourcc, None


def parse_moov(moov):
    """Parse a moov box (bytes, starting at its header). Returns dict with
    'tracks' (list) and 'created' (datetime or None)."""
    top = (b"moov", 0, 8, len(moov))
    created = None
    mvhd = _child(moov, top, b"mvhd")
    if mvhd:
        p = _payload(moov, mvhd)
        secs = struct.unpack(">I", p[4:8])[0] if p[0] == 0 else struct.unpack(">Q", p[4:12])[0]
        if secs:
            created = datetime.datetime(1904, 1, 1) + datetime.timedelta(seconds=secs)
    tracks = []
    for trak in boxes(moov, 8, len(moov)):
        if trak[0] != b"trak":
            continue
        hdlr = _path(moov, trak, [b"mdia", b"hdlr"])
        mdhd = _path(moov, trak, [b"mdia", b"mdhd"])
        stbl = _path(moov, trak, [b"mdia", b"minf", b"stbl"])
        tkhd = _child(moov, trak, b"tkhd")
        if not (hdlr and stbl):
            continue
        hp = _payload(moov, hdlr)
        handler = hp[8:12]
        name = hp[24:].strip(b"\0").lstrip(b"\x0b").decode("latin1", "replace").strip()
        timescale = duration = 0
        if mdhd:
            mp = _payload(moov, mdhd)
            if mp[0] == 0:
                timescale, duration = struct.unpack(">II", mp[12:20])
            else:
                timescale, duration = struct.unpack(">IQ", mp[20:32])
        width = height = 0
        if tkhd:
            tp = _payload(moov, tkhd)
            o = 76 if tp[0] == 0 else 88
            if len(tp) >= o + 8:
                width = struct.unpack(">I", tp[o:o + 4])[0] >> 16
                height = struct.unpack(">I", tp[o + 4:o + 8])[0] >> 16
        stsd = _child(moov, stbl, b"stsd")
        fourcc, nal_len = _nal_length_size(_payload(moov, stsd)) if stsd else (None, None)

        stsz = _child(moov, stbl, b"stsz")
        if stsz is None:
            continue
        sp = _payload(moov, stsz)
        fixed, count = struct.unpack(">II", sp[4:12])
        sizes = [fixed] * count if fixed else list(struct.unpack(">%dI" % count, sp[12:12 + 4 * count]))

        co = _child(moov, stbl, b"stco") or _child(moov, stbl, b"co64")
        if co is None:
            continue
        cp = _payload(moov, co)
        n = struct.unpack(">I", cp[4:8])[0]
        if co[0] == b"stco":
            chunks = list(struct.unpack(">%dI" % n, cp[8:8 + 4 * n]))
        else:
            chunks = list(struct.unpack(">%dQ" % n, cp[8:8 + 8 * n]))

        sc = _payload(moov, _child(moov, stbl, b"stsc"))
        m = struct.unpack(">I", sc[4:8])[0]
        stsc = [struct.unpack(">III", sc[8 + 12 * k:20 + 12 * k]) for k in range(m)]

        offsets, si, k = [], 0, 0
        for ci, base in enumerate(chunks):
            while k + 1 < len(stsc) and ci + 1 >= stsc[k + 1][0]:
                k += 1
            per_chunk = stsc[k][1] if stsc else 1
            pos = base
            for _ in range(per_chunk):
                if si >= len(sizes):
                    break
                offsets.append(pos)
                pos += sizes[si]
                si += 1
        tracks.append(dict(handler=handler, name=name, codec=fourcc, nal_length_size=nal_len,
                           width=width, height=height, timescale=timescale, duration=duration,
                           sizes=sizes[:len(offsets)], offsets=offsets))
    return dict(tracks=tracks, created=created)


def moov_sane(moov, mdat_start, mdat_end):
    """Strong consistency check for a candidate moov: well-formed box tree, at least one
    track, every sample inside the mdat payload, and no two samples overlapping."""
    if len(moov) < 16 or moov[4:8] != b"moov" or not walk_ok(moov, 0, len(moov)):
        return False
    try:
        info = parse_moov(moov)
    except (struct.error, IndexError, TypeError):
        return False
    spans = []
    for t in info["tracks"]:
        if t["handler"] == b"vide" and t["nal_length_size"] is None:
            return False
        for o, s in zip(t["offsets"], t["sizes"]):
            if o < mdat_start or o + s > mdat_end:
                return False
            if s:
                spans.append((o, s))
    if not spans:
        return False
    spans.sort()
    for (o1, s1), (o2, _) in zip(spans, spans[1:]):
        if o1 + s1 > o2:
            return False
    return True


def video_tracks(info):
    return [t for t in info["tracks"] if t["handler"] == b"vide"]


def describe(info):
    """Guess what kind of GoPro file this is from its tracks."""
    vids = video_tracks(info)
    if len(vids) >= 2 and len({(v["width"], v["height"]) for v in vids}) == 1:
        return "360"          # GoPro MAX / Fusion style: one track per lens
    if vids and max(v["width"] for v in vids) <= 1280:
        return "LRV"          # low-res proxy written alongside the main file
    return "MP4"


TABLE_WIDTH = {b"stco": 4, b"co64": 8, b"stss": 4, b"stts": 8, b"ctts": 8, b"stsc": 12}


def _fourcc_ok(t):
    return all(0x20 <= c <= 0x7E or c == 0xA9 for c in t)


def moov_prefix_check(buf, total, window, mdat_start, mdat_end):
    """Check a moov that is only partly read (buf = its first len(buf) of `total` bytes).
    Returns (ok, evidence): ok=False if anything readable contradicts a valid moov; evidence
    counts box headers and table entries that lie at or after byte `window` (the newest unit),
    i.e. how much the newest bytes were actually tested."""
    n = len(buf)
    ev = [0]
    span = mdat_end - mdat_start

    def table(typ, s, e):
        # s = payload start (after version/flags), e = declared payload end
        if s + 4 > n:
            return True
        if typ == b"stsz":
            if s + 8 > n:
                return True
            fixed, count = struct.unpack(">II", buf[s:s + 8])
            if fixed:
                return fixed <= span and s + 8 == e
            first, width = s + 8, 4
        else:
            count = struct.unpack(">I", buf[s:s + 4])[0]
            first, width = s + 4, TABLE_WIDTH[typ]
        if first + count * width != e:
            return False
        prev = 0
        for k in range(count):
            o = first + k * width
            if o + width > n:
                break
            if typ == b"stsz":
                ok = int.from_bytes(buf[o:o + 4], "big") <= span
            elif typ in (b"stco", b"co64"):
                v = int.from_bytes(buf[o:o + width], "big")
                ok = mdat_start <= v < mdat_end and v >= prev
                prev = v
            elif typ == b"stss":
                v = int.from_bytes(buf[o:o + 4], "big")
                ok = v > prev
                prev = v
            elif typ in (b"stts", b"ctts"):
                ok = 1 <= int.from_bytes(buf[o:o + 4], "big") <= 100_000_000
            else:  # stsc: first_chunk ascending, sane samples-per-chunk and description index
                fc, spc, di = struct.unpack(">III", buf[o:o + 12])
                ok = fc > prev and 1 <= spc <= 1_000_000 and 1 <= di <= 64
                prev = fc
            if o >= window:
                ev[0] += 1
            if not ok:
                return False
        return True

    def walk(s, e, depth):
        i = s
        while i < e:
            if i + 8 > n:
                return True
            size = struct.unpack(">I", buf[i:i + 4])[0]
            typ = bytes(buf[i + 4:i + 8])
            hdr = 8
            if size == 1:
                if i + 16 > n:
                    return True
                size = struct.unpack(">Q", buf[i + 8:i + 16])[0]
                hdr = 16
            if size < hdr or i + size > e or not _fourcc_ok(typ):
                return False
            if i + 7 >= window:
                ev[0] += 1
            if typ in CONTAINERS and depth < 8:
                if not walk(i + hdr, i + size, depth + 1):
                    return False
            elif typ in TABLE_WIDTH or typ == b"stsz":
                if not table(typ, i + hdr + 4, i + size):
                    return False
            i += size
        return True

    if n < 8 or buf[4:8] != b"moov" or struct.unpack(">I", buf[:4])[0] != total:
        return False, ev[0]
    return walk(8, total, 0), ev[0]
