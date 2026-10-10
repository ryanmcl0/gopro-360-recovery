#!/usr/bin/env python3
"""Compare two recoveries of the same footage frame by frame and list where they differ.

    compare_versions.py <old> <new> [--old-stream N] [--new-stream N]

Different recoveries often start at different points (a lost first GOP, trimmed
heads), so the frame offset between them is found automatically by matching
thumbnails. Every old frame is then compared with its counterpart in the new file;
runs of frames that differ much more than usual are listed. Look at those with
tools/contact_sheet.py to see which version is the damaged one.
Needs ffmpeg and numpy.
"""
import argparse
import subprocess

import numpy as np

TW, TH = 284, 92


def thumbs(path, stream):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{stream}",
                        "-vf", f"scale={TW}:{TH}", "-pix_fmt", "gray", "-f", "rawvideo", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(p.stdout, np.uint8).reshape(-1, TH, TW).astype(np.float32)


def find_offset(old, new):
    votes = {}
    for i in np.linspace(0, len(old) - 1, 25).astype(int):
        d = np.abs(new - old[i]).mean(axis=(1, 2))
        off = int(d.argmin()) - int(i)
        votes[off] = votes.get(off, 0) + 1
    return max(votes, key=votes.get)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--old-stream", type=int, default=0)
    ap.add_argument("--new-stream", type=int, default=0)
    a = ap.parse_args()
    old, new = thumbs(a.old, a.old_stream), thumbs(a.new, a.new_stream)
    off = find_offset(old, new)
    print(f"old has {len(old)} frames, new has {len(new)}; old frame i == new frame i{off:+d}")
    d = np.full(len(old), np.nan)
    tile = np.full(len(old), np.nan)
    for i in range(len(old)):
        js = [j for j in (i + off - 1, i + off, i + off + 1) if 0 <= j < len(new)]
        if not js:
            continue
        diffs = [np.abs(new[j] - old[i]) for j in js]
        best = min(diffs, key=lambda x: x.mean())
        d[i] = best.mean()
        t = best[:TH - TH % 8, :TW - TW % 8]
        tile[i] = t.reshape(t.shape[0] // 8, 8, t.shape[1] // 8, 8).mean(axis=(1, 3)).max()
    md, mt = np.nanmedian(d), np.nanmedian(tile)
    print(f"median frame difference {md:.2f}, median worst-tile difference {mt:.1f}")
    bad = np.where((d > 2 * md) | (tile > max(40, 3 * mt)))[0]
    runs = []
    for i in bad:
        if runs and i - runs[-1][-1] <= 2:
            runs[-1].append(int(i))
        else:
            runs.append([int(i)])
    for r in runs:
        pk = max(r, key=lambda k: tile[k])
        print(f"  old {r[0]}-{r[-1]} ({len(r)} fr) = new {r[0] + off}-{r[-1] + off}: "
              f"diff {d[pk]:.1f}, worst tile {tile[pk]:.0f}")
    if not runs:
        print("  no differing runs")


if __name__ == "__main__":
    main()
