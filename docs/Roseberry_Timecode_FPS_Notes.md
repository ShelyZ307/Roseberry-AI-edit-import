# Roseberry Timecode / FPS Notes

## Editor Summary

JSON times describe where a segment comes from in the original source episode. The generated edited timeline may place that segment somewhere else because the tool currently adds review gaps between segments.

## Source Time vs Timeline Time

Source Time means where the segment comes from in the original source episode.

Timeline Time means where that segment is placed in the generated edited timeline.

These can differ because the generated edited timeline currently includes 5-second review gaps.

## Source-Relative Time

Preferred JSON starts at:

```text
00:00:00.000
```

If input starts at `01:00:00`, the script can normalize it only for `Create Edited Timeline` when safe.

## FPS / Frame Conversion

DaVinci cuts on frames. AI timestamps may use milliseconds.

The current conversion rule is:

```python
source_frame = source_timeline_start_frame + int(seconds * fps + 0.5)
```

Small differences of a few milliseconds or one frame can be normal, especially at 23.976 fps.

## Exclusive Out

Segment timing is:

```text
start_time inclusive
end_time exclusive
```

One segment can end at the same timestamp where the next segment begins.

DaVinci may display the last visible frame one frame before the exclusive out boundary.

