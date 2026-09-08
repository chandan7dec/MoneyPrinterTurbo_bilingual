#!/usr/bin/env python3
"""Add an animated, narrated intro card and a 'Thanks for watching' outro card
to a generated MoneyPrinterTurbo video.

The card size and frame rate are read from the main video itself, so this works
for 720p, 1080p, or any other output without resizing the main content.

When the title is written in Hindi (Devanagari), the cards automatically use the
bundled Noto Sans Devanagari font and a Hindi 'thanks for watching' outro.

Usage:
  python add_intro_outro.py --video <final-1.mp4> --title "Topic Title" \
      [--voice en-US-JennyNeural] [--out output.mp4] \
      [--outro-text "Thanks for watching!|Subscribe for more"]
"""
import argparse
import asyncio
import io
import random
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

FONT_DIR = Path.home() / "MoneyPrinterTurbo" / "resource" / "fonts"
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
CROSSFADE = 0.6

_SUPPORTED_CARD_CODECS = (
    "libx264", "h264_nvenc", "h264_amf", "h264_qsv", "h264_mf", "h264_videotoolbox",
)


def pick_video_codec() -> str:
    """Video encoder from config.toml [app] video_codec (default libx264)."""
    codec = "libx264"
    try:
        import tomllib
        cfg = Path(__file__).resolve().parents[1] / "config.toml"
        value = tomllib.loads(cfg.read_text(encoding="utf-8")).get("app", {}).get("video_codec")
        codec = str(value or "libx264").strip()
    except Exception:
        pass
    return codec if codec in _SUPPORTED_CARD_CODECS else "libx264"


def encode_args_for(codec: str) -> list[str]:
    if codec == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", "22"]
    if codec == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-cq", "22"]
    if codec == "h264_amf":
        return ["-c:v", "h264_amf", "-qp_i", "22", "-qp_p", "22"]
    if codec == "libx264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]
    return ["-c:v", codec]


def has_devanagari(text: str) -> bool:
    """True when the text contains any Devanagari (Hindi) character."""
    return bool(DEVANAGARI_RE.search(text or ""))


def run(cmd, log_path=None):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    log = open(log_path, "w", encoding="utf-8") if log_path else subprocess.DEVNULL
    try:
        result = subprocess.run(cmd, stdout=log if log_path else None, stderr=subprocess.STDOUT)
    finally:
        if log_path:
            log.close()
    if result.returncode != 0 and log_path:
        lines = open(log_path, encoding="utf-8", errors="replace").read().splitlines()
        print("\n".join(lines[-25:]))
    result.check_returncode()
    return result


def _probe(path, entries):
    out = subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", entries, "-of", "csv=p=0", str(path),
        ],
        text=True,
    ).strip()
    return out


def probe_video(path):
    """Return (width, height, fps, duration) of the main video stream."""
    wh = _probe(path, "stream=width,height").split(",")
    width, height = int(wh[0]), int(wh[1])
    rate_str = _probe(path, "stream=avg_frame_rate")
    fps = 25
    if rate_str and "/" in rate_str:
        num, den = rate_str.split("/")
        if den and float(den) > 0:
            fps = float(num) / float(den)
    fps = max(1, int(round(fps)))
    duration = float(_probe(path, "format=duration") or 0)
    return width, height, fps, duration


def make_gradient(size, top, bottom):
    img = Image.new("RGB", size)
    draw = ImageDraw.Draw(img)
    w, h = size
    for y in range(h):
        t = y / (h - 1)
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=color)
    return img


