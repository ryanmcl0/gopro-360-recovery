# Case study: a GoPro MAX `.360` lost to a formatted card

A 28-second GoPro MAX recording (a rooftop at sunset) was on a 16 GB microSD card that
was accidentally formatted. Nothing was written to the card afterwards. About four
years later it was imaged and recovered.

## What the first attempts produced

1. **Imaging:** `ddrescue` read the whole card with **0 errors**. The data was intact.
2. **Carving:** PhotoRec found the file but mis-assembled it. A contiguous `dd` carve
   from the file's start gave a 360 MB blob with no `moov` ("moov atom not found").
3. **untrunc** (`-s`, with a healthy `.360` from the same camera as reference) made it
   play, but put all 3,483 frames of **both lenses into one track**. Decoded in order,
   each frame predicts from the other lens's picture: garbled motion.
4. **De-interleaving** the merged track by picture-order count split it back into two
   lenses (1664 + 1649 frames). The first second was missing, two GOPs failed to decode
   (two ~0.65 s freezes), and short bursts of **magenta, checkerboard and blown-out
   white** remained. These were patched by holding the nearest clean frame.

The result was watchable, but still had sporadic glitches: white flashes and purple
patches that were only obvious frame by frame.

## The actual cause

A clean read and yet sporadic damage pointed at the carve rather than the data. The
card also held three recordings that had good copies elsewhere. Mapping one of them
(`GS010035.360`) onto the card image cluster by cluster showed:

```
file clusters    0– 335  → image clusters    0– 335   (contiguous)
file clusters  336– 751  → image clusters  352– 767   (16-cluster gap)
file clusters  752–1135  → image clusters  784–1167   (16-cluster gap)
… 26 runs in all, every run a multiple of 16 clusters, every gap exactly 16 clusters
```

The 16-cluster (512 KiB) gaps hold the `.LRV` proxy, which the camera writes alongside
the `.360`. The contiguous carve had spliced **19 chunks of proxy data** into the lost
recording. Every frame that crossed one broke, and the damage carried on to the next
keyframe. That accounted for all of it: the flashes, the magenta, the checkerboard,
the freezes and the missing start.

## The camera's own index was still there

The lost file's `mdat` header held its true size, 236,590,355 bytes, so the camera had
finalised it. The `moov` (122,827 bytes) was found 19 × 512 KiB past where a contiguous
read would expect it. It was intact, and held the full sample tables for both lenses
(1707 + 1706 frames, 29 keyframes each), AAC and 4-channel ambisonic audio, a timecode
track, GPMF telemetry (gyro, accelerometer, gravity, orientation, GPS) and the
recording's own lens calibration.

Walking the file in 512 KiB units and checking every frame's NAL chain against the
index placed all 19 gaps. That number has to match the index's shift, and it did.

## Results

| | previous best | rebuilt from the card |
|---|---|---|
| frames (front + back lens) | 1652 + 1652 (re-encoded, holds) | **1707 + 1706**, original |
| decode errors | 224 + 161 concealed | **0** |
| first second | missing | present |
| ~9 s and ~15 s | 0.65 s freezes | real motion |
| bursts at 1.5 s, 2 s, 3.4 s, 5 s, 7.8 s, 13 s, 20.8 s, 22.7 s, 28 s | held frames or visible damage | clean |
| telemetry and calibration | borrowed from another clip | the clip's own |

The method was checked by rebuilding all three recordings with good copies from the
same image: **md5 identical** for each.

## Takeaways

- A clean `ddrescue` read plus "random" corruption in a carved video usually means
  fragmentation, not bad data.
- GoPro writes the main file and the `.LRV` at the same time. On FAT32 that interleaves
  them in fixed allocation units.
- A finalised GoPro file's `moov` survives a quick format. It's just not where a
  contiguous carve looks.
- Patching symptoms (frame holds, interpolation) can only hide damage. Undoing the
  fragmentation removes it.
