# gopro-360-recovery

Recover GoPro videos, including GoPro MAX `.360` files, from a formatted or "corrupted"
SD card, **byte for byte as the camera wrote them**.

Most "corrupted" GoPro recoveries aren't damaged data. The camera writes two files at
once: the main video and a low-resolution `.LRV` proxy. On the card their clusters
interleave. Once the card is formatted (or the file table is lost), carving tools read
the main file as one contiguous block and splice chunks of the proxy into the video.
This tool works out exactly which chunks belong to the video, skips the rest, and
rebuilds the original file, including its own index, telemetry and lens calibration.

## Does this match your problem?

You recovered a GoPro file (PhotoRec, Disk Drill, `dd`, …) and:

- it won't open: **"moov atom not found"**, or
- it plays, but with sporadic **white flashes, magenta/purple or green patches,
  checkerboard blocks**, smeared or ghosted motion, or **freezes**, starting about
  every 10–13 MB of file, or
- `untrunc` "fixes" a `.360`, but the two lenses end up **merged into one track**,
  so motion is garbled and the start is missing.

If the camera **finished** the recording (you pressed stop; the battery didn't die
mid-clip), its original index is very likely still on the card, and this tool can
give you the file back exactly.

## Requirements

- Python 3.8+ (the recovery itself uses only the standard library)
- `ffmpeg` / `ffprobe`, optional, for the decode check (`--verify`)
- for the inspection tools in `tools/`: `pip install -r requirements-tools.txt`
  (numpy, Pillow)

## 1. Image the card first. Never work on the card itself

Stop using the card. Don't let anything write to it. Make a raw image and work only on
that image:

```bash
# macOS: find the card (e.g. disk4), unmount it (don't eject), image the raw device
diskutil list
diskutil unmountDisk /dev/disk4
sudo ddrescue -r3 /dev/rdisk4 card.img card.map      # brew install ddrescue

# Linux
sudo ddrescue -r3 /dev/sdX card.img card.map
```

## 2. Scan

```bash
python3 gopro_recover.py scan card.img
```

```
FAT32 volume: 32 KiB clusters, data region at byte 8388608
9 file start(s) found

      22773760  360 2022-04-28 15:57:19    30.0s  [hvc1 2272x736 1797 fr, hvc1 2272x736 1796 fr]  moov 10.0 MiB past the mdat end
      31686656  LRV 2022-04-28 15:57:19    30.0s  [avc1 1408x704 1796 fr]  moov 228.9 MiB past the mdat end
     ...
     992935936  360 2022-05-01 06:50:30    28.5s  [hvc1 2272x736 1707 fr, hvc1 2272x736 1706 fr]  moov 9.5 MiB past the mdat end
```

Each line is a recording the camera finalised: its byte offset in the image, its type,
the camera's timestamp, its length and tracks. "moov … past the mdat end" means the
file is fragmented: its index sits that far beyond where a contiguous carve would look.

## 3. Recover

```bash
python3 gopro_recover.py recover card.img --out recovered --verify
python3 gopro_recover.py recover card.img --offset 992935936 --verify   # just one file
```

| option | |
|---|---|
| `--offset N` | rebuild only the file whose `ftyp` box starts at byte N (repeatable) |
| `--out DIR` | output folder (default `recovered/`) |
| `--verify` | decode every video stream with ffmpeg and report frame and error counts |
| `--include-lrv` | also rebuild the `.LRV` proxies (useful if the main file is gone) |
| `--expect FILE` | compare the result's md5 with a known-good copy (see below) |
| `--unit BYTES` | force the allocation unit instead of auto-detecting it |
| `--max-shift BYTES` | how far past the mdat to look for the index (default 2 GiB) |

Output files are named `GOPRO_<type>_<camera time>_at<offset>.<ext>`. Exit status:
0 = success, 1 = an index was found but the file couldn't be mapped, 2 = a rebuilt
file failed the decode check.

A `.360` comes back as the camera's own dual-track file. Open it in **GoPro Player**
or **GoPro Quik**, which stitch it using the clip's own calibration and telemetry, or
export it to equirectangular from there.

### Prove it on your own card first

If the same card also holds recordings you still have good copies of, rebuild one of
those and compare:

```bash
python3 gopro_recover.py recover card.img --offset 334397440 --expect GS010035.360
#   expected md5 c2e6073f…: MATCH
```

A `MATCH` means the method reproduces that file exactly from the card image, so the
lost one rebuilt the same way can be trusted the same way.

## How it works

```
what the card holds after formatting (FAT32, 32 KiB clusters, GoPro MAX):

  [ .360 ×23 units ][LRV][ .360 ×25 ][LRV][ .360 ×22 ][LRV] … [ .360 tail + moov ][LRV moov]
    1 unit = 512 KiB = 16 clusters                 the camera's index ──┘
```