def _soft_random_rgb(rng):
    """Return a soft, silk, light RGB tuple from a random source.

    Avoids deep saturated single colors. Favors pastel/light mixes with
    gentle hue variation and higher lightness so white card text stays readable.
    """
    # Pick a soft hue region, then mix toward white/gray for silkiness.
    hue_angle = rng.random() * 360.0
    # Two-tone mix: primary soft color + a neighboring soft tint.
    def _hue_rgb(h, saturation, lightness):
        saturation /= 100.0
        lightness /= 100.0
        c = (1 - abs(2 * lightness - 1)) * saturation
        x = c * (1 - abs((h / 60.0) % 2 - 1))
        m = lightness - c / 2.0
        h = h % 360.0
        if h < 60:
            r, g, b = c, x, 0.0
        elif h < 120:
            r, g, b = x, c, 0.0
        elif h < 180:
            r, g, b = 0.0, c, x
        elif h < 240:
            r, g, b = 0.0, x, c
        elif h < 300:
            r, g, b = x, 0.0, c
        else:
            r, g, b = c, 0.0, x
        return tuple(int((v + m) * 255) for v in (r, g, b))

    base = _hue_rgb(hue_angle, rng.randint(28, 55), rng.randint(72, 90))
    tint = _hue_rgb((hue_angle + rng.choice((35, 45, 60, 90, 120)) + rng.randint(-20, 20)) % 360, 30, 88)
    # Blend base with tint and a touch of warm/cool gray for a mixed, non-flat look.
    mix = rng.random()
    blended = tuple(int(base[i] * (1 - mix) + tint[i] * mix) for i in range(3))
    gray = (rng.randint(225, 250), rng.randint(228, 252), rng.randint(225, 250))
    soft = tuple(int(blended[i] * 0.75 + gray[i] * 0.25) for i in range(3))
    return soft


def generate_soft_palette(rng):
    """Return (top, bottom, accent) with a soft, light, gradient, mixed feel.

    ``top`` and ``bottom`` are two related soft tones so the vertical gradient
    reads as a gentle silk gradient rather than a flat wash; ``accent`` is a
    slightly more saturated sibling kept light enough for white text contrast.
    """
    top_base = _soft_random_rgb(rng)
    bottom_base = _soft_random_rgb(rng)
    # Keep top and bottom close enough to feel like one soft gradient.
    def _lerp(a, b, t):
        return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

    # Ensure top is the lighter end and bottom a touch deeper for a calm gradient.
    top = tuple(min(255, int(v + rng.randint(6, 22))) for v in top_base)
    bottom = tuple(max(0, int(v - rng.randint(4, 26))) for v in bottom_base)
    # If bottom got darker than top, gently swap roles.
    if sum(bottom) < sum(top):
        top, bottom = bottom, top
    accent = _soft_random_rgb(rng)
    # Accent should pop a little more but still stay soft; nudge saturation up.
    accent = tuple(min(255, int(v + rng.randint(12, 40))) for v in accent)
    return top, bottom, accent


def _hsl_to_rgb(hue, saturation, lightness):
    """Convert HSL (hue in degrees, s/l in 0..1) to an sRGB tuple."""
    hue = hue % 360.0
    chroma = (1.0 - abs(2.0 * lightness - 1.0)) * saturation
    sector = hue / 60.0
    x = chroma * (1.0 - abs(sector % 2.0 - 1.0))
    m = lightness - chroma / 2.0
    if sector < 1:
        r, g, b = chroma, x, 0.0
    elif sector < 2:
        r, g, b = x, chroma, 0.0
    elif sector < 3:
        r, g, b = 0.0, chroma, x
    elif sector < 4:
        r, g, b = 0.0, x, chroma
    elif sector < 5:
        r, g, b = x, 0.0, chroma
    else:
        r, g, b = chroma, 0.0, x
    return tuple(int((v + m) * 255) for v in (r, g, b))


def generate_silk_palette(rng):
    """Return a fresh, eye-catching soft-silk gradient (top, bottom, accent).

    ``auto`` samples the footage and blends it hard toward near-white, which is
    why cards from different videos end up looking almost the same. This mode
    draws a NEW soft-silk gradient for every run instead: a light airy top tone,
    a deeper still-soft sibling for the bottom, and a livelier accent for the
    bar/glow. Different videos therefore get clearly different, gentle-but-vivid
    card colors while white title text stays readable.
    """
    base_hue = rng.uniform(0.0, 360.0)
    # Top: airy, light silk tone from the base hue family.
    top = _hsl_to_rgb(base_hue, rng.uniform(0.45, 0.62), rng.uniform(0.80, 0.92))
    # Bottom: a deeper, still-soft sibling tone for a silky vertical gradient.
    bottom = _hsl_to_rgb(
        (base_hue + rng.uniform(-24.0, 24.0)) % 360.0,
        rng.uniform(0.58, 0.80),
        rng.uniform(0.52, 0.66),
    )
    # Accent: a livelier pop on an analogous/complementary drift.
    accent = _hsl_to_rgb(
        (base_hue + rng.choice((35.0, 60.0, 120.0, 165.0, 200.0, 245.0, 300.0)))
        % 360.0,
        rng.uniform(0.72, 0.92),
        rng.uniform(0.56, 0.76),
    )
    return top, bottom, accent


