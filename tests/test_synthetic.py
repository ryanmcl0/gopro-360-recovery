#!/usr/bin/env python3
"""Self-contained tests: build fragmented card images from synthetic videos and check the
rebuilt file is byte-identical to the original. Needs ffmpeg with libx265 + libx264.

    python3 tests/test_synthetic.py        (or: python3 -m pytest tests)
"""
import hashlib
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import gopro_recover  # noqa: E402

KiB = 1024


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def make_videos(d, slices=1):
    """A two-track HEVC file shaped like a GoPro MAX .360 (identical parameters per lens,
    plus AAC audio, moov written at the end) and an H.264 'proxy' used as foreign data."""
    main = os.path.join(d, "main.mp4")
    proxy = os.path.join(d, "proxy.mp4")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x320:rate=30:duration=6",
           "-f", "lavfi", "-i", "mandelbrot=size=640x320:rate=30",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
           "-map", "0:v", "-map", "1:v", "-map", "2:a", "-t", "6",
           "-c:v", "libx265", "-x265-params", f"log-level=error:keyint=30:ctu=16:slices={slices}", "-b:v", "6M",
           "-tag:v", "hvc1", "-c:a", "aac", main)
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x160:rate=30:duration=6",
           "-c:v", "libx264", "-b:v", "4M", proxy)
    return main, proxy


def fat32_header(cluster):
    """MBR + FAT32 boot sector; returns (bytes, data_start)."""
    lba, rsv, nfats, fatsz, bps = 2048, 32, 2, 100, 512
    mbr = bytearray(512)
    mbr[446 + 4] = 0x0C
    struct.pack_into("<II", mbr, 446 + 8, lba, 1 << 22)
    mbr[510:512] = b"\x55\xaa"
    bs = bytearray(512)
    bs[0:3] = b"\xeb\x58\x90"
    struct.pack_into("<HBHB", bs, 11, bps, cluster // bps, rsv, nfats)
    struct.pack_into("<I", bs, 36, fatsz)
    bs[82:90] = b"FAT32   "
    bs[510:512] = b"\x55\xaa"
    data_start = (lba + rsv + nfats * fatsz) * bps
    img = bytearray(data_start)
    img[0:512] = mbr
    img[lba * bps:lba * bps + 512] = bs
    return img, data_start


def interleave(main, foreign, unit, rng, gap_inside_moov=False, max_run=9, max_gap=3):
    """Lay `main` out in runs of 1..max_run units with 1..max_gap foreign units between.
    Returns (image body, number of foreign units)."""
    n_units = (len(main) + unit - 1) // unit
    cuts, u = set(), 0
    while True:
        u += rng.randint(1, max_run)
        if u >= n_units:
            break
        cuts.add(u)
    if gap_inside_moov:
        moov = main.rfind(b"moov") - 4
        first, last = moov // unit + 1, (len(main) - 1) // unit
        assert last >= first, "moov must span a unit boundary for this test"
        cuts.add(rng.randint(first, last))
    body, fpos, skipped = bytearray(), 0, 0
    for k in range(n_units):
        if k in cuts:
            g = rng.randint(1, max_gap)
            for _ in range(g):
                body += foreign[fpos:fpos + unit].ljust(unit, b"\0")
                fpos = (fpos + unit) % max(unit, len(foreign) - unit)
            skipped += g
        body += main[k * unit:(k + 1) * unit].ljust(unit, b"\0")
    return body, skipped


def run_case(name, unit, with_fs, gap_inside_moov=False, seed=1, cluster=32 * KiB, slices=1):
    d = tempfile.mkdtemp(prefix="gopro_test_")
    try:
        main_p, proxy_p = make_videos(d, slices)
        main, foreign = open(main_p, "rb").read(), open(proxy_p, "rb").read()
        rng = random.Random(seed)
        if with_fs:
            img, data_start = fat32_header(cluster)
        else:
            img, data_start = bytearray(), 0
        img += foreign[:unit * 3].ljust(unit * 3, b"\0")        # unrelated data first
        start = data_start + unit * 3
        body, skipped = interleave(main, foreign, unit, rng, gap_inside_moov)
        img += body + os.urandom(unit * 4)
        img_p = os.path.join(d, "card.img")
        open(img_p, "wb").write(img)
        out = os.path.join(d, "out")
        try:
            gopro_recover.main(["recover", img_p, "--offset", str(start), "--out", out])
        except SystemExit as e:
            assert not e.code, f"{name}: recover exited {e.code}"
        files = os.listdir(out)
        assert len(files) == 1, f"{name}: expected one output, got {files}"
        got = open(os.path.join(out, files[0]), "rb").read()
        ok = hashlib.md5(got).digest() == hashlib.md5(main).digest()
        print(f"{'PASS' if ok else 'FAIL'}  {name}: unit {unit // KiB} KiB, {skipped} foreign units, "
              f"{len(main)} bytes")
        assert ok, f"{name}: rebuilt file differs from the original"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_fat32_512k_units():
    run_case("FAT32 volume (32 KiB clusters), 512 KiB allocation units", 512 * KiB, with_fs=True)


def test_raw_image_small_units():
    run_case("no filesystem, 64 KiB units (auto-detected)", 64 * KiB, with_fs=False, seed=7)


def test_moov_straddles_gap():
    # fine-grained: 8 KiB units (real GoPro files resolve at 512 KiB), with 16 slices per frame
    # so every unit holds a NAL header to test; the moov itself is split by foreign data
    run_case("moov split by foreign units", 8 * KiB, with_fs=False, gap_inside_moov=True, seed=3,
             slices=16)


def test_unfinalised_is_reported():
    d = tempfile.mkdtemp(prefix="gopro_test_")
    try:
        main_p, _ = make_videos(d)
        main = bytearray(open(main_p, "rb").read())
        i = main.find(b"mdat") - 4
        struct.pack_into(">I", main, i, 0)                       # size 0 = never finalised
        main = main[:main.rfind(b"moov") - 4]
        import mmap
        p = os.path.join(d, "card.img")
        open(p, "wb").write(bytes(main) + b"\0" * 65536)
        with open(p, "rb") as f:
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            res = gopro_recover.analyse(m, 0, 512, 1 << 30)
        assert res["status"].startswith("not finalised"), res["status"]
        print("PASS  unfinalised file is reported, not guessed")
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg is required for the tests")
    test_fat32_512k_units()
    test_raw_image_small_units()
    test_moov_straddles_gap()
    test_unfinalised_is_reported()
    print("all tests passed")
