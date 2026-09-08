#!/usr/bin/env python3
"""Mux one video with several extra audio tracks into a single playable MP4.

The base video keeps its own audio as track 0 (the default). Every extra audio
file becomes an additional audio track with language metadata, so players and
platforms that support multi-audio (YouTube, VLC, etc.) let the viewer switch.

All tracks are padded/trimmed to the longest duration; the video is extended by
freezing the last frame if an extra narration is longer than the base video.

Usage:
  python make_multitrack.py \
      --video final-1.mp4 \
      --track1-code hin --track1-label "Hindi" \
      --add "eng|English|/path/eng_voice.mp3" \
      --out final-multitrack.mp4
"""
import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if result.returncode != 0:
        print(result.stdout[-3000:], file=sys.stderr)
        raise SystemExit(f"command failed with exit code {result.returncode}")
    return result


def ffprobe(path, entries, select="a:0"):
    return subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", select,
         "-show_entries", entries, "-of", "csv=p=0", str(path)],
        text=True,
    ).strip()


def probe_duration(path, stream):
    """Duration of a file/stream in seconds; falls back to container duration."""
    try:
        val = ffprobe(path, "stream=duration", select=stream)
        if val and float(val) > 0:
            return float(val)
    except Exception:
        pass
    return float(ffprobe(path, "format=duration", select="") or 0)


def validate(out_file, expected_audio):
    pf = ffprobe(out_file, "stream=pix_fmt", select="v:0")
    if pf != "yuv420p":
        raise SystemExit(f"validation failed: expected yuv420p, got {pf}")
    audio_count = ffprobe(out_file, "stream=index", select="a").count("\n") + 1 if ffprobe(out_file, "stream=index", select="a") else 0
    if audio_count != expected_audio:
        raise SystemExit(f"validation failed: expected {expected_audio} audio streams, found {audio_count}")
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(out_file), "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        raise SystemExit("validation failed: output cannot be decoded: " + (result.stderr.strip().splitlines()[-1] if result.stderr.strip() else ""))
    print(f"validation passed: pix_fmt={pf}, audio_streams={audio_count}, decode clean")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="base video (video + default audio track)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--track1-code", default="und", help="language code of the base audio (e.g. hin)")
    parser.add_argument("--track1-label", default="", help="display label of the base audio (e.g. Hindi)")
    parser.add_argument("--add", action="append", default=[],
                        help="extra audio as code|label|path (repeatable)")
    args = parser.parse_args()

    base = Path(args.video).resolve()
    if not base.exists():
        raise SystemExit(f"video not found: {base}")
    out = Path(args.out).resolve()

    adds = []
    for spec in args.add:
        parts = spec.split("|", 2)
        if len(parts) != 3:
            raise SystemExit(f"bad --add spec (want code|label|path): {spec}")
        code, label, path = parts
        p = Path(path).resolve()
        if not p.exists():
            raise SystemExit(f"audio file not found: {p}")
        adds.append((code, label, p))

    base_dur = probe_duration(base, "v:0")
    print(f"base video: {base_dur:.2f}s")
    add_durs = []
    for code, label, p in adds:
        dur = probe_duration(p, "a:0")
        add_durs.append(dur)
        print(f"extra audio {code} ({label}): {dur:.2f}s -> {p.name}")

    # every track must span the whole video
    total = max([base_dur] + [d + 0.3 for d in add_durs])
    extra = max(0.0, total - base_dur)

    n_add = len(adds)
    inputs = ["-i", str(base)]
    for _, _, p in adds:
        inputs += ["-i", str(p)]

    parts = []
    parts.append(
        f"[0:v]format=yuv420p,setsar=1,tpad=stop_mode=clone:stop_duration={extra:.3f},"
        f"settb=AVTB[v]"
    )
    parts.append(
        f"[0:a:0]aformat=sample_rates=44100:channel_layouts=stereo,"
        f"apad=whole_dur={total:.3f},atrim=0:{total:.3f},asetpts=N/SR/TB[a0]"
    )
    for i in range(n_add):
        parts.append(
            f"[{i + 1}:a:0]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"apad=whole_dur={total:.3f},atrim=0:{total:.3f},asetpts=N/SR/TB[a{i + 1}]"
        )

    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(parts)]
    cmd += ["-map", "[v]", "-map", "[a0]"]
    for i in range(n_add):
        cmd += ["-map", f"[a{i + 1}]"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-metadata:s:a:0", f"language={args.track1_code}"]
    if args.track1_label:
        cmd += ["-metadata:s:a:0", f"title={args.track1_label}"]
    cmd += ["-disposition:a:0", "default"]
    for i, (code, label, _) in enumerate(adds):
        cmd += ["-metadata:s:a:{i}".format(i=i + 1), f"language={code}"]
        if label:
            cmd += ["-metadata:s:a:{i}".format(i=i + 1), f"title={label}"]
    cmd += ["-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]

    out.parent.mkdir(parents=True, exist_ok=True)
    run(cmd)
    validate(out, expected_audio=1 + n_add)
    print(f"done: {out} (total {total:.2f}s, {1 + n_add} audio tracks)")


if __name__ == "__main__":
    main()