def _luma(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def _darken(c, f=0.5):
    return tuple(max(0, int(v * f)) for v in c)


def _brighten(c, f=1.5):
    return tuple(min(255, int(v * f)) for v in c)


# Curated palettes used when the footage palette can't be extracted or
# --palette random is requested. Kept intentionally varied: some darker for
# high contrast, some soft/silk/light so cards never look identical.
_RANDOM_PALETTES = [
    ((18, 20, 52), (78, 40, 130), (255, 170, 90)),    # night purple + amber
    ((10, 30, 45), (25, 100, 110), (255, 200, 90)),   # deep teal + gold
    ((40, 8, 30), (130, 30, 70), (255, 140, 120)),    # berry + coral
    ((20, 34, 8), (70, 110, 30), (220, 230, 90)),     # forest + lime
    ((30, 20, 50), (80, 60, 130), (190, 160, 255)),   # violet + lavender
    ((8, 8, 8), (60, 55, 90), (255, 120, 120)),       # charcoal + soft red
    ((12, 22, 46), (34, 62, 140), (255, 190, 40)),    # classic blue + amber
    # Soft / silk / light palettes for a lighter, mixed feel.
    ((232, 224, 240), (214, 206, 228), (196, 150, 218)),  # soft lavender silk
    ((240, 232, 224), (226, 210, 196), (212, 168, 128)),  # warm beige silk
    ((224, 236, 232), (200, 224, 218), (140, 184, 188)),  # soft aqua mist
    ((240, 236, 244), (226, 214, 232), (206, 156, 196)),  # blush rose silk
    ((232, 240, 228), (214, 228, 208), (156, 196, 140)),  # sage silk
    ((244, 238, 224), (232, 216, 190), (232, 176, 92)),   # honey cream
    ((236, 224, 236), (218, 202, 220), (176, 138, 200)),  # lilac haze
    ((230, 240, 244), (206, 224, 236), (138, 188, 214)),  # sky silk
    ((244, 236, 228), (230, 214, 196), (214, 148, 132)),  # peach silk
    ((236, 228, 244), (216, 206, 232), (168, 140, 214)),  # wisteria mist
]


def extract_footage_palette(video_path, n_frames=4):
    """Sample key frames from the footage and return (top, bottom, accent).

    Frames are pulled mid-video (not the first second, which may be a title plate),
    downscaled hard, then median-cut quantized. Returns None on any failure so
    the caller can fall back to a curated palette.
    """
    try:
        rng = random.Random()
        duration = float(_probe(video_path, "format=duration") or 0)
        if duration <= 1.5:
            return None
        times = [duration * f for f in (0.12, 0.38, 0.62, 0.85)]
        pixels = []
        for t in times:
            raw = subprocess.check_output(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video_path),
                    "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
                ]
            )
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img = img.resize((40, 50), Image.BILINEAR)  # tiny sample = fast
            pixels.extend(img.getdata())
        if len(pixels) < 5:
            return None
        sample = Image.new("RGB", (len(pixels), 1))
        sample.putdata(pixels)
        pal_img = sample.quantize(colors=6, method=Image.MEDIANCUT).convert("RGB")
        palette = list(pal_img.getdata())
        palette.sort(key=_luma)
        lightest = palette[-1]
        # Build a soft, light, gradient-friendly card from the footage tones.
        # Keep the background relatively light/silk so cards vary per video and
