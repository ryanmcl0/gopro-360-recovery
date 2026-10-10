#!/usr/bin/env python3
"""Contact sheet of exact frames, in colour, for checking damage by eye.

    contact_sheet.py out.png --row LABEL FILE STREAM FRAMES [--row ...] [--width 480]

FRAMES is a comma list of frame numbers and ranges, e.g. 120,125,130-140:2.
Put rows from two recoveries one above the other to compare the same moments:

    contact_sheet.py cmp.png --row OLD old.360 0 31,38,45 --row NEW new.360 0 91,98,105

Needs ffmpeg, numpy and Pillow.
"""
import argparse
import json
import subprocess

import numpy as np
from PIL import Image, ImageDraw


def frames_arg(s):
    out = []
    for part in s.split(","):
        if "-" in part:
            rng, _, step = part.partition(":")
            a, b = (int(x) for x in rng.split("-"))
            out += list(range(a, b + 1, int(step or 1)))
        else:
            out.append(int(part))
    return out


def grab(path, stream, idxs):
    q = subprocess.run(["ffprobe", "-v", "error", "-select_streams", str(stream), "-show_entries",
                        "stream=width,height", "-of", "json", path], capture_output=True, text=True,
                       check=True)
    s = json.loads(q.stdout)["streams"][0]
    w, h = s["width"], s["height"]
    sel = "+".join(f"eq(n\\,{i})" for i in sorted(set(idxs)))
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", f"0:{stream}",
                        "-vf", f"select='{sel}'", "-fps_mode", "passthrough", "-pix_fmt", "rgb24",
                        "-f", "rawvideo", "-"], capture_output=True, check=True)
    arr = np.frombuffer(p.stdout, np.uint8).reshape(-1, h, w, 3)
    return dict(zip(sorted(set(idxs)), arr)), w, h


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--row", nargs=4, action="append", required=True,
                    metavar=("LABEL", "FILE", "STREAM", "FRAMES"))
    ap.add_argument("--width", type=int, default=480, help="tile width in pixels")
    a = ap.parse_args()
    rows = [(lab, f, int(st), frames_arg(fr)) for lab, f, st, fr in a.row]
    grabbed = [grab(f, st, idx) for _, f, st, idx in rows]
    tw = a.width
    th = max(int(tw * h / w) for _, w, h in grabbed)
    ncol = max(len(r[3]) for r in rows)
    img = Image.new("RGB", (ncol * tw, len(rows) * (th + 18)), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    for ri, ((lab, _, _, idxs), (frames, w, h)) in enumerate(zip(rows, grabbed)):
        y = ri * (th + 18)
        for ci, i in enumerate(idxs):
            if i not in frames:
                continue
            tile = Image.fromarray(frames[i]).resize((tw, int(tw * h / w)), Image.LANCZOS)
            img.paste(tile, (ci * tw, y + 18))
            draw.text((ci * tw + 4, y + 3), f"{lab} #{i}", fill=(255, 255, 0))
    img.save(a.out)
    print(a.out, img.size)


if __name__ == "__main__":
    main()
