#!/usr/bin/env python3
"""Scan every frame of a video stream for the kinds of damage a bad recovery shows,
and list the frames that spike above their neighbours.

    frame_scan.py <video> [--stream N] [--save metrics.npz]

Metrics (per frame):
  checker   multi-scale checkerboard score: adjacent-pixel differences that exceed
            same-phase differences. Clean footage sits ~2; block corruption > 3.5.
  chroma    99.5th percentile chroma magnitude: magenta / purple / green patches.
  outlier   max over 16x16 tiles of |frame - mean(prev, next)| - |prev - next| / 2:
            a patch that differs from both neighbours while they agree with each other.
  flash     max over tiles of how much brighter a tile is than in both neighbours.

A frame is reported when a metric jumps well above the rolling median of the
surrounding 41 frames (sustained texture, like a mesh fence, is not a spike).
Fast pans, a swinging selfie stick or sun glare also spike `outlier`/`flash`:
always look at the frames (tools/contact_sheet.py) before calling them damage.
Needs ffmpeg/ffprobe and numpy.
"""
import argparse
import json
import subprocess

import numpy as np

FLOORS = {"checker": (1.0, 3.5), "chroma": (15, 40), "outlier": (20, 30), "flash": (20, 30)}


def probe(path, stream):
    q = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(stream), "-show_entries",
                        "stream=width,height,avg_frame_rate", "-of", "json", path],
                       capture_output=True, text=True, check=True)
    s = json.loads(q.stdout)["streams"][0]
    num, den = (int(x) for x in s["avg_frame_rate"].split("/"))
    return s["width"], s["height"], (num / den if den else 30.0)


def checker(y):
    h, w = y.shape
    total = 0.0
    for s in (1, 2, 4):
        a = np.abs(y[:, s:] - y[:, :-s])[:, :w - 2 * s]
        f = np.abs(y[:, 2 * s:] - y[:, :-2 * s])
        total += np.maximum(0, a - f).sum()
        a = np.abs(y[s:, :] - y[:-s, :])[:h - 2 * s]
        f = np.abs(y[2 * s:, :] - y[:-2 * s, :])
        total += np.maximum(0, a - f).sum()
    return total / (w * h)


def scan(path, stream):
    w, h, fps = probe(path, stream)
    w2, h2 = w - w % 16, h - h % 16
    cw, ch = w // 2, h // 2
    ysz, csz = w * h, cw * ch
    fsz = ysz + 2 * csz
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{stream}",
                          "-pix_fmt", "yuv420p", "-f", "rawvideo", "-"], stdout=subprocess.PIPE)
    cb, chroma, tiles = [], [], []
    while True:
        buf = p.stdout.read(fsz)
        if len(buf) < fsz:
            break
        y = np.frombuffer(buf[:ysz], np.uint8).reshape(h, w).astype(np.float32)
        u = np.frombuffer(buf[ysz:ysz + csz], np.uint8).reshape(ch, cw).astype(np.float32)
        v = np.frombuffer(buf[ysz + csz:], np.uint8).reshape(ch, cw).astype(np.float32)
        cb.append(checker(y))
        chroma.append(np.percentile(np.sqrt((u - 128) ** 2 + (v - 128) ** 2), 99.5))
        tiles.append(y[:h2, :w2].reshape(h2 // 16, 16, w2 // 16, 16).mean(axis=(1, 3)))
    p.wait()
    n = len(tiles)
    outlier, flash = np.zeros(n), np.zeros(n)
    for i in range(1, n - 1):
        a, b, c = tiles[i - 1], tiles[i], tiles[i + 1]
        outlier[i] = (np.abs(b - (a + c) / 2) - np.abs(a - c) / 2).max()
        flash[i] = (b - np.maximum(a, c)).max()
    return fps, dict(checker=np.array(cb), chroma=np.array(chroma), outlier=outlier, flash=flash)


def rolling_median(x, w=20):
    return np.array([np.median(x[max(0, i - w):i + w + 1]) for i in range(len(x))])


def spikes(x, rise, floor):
    base = rolling_median(x)
    idx = np.where((x - base > rise) & (x > floor))[0]
    runs = []
    for i in idx:
        if runs and i - runs[-1][-1] <= 4:
            runs[-1].append(int(i))
        else:
            runs.append([int(i)])
    return runs, base


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("video")
    ap.add_argument("--stream", type=int, default=0)
    ap.add_argument("--save", help="write per-frame metrics to this .npz")
    a = ap.parse_args()
    fps, m = scan(a.video, a.stream)
    n = len(m["checker"])
    print(f"{a.video} stream {a.stream}: {n} frames")
    if a.save:
        np.savez(a.save, **m)
    for k, (rise, floor) in FLOORS.items():
        runs, base = spikes(m[k], rise, floor)
        print(f"  {k:8s} median {np.median(m[k]):7.2f}  max {m[k].max():7.2f}@{int(m[k].argmax())}  "
              f"-> {len(runs)} spike run(s)")
        for r in runs:
            pk = max(r, key=lambda j: m[k][j])
            print(f"      frames {r[0]}-{r[-1]}  t={r[0] / fps:6.2f}s  peak {m[k][pk]:.1f}@{pk} "
                  f"(local median {base[pk]:.1f})")


if __name__ == "__main__":
    main()