# still keep white text readable via the soft shadow + bar contrast.
        top = tuple(min(255, int(v + rng.randint(10, 32))) for v in lightest)
        bottom = tuple(max(0, int(v - rng.randint(6, 26))) for v in lightest)
        if _luma(bottom) > _luma(top):
            top, bottom = bottom, top
        # Blend toward a soft neutral so the card feels mixed, not saturated footage.
        neutral = (rng.randint(225, 248), rng.randint(226, 250), rng.randint(225, 248))
        top = tuple(int(top[i] * 0.82 + neutral[i] * 0.18) for i in range(3))
        bottom = tuple(int(bottom[i] * 0.82 + neutral[i] * 0.18) for i in range(3))
        # Accent: pick a pleasingly saturated but not harsh tone from the palette.
        accent = max(palette, key=lambda c: max(c) - min(c))
        accent = tuple(min(255, int(v * 1.25)) for v in accent)
        if _luma(accent) > 200:  # avoid a near-white accent that loses punch
            accent = _darken(accent, 0.8)
        return tuple(top), tuple(bottom), tuple(accent)
    except Exception as exc:
        print(f"palette extraction failed ({exc}); using curated palette", flush=True)
        return None


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines, cur = [], ""
    for word in words:
        test = (cur + " " + word).strip()
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def pick_font(size, bold=True, devanagari=False):
    if devanagari:
        dev = FONT_DIR / "NotoSansDevanagari-Bold.ttf"
        if dev.exists():
            return ImageFont.truetype(str(dev), size)
    candidates = [
        FONT_DIR / ("BeVietnamPro-Bold.ttf" if bold else "BeVietnamPro-Medium.ttf"),
        FONT_DIR / "MicrosoftYaHeiBold.ttc",
        FONT_DIR / "STHeitiMedium.ttc",
    ]
    for c in candidates:
        if c.exists():
            return ImageFont.truetype(str(c), size)
    raise SystemExit(f"no usable font found in {FONT_DIR}")


def make_card(
    out_path,
    width,
    height,
    title,
    subtitle=None,
    top=None,
    bottom=None,
    accent=None,
):
    """Render an intro/outro card whose colors come from the video footage.

    ``top`` / ``bottom`` are the vertical gradient ends, ``accent`` the highlight
    color for the bar and glow. Falls back to the classic blue when None.
    """
    if top is None:
        top = (12, 22, 46)
    if bottom is None:
        bottom = (34, 62, 140)
    if accent is None:
        accent = (255, 190, 40)
    dev = has_devanagari(title) or bool(subtitle and has_devanagari(subtitle))
    img = make_gradient((width, height), top, bottom)
    # two soft blobs: a broad glow tinted with the footage color, and a smaller
    # accent-colored pop in the lower corner for a richer, less flat canvas
    def _blob(box, strength):
        mask = Image.new("L", (width, height), 0)
        md = ImageDraw.Draw(mask)
        md.ellipse(box, fill=strength)
        return mask.filter(ImageFilter.GaussianBlur(max(40, int(width * 0.19))))

    glow_img = Image.new("RGB", (width, height), _brighten(bottom, 1.15))
    img = Image.composite(glow_img, img, _blob(
        [int(width * 0.02), int(height * 0.02), int(width * 0.98), int(height * 0.70)], 90))
    pop_img = Image.new("RGB", (width, height), accent)
    img = Image.composite(pop_img, img, _blob(
        [int(width * 0.45), int(height * 0.55), int(width * 1.05), int(height * 1.0)], 55))
    draw = ImageDraw.Draw(img)

    scale = width / 720.0
    font_size = max(26, int(62 * scale))
    title_font = pick_font(font_size, devanagari=dev)
    max_w = width - int(110 * scale)
    lines = wrap_text(draw, title, title_font, max_w)
    while len(lines) > 4 and font_size > 26:
        font_size -= int(6 * scale)
        title_font = pick_font(font_size, devanagari=dev)
        lines = wrap_text(draw, title, title_font, max_w)

    line_h = title_font.size + int(14 * scale)
    block_h = len(lines) * line_h
    start_y = (height - block_h) // 2 - int(40 * scale)
    for i, line in enumerate(lines):
        tw = draw.textlength(line, font=title_font)
        x = (width - tw) / 2
        y = start_y + i * line_h
        # soft shadow then main text
        draw.text((x + 3, y + 3), line, font=title_font, fill=(0, 0, 0, 180))
        draw.text((x, y), line, font=title_font, fill=(255, 255, 255))

    # accent bar under title
    bar_y = start_y + block_h + int(28 * scale)
    bar_w = int(160 * scale)
    draw.rectangle([(width - bar_w) / 2, bar_y, (width + bar_w) / 2, bar_y + int(6 * scale)], fill=accent)

    if subtitle:
        sub_font = pick_font(int(30 * scale), bold=False, devanagari=dev)
        y = bar_y + int(40 * scale)
        sw = draw.textlength(subtitle, font=sub_font)
        draw.text(((width - sw) / 2, y), subtitle, font=sub_font, fill=(235, 238, 245))

    img.save(out_path)
    print(f"card written: {out_path}")