1. **Find the files.** Every recording starts with an `ftyp` box on a cluster boundary.
   The filesystem's cluster size is read from the FAT32/exFAT boot sector.
2. **Find the index.** A finalised recording's `mdat` header holds the exact length of
   the video data. The `moov` (every track's sample table) follows it in the file, but
   on the card it sits further on by exactly the size of the foreign chunks in between,
   so it's searched for at `mdat end + k × alignment` and must pass strict checks
   (well-formed box tree; every sample inside the `mdat`; no two samples overlapping).
3. **Place the gaps.** The file is walked one allocation unit at a time. Each video
   sample is a chain of length-prefixed NAL units whose lengths must add up **exactly**
   to the sample size the index records, and every NAL header must be well-formed. A
   unit that breaks a chain is foreign: skip ahead until the chain holds again. The
   number of skipped units must equal the index's shift exactly. The largest allocation
   unit that works is used (4 MiB down to one cluster), so gaps of any length are
   handled, as is an index that is itself split by foreign data.
4. **Rebuild.** Copy the units in order. Nothing is re-encoded, guessed or patched.

When a gap boundary falls in a stretch with no NAL header to test (only likely with
very small allocation units), its exact position can't be proven. The tool says so,
decode-tests the result automatically, and exits with status 2 if the decoder
complains.

## When it won't work

- **The recording wasn't finalised.** If the battery died, the card was pulled or the
  camera crashed mid-recording, the `mdat` size is 0 and there's no `moov`. `scan`
  reports it as "not finalised". The fallback is
  [untrunc](https://github.com/anthwlock/untrunc) with a healthy file from the same
  camera as reference. Be aware that for `.360` files it merges the two lenses into one
  track, and it can't see the LRV chunks, so the result will still have glitches.
- **The data was overwritten.** Anything recorded over the lost file's clusters is gone.
- **Other layouts.** This was built and validated on a GoPro MAX with a FAT32 card. The
  method is generic (it reads the cluster size and finds the allocation unit itself, and
  handles HEVC and H.264), but other cameras and exFAT cards haven't been tested. Use
  `--verify`, and `--expect` with a known-good file from the same card.

## Inspecting the result

`tools/` has what was used to check the footage frame by frame. They need numpy and
Pillow.

```bash
# every frame scored for checkerboard blocks, magenta/purple chroma, flashes and outliers
python3 tools/frame_scan.py recovered/GOPRO_360_….360 --stream 0

# two recoveries of the same clip: finds the frame offset, lists where they differ
python3 tools/compare_versions.py old_attempt.360 recovered/GOPRO_360_….360

# look at exact frames, two versions one above the other
python3 tools/contact_sheet.py cmp.png --row OLD old.360 0 31,38,45 --row NEW new.360 0 91,98,105
```

Fast pans, a swinging selfie stick and sun glare also spike the outlier and flash
metrics. Look at the frames before calling anything damage.

## Tests

```bash
python3 tests/test_synthetic.py        # needs ffmpeg with libx265 and libx264
```

The tests build fragmented card images from synthetic videos (a two-track HEVC "360"
file interleaved with real H.264 data as the foreign chunks) and require a byte-exact
rebuild:

- a FAT32 volume with 512 KiB allocation units
- a raw image with no filesystem, where the 64 KiB unit has to be auto-detected
- an index split by foreign data, at 8 KiB granularity
- an unfinalised file, which must be reported rather than guessed at

## Validation

On a real 16 GB GoPro MAX card, formatted after recording:

| file | foreign units skipped | result |
|---|---|---|
| `GS010025.360` (30.0 s) | 20 × 512 KiB | md5 **MATCH** with the good copy |
| `GS010035.360` (38.5 s) | 25 × 512 KiB | md5 **MATCH** with the good copy |
| `GS010036.360` (32.0 s) | 21 × 512 KiB | md5 **MATCH** with the good copy |
| lost recording (28.5 s) | 19 × 512 KiB | both lenses 1707 + 1706 frames, **0 decode errors** |
| its `.LRV` proxy | 857 units in 19 gaps (irregular) | 1706 frames, 0 decode errors |

[docs/case-study.md](docs/case-study.md) tells the full story of that recovery.

## Layout

```
gopro_recover.py         scan / recover / verify (standard library only)
mp4meta.py               MP4 box and sample-table reader
tests/test_synthetic.py  fragmented-image tests
tools/                   frame_scan, compare_versions, contact_sheet (numpy, Pillow)
docs/case-study.md       the recovery this was built for
```