async def narrate(text, voice, out_path):
    import edge_tts

    await edge_tts.Communicate(text, voice).save(str(out_path))
    print(f"narration written: {out_path} (voice={voice})")


def build_filter(width, height, fps, intro_dur, main_dur, outro_dur):
    off1 = intro_dur - CROSSFADE
    off2 = intro_dur + main_dur - 2 * CROSSFADE
    total = intro_dur + main_dur + outro_dur - 2 * CROSSFADE
    intro_frames = int(round(intro_dur * fps))
    vf = (
        "[0:v]format=yuv420p,scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h},zoompan=z='min(zoom+0.0010,1.12)':d={ifr}:s={w}x{h}:fps={fps},"
        "fade=t=in:st=0:d=0.45,format=yuv420p,settb=AVTB[v0];"
        "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,apad,atrim=0:{idur}[a0];"
        "[2:v]format=yuv420p,scale={w}:{h},setsar=1,fps={fps},settb=AVTB[v1];"
        "[2:a]aformat=sample_rates=44100:channel_layouts=stereo[a1];"
        "[3:v]format=yuv420p,scale={w}:{h}:force_original_aspect_ratio=increase,"
        "crop={w}:{h},zoompan=z='min(zoom+0.0012,1.10)':d={ofr}:s={w}x{h}:fps={fps},"
        "fade=t=in:st=0:d=0.45,format=yuv420p,settb=AVTB[v2];"
        "[4:a]aformat=sample_rates=44100:channel_layouts=stereo[a2];"
        "[v0][v1]xfade=transition=fade:duration={cf}:offset={off1}[x1];"
        "[x1][v2]xfade=transition=fade:duration={cf}:offset={off2}[vx];"
        "[vx]format=yuv420p[vout];"
        "[a0][a1]acrossfade=d={cf}:c1=tri:c2=tri[a01];"
        "[a01][a2]acrossfade=d={cf}:c1=tri:c2=tri[aout]"
    ).format(
        w=width, h=height, fps=fps,
        ifr=intro_frames, ofr=int(round(outro_dur * fps)),
        idur=f"{intro_dur:.3f}",
        cf=CROSSFADE, off1=f"{off1:.3f}", off2=f"{off2:.3f}",
    )
    return vf, total


def validate_output(out_file):
    """Ensure the rendered video is standard yuv420p H.264 that every player can decode."""
    import subprocess as sp

    pf = sp.check_output(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=pix_fmt", "-of", "csv=p=0", str(out_file),
        ],
        text=True,
    ).strip()
    if pf != "yuv420p":
        raise SystemExit(
            f"render validation failed: expected yuv420p, got {pf}; refusing to deliver"
        )
    result = sp.run(
        ["ffmpeg", "-v", "error", "-i", str(out_file), "-f", "null", "-"],
        stdout=sp.DEVNULL,
        stderr=sp.PIPE,
        text=True,
    )
    if result.returncode != 0 or result.stderr.strip():
        raise SystemExit(
            "render validation failed: output cannot be decoded: "
            + (result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error")
        )
    print(f"validation passed: pix_fmt={pf}, decode clean")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True, help="main video file (final-1.mp4)")
    parser.add_argument("--title", required=True, help="video topic / title text")
    parser.add_argument("--voice", default="en-US-JennyNeural")
    parser.add_argument("--out", required=True, help="output file path")
    parser.add_argument(
        "--outro-text",
        default=None,
        help="custom outro lines; defaults to English, or Hindi when the title is Devanagari",
    )
    parser.add_argument("--intro-min-duration", type=float, default=3.2)
    parser.add_argument("--outro-duration", type=float, default=3.5)
    parser.add_argument(
        "--palette",
        default="auto",
        choices=("auto", "random", "classic", "soft", "silk"),
        help=(
            "card colors: auto = match video footage, random = curated palette, "
            "classic = original blue, soft = fully random soft/silk/light gradient, "
            "silk = fresh dynamic soft-silk gradient chosen per video"
        ),
    )
    parser.add_argument("--seed", type=int, default=None, help="seed for --palette random")
    args = parser.parse_args()

    main_video = Path(args.video).resolve()
    if not main_video.exists():
        raise SystemExit(f"video not found: {main_video}")
    out_file = Path(args.out).resolve()
    workdir = out_file.parent
    workdir.mkdir(parents=True, exist_ok=True)

    width, height, fps, main_dur = probe_video(main_video)
    print(f"main video: {width}x{height} @ {fps}fps, {main_dur:.1f}s")

    intro_png = workdir / "intro_card.png"
    outro_png = workdir / "outro_card.png"
    narration = workdir / "intro_narration.mp3"

    if args.outro_text:
        outro_lines = [ln.strip() for ln in args.outro_text.splitlines() if ln.strip()]
    elif has_devanagari(args.title):
        outro_lines = ["देखने के लिए धन्यवाद!", "ऐसे ही और वीडियो के लिए सब्सक्राइब करें"]
    else:
        outro_lines = ["Thanks for watching!", "Subscribe for more"]
    outro_title = outro_lines[0] if outro_lines else "Thanks for watching!"
    outro_sub = outro_lines[1] if len(outro_lines) > 1 else None

    # pick card colors: extract from the footage, or fall back to curated/classic/soft
    palette = extract_footage_palette(main_video) if args.palette == "auto" else None
    if palette is None:
        if args.palette == "classic":
            palette = ((12, 22, 46), (34, 62, 140), (255, 190, 40))
        elif args.palette == "soft":
            rng = random.Random(args.seed)
            palette = generate_soft_palette(rng)
        elif args.palette == "silk":
            rng = random.Random(args.seed)
            palette = generate_silk_palette(rng)
        else:
            rng = random.Random(args.seed)
            palette = rng.choice(_RANDOM_PALETTES)
    top, bottom, accent = palette
    print(
        f"card palette: top={top} bottom={bottom} accent={accent}"
        + (" (extracted from footage)" if args.palette == "auto" and palette else ""),
        flush=True,
    )

    make_card(intro_png, width, height, args.title, top=top, bottom=bottom, accent=accent)
    make_card(
        outro_png, width, height, outro_title,
        subtitle=outro_sub, top=top, bottom=bottom, accent=accent,
    )

    asyncio.run(narrate(args.title, args.voice, narration))

    narr_dur = float(subprocess.check_output(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(narration),
        ],
        text=True,
    ).strip())
    intro_dur = max(args.intro_min_duration, narr_dur + 1.2)
    outro_dur = args.outro_duration

    vf, total = build_filter(width, height, fps, intro_dur, main_dur, outro_dur)
    print(f"main={main_dur:.1f}s intro={intro_dur:.1f}s outro={outro_dur:.1f}s total={total:.1f}s")

    codec = pick_video_codec()
    print(f"video codec: {codec}", flush=True)
    codec_args = encode_args_for(codec)
    cmd = [
        "ffmpeg", "-y",
        "-i", str(intro_png),
        "-i", str(narration),
        "-i", str(main_video),
        "-i", str(outro_png),
        "-f", "lavfi", "-t", f"{outro_dur:.3f}", "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", vf,
        "-map", "[vout]", "-map", "[aout]",
        *codec_args,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total:.3f}",
        "-movflags", "+faststart",
        str(out_file),
    ]
    log_path = workdir / "intro_outro_ffmpeg.log"
    try:
        run(cmd, log_path=log_path)
    except subprocess.CalledProcessError:
        if codec == "libx264":
            raise
        print(f"codec {codec} failed; retrying with libx264", flush=True)
        idx = cmd.index("-c:v")
        cmd[idx : idx + len(codec_args)] = encode_args_for("libx264")
        run(cmd, log_path=log_path)
    validate_output(out_file)
    print(f"done: {out_file}")


if __name__ == "__main__":
    main()
