#!/usr/bin/env python3
"""
medal_project_render.py - render a Medal editor project (project.json) with ffmpeg.

Reproduces what the Medal editor saved: clip order and trims, the separate audio
tracks (game / Discord / music / sound effects) with their volumes and fades,
color adjustment, bloom, fade-in/out animations and the "linear blur" transitions.

Requires: Python 3.8+ and ffmpeg/ffprobe (6.0 or newer) on PATH.

Usage:
  python medal_project_render.py "C:\\path\\to\\project folder"
  python medal_project_render.py project.json --media "D:\\other\\media" -o final.mp4
  python medal_project_render.py project.json --dry-run     # just print the timeline
  python medal_project_render.py project.json --preview     # fast low-quality test render

Media files are found by the hash listed in project.json (a file named exactly like
the hash, optionally with an extension or a prefix). By default it looks in the
project's folder and its sub-folders; use --media to add more folders.

Reproduced as stored : cuts/trims and order, audio mixing, volumes, audio fades,
                       saturation/contrast/gamma/brightness, fade in/out.
Approximated         : the bloom effect and the linear-blur transition (Medal's own
                       shaders can't be run outside Medal; tune with --blur-scale
                       and --bloom-blur). Notes are printed at the end.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

EPS = 1e-6


def die(msg):
    sys.exit(f"ERROR: {msg}")


def fmt(x):
    s = f"{x:.5f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def num(v, default=0.0):
    return default if v is None else float(v)


# ----------------------------------------------------------------------------
# locating files
# ----------------------------------------------------------------------------
def find_project(arg):
    p = Path(arg)
    if p.is_file():
        return p
    if p.is_dir():
        for pattern in ("project.json", "*project.json"):
            hits = sorted(p.glob(pattern))
            if hits:
                return hits[0]
        hits = sorted(p.rglob("project.json"))
        if hits:
            return hits[0]
    die(f"could not find project.json at '{arg}'")


def index_files(folders):
    files, seen = [], set()
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for f in folder.rglob("*"):
            if f.is_file() and len(f.relative_to(folder).parts) <= 3 and f not in seen:
                seen.add(f)
                files.append(f)
    return files


def locate(files, entry):
    h = (entry.get("hash") or "").strip()
    if not h:
        return None
    for f in files:
        if f.name == h or f.stem == h or f.name.endswith("-" + h) or f.stem.endswith("-" + h):
            return f
    return None


def probe_has_audio(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
             "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
        return "audio" in out
    except OSError:
        return False


# ----------------------------------------------------------------------------
# reading the project
# ----------------------------------------------------------------------------
def load_items(project, files, notes):
    lib = {m["id"]: m for m in project["library"]["media"]}
    tl = project["timeline"]
    layers = tl["layers"]
    video, audio, missing = [], [], {}

    def common(c, li):
        spd = num(c.get("playbackSpeed"), 1.0) or 1.0
        start = c["startTime"] + c["leftTrim"]  # where the visible part begins on the timeline
        end = c["startTime"] + c["duration"] - c["rightTrim"]
        if end - start <= EPS:
            return None
        if abs(spd - 1) > EPS:
            notes.add("playback speed changes are applied in a simplified way")
        entry = lib.get(c["mediaDataId"])
        if entry is None:
            die(f"clip {c['id']} refers to unknown media {c['mediaDataId']}")
        path = locate(files, entry)
        if path is None:
            missing[entry.get("hash") or entry["id"]] = entry.get("name", "?")
        return dict(c=c, layer=li, start=start, end=end, len=end - start, spd=spd,
                    src_in=c["leftTrim"], src_out=end - c["startTime"], entry=entry, path=path, id=c["id"])

    for li, layer in enumerate(layers):
        for c in layer["clips"]:
            if c.get("isVisible") is False:
                continue
            if c.get("clipMasks"):
                notes.add("clip masks are not supported (ignored)")
            if c.get("filters"):
                notes.add("clip filters are not supported (ignored)")
            if c.get("blendMode", "normal") != "normal":
                notes.add("blend modes other than normal are not supported (ignored)")
            base = common(c, li)
            if base is None:
                continue
            vol = num(c.get("volume"), 1.0) * num(layer.get("volume"), 1.0)
            muted = c.get("muted") or layer.get("muted")
            if c["type"] == "video":
                if layer.get("visible", True):
                    video.append(base)
                if vol > EPS and not muted and base["path"] is not None and probe_has_audio(base["path"]):
                    audio.append(dict(base, vol=vol))
            elif c["type"] == "audio":
                if vol > EPS and not muted:
                    audio.append(dict(base, vol=vol))
            else:
                notes.add(f"clip type '{c['type']}' is not supported (ignored)")

    # Pieces created by splitting a clip carry copies of its fade animations.
    # Those copies must not restart in the middle of continuous footage.
    for li in range(len(layers)):
        vs = sorted([v for v in video if v["layer"] == li], key=lambda v: v["start"])
        for i, v in enumerate(vs):
            prev = vs[i - 1] if i > 0 else None
            nxt = vs[i + 1] if i + 1 < len(vs) else None
            v["cont_prev"] = bool(prev) and prev["entry"]["id"] == v["entry"]["id"] \
                and abs(prev["end"] - v["start"]) < 0.02 and abs(prev["src_out"] - v["src_in"]) < 0.02
            v["cont_next"] = bool(nxt) and nxt["entry"]["id"] == v["entry"]["id"] \
                and abs(nxt["start"] - v["end"]) < 0.02 and abs(nxt["src_in"] - v["src_out"]) < 0.02

    ends = [i["end"] for i in video + audio]
    total = float(tl.get("fitDuration") or (max(ends) if ends else 0))
    return video, audio, total, missing


# ----------------------------------------------------------------------------
# filter graph helpers
# ----------------------------------------------------------------------------
class Chain:
    """Collects filters for one stream and flushes them into labelled statements."""

    def __init__(self, in_label, stmts, uid):
        self.cur, self.f, self.stmts, self.uid, self.n = in_label, [], stmts, uid, 0

    def add(self, *filters):
        self.f.extend(x for x in filters if x)

    def flush(self, out=None):
        self.n += 1
        out = out or f"{self.uid}_{self.n}"
        self.stmts.append(f"{self.cur}{','.join(self.f) if self.f else 'null'}[{out}]")
        self.cur, self.f = f"[{out}]", []
        return out

    def bloom(self, thr, mult, sigma):
        """Additive glow: keep pixels above the threshold, blur them, add them back."""
        self.add("format=gbrp")
        src = self.flush()
        a, b, o = (f"{self.uid}_{x}{self.n}" for x in ("o", "g", "b"))
        self.stmts.append(f"[{src}]split=2[{a}][{b}]")
        expr = f"if(gt(val,{fmt(thr)}),val*{fmt(mult)},0)"
        self.stmts.append(f"[{b}]lutrgb=r='{expr}':g='{expr}':b='{expr}',gblur=sigma={fmt(sigma)}:steps=3[{o}]")
        out = f"{self.uid}_m{self.n}"
        self.stmts.append(f"[{a}][{o}]blend=all_mode=addition[{out}]")
        self.cur = f"[{out}]"
        self.add("format=yuv420p")


def adjustment_filters(p, notes):
    f, eq = [], []
    g, s, ct = p.get("gamma", 1), p.get("saturation", 1), p.get("contrast", 1)
    if abs(g - 1) > EPS:
        eq.append(f"gamma={fmt(max(0.1, min(10, g)))}")
    if abs(s - 1) > EPS:
        eq.append(f"saturation={fmt(max(0, min(3, s)))}")
    if abs(ct - 1) > EPS:
        eq.append(f"contrast={fmt(ct)}")
    if eq:
        f.append("eq=" + ":".join(eq))
    b, r, gr, bl = p.get("brightness", 1), p.get("red", 1), p.get("green", 1), p.get("blue", 1)
    if any(abs(x - 1) > EPS for x in (b, r, gr, bl)):
        f.append(f"colorchannelmixer=rr={fmt(r * b)}:gg={fmt(gr * b)}:bb={fmt(bl * b)}")
    for k in ("hue", "vibrance", "temperature", "tint"):
        if abs(p.get(k, 0)) > EPS:
            notes.add(f"color adjustment '{k}' is not applied")
    if abs(p.get("alpha", 1) - 1) > EPS:
        notes.add("adjustment alpha is not applied")
    return f


def is_fade(anim, dur, kind, notes):
    """True if this stored animation is a simple alpha fade in/out of the requested kind."""
    if not anim or dur <= EPS:
        return False
    for pa in anim.get("propertyAnimations", []):
        if pa.get("property") != "alpha":
            notes.add(f"animation '{anim.get('name')}' animates '{pa.get('property')}' (not supported)")
            return False
        kfs = pa.get("keyframes", [])
        if len(kfs) >= 2 and (kfs[-1]["value"] > kfs[0]["value"]) == (kind == "in"):
            return True
    return False


def linear_blur(length, dur, peak, ramp_up, steps=4):
    """Directional (horizontal) blur in steps: ramps up towards the cut or down after it."""
    dur = min(dur, length)
    out = []
    for k in range(1, steps + 1):
        if ramp_up:
            a, b, s = length - dur + (k - 1) * dur / steps, length - dur + k * dur / steps, peak * k / steps
        else:
            a, b, s = (k - 1) * dur / steps, k * dur / steps, peak * (steps - k + 1) / steps
        out.append(f"gblur=sigma={fmt(s)}:sigmaV=0:steps=2:enable='gte(t,{fmt(a)})*lt(t,{fmt(b)})'")
    return out


def atempo_chain(speed):
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={fmt(speed)}")
    return ",".join(parts)


# ----------------------------------------------------------------------------
# the graph
# ----------------------------------------------------------------------------
def build_graph(video, audio, total, fps, display, project, args, notes):
    W, H = int(display["width"]), int(display["height"])
    stmts, inputs = [], []

    t_after, t_before = {}, {}  # clip id -> (duration, intensity)
    for layer in project["timeline"]["layers"]:
        for tr in layer.get("transitions", []):
            if tr.get("transitionId") != "linearblur":
                notes.add(f"transition '{tr.get('transitionId')}' is not supported (hard cut used)")
                continue
            inten = dict(tr.get("properties", [])).get("intensity", 0.1)
            t_after[tr["startClipId"]] = (tr.get("inDuration", 0.1), inten)
            t_before[tr["endClipId"]] = (tr.get("outDuration", 0.1), inten)
            notes.add("linear-blur transitions are approximated")

    # ---- video: every clip is processed, shifted to its timeline position and overlaid ----
    video = sorted(video, key=lambda v: (v["layer"], num((v["c"].get("style") or {}).get("zIndex")), v["start"]))
    placed = []
    for n, v in enumerate(video):
        k = len(inputs)
        inputs.append((v["src_in"], v["len"] * v["spd"], v["path"]))
        c, st = v["c"], v["c"].get("style") or {}
        sx, sy = st.get("scale") or [1, 1]
        tw = max(2, int(round(num(st.get("width"), W) * sx / 2)) * 2)
        th = max(2, int(round(num(st.get("height"), H) * sy / 2)) * 2)
        px, py = st.get("position") or [W / 2, H / 2]
        if abs(num(st.get("rotation"))) > EPS:
            notes.add("clip rotation is not supported (ignored)")
        if any(abs(x) > EPS for x in (st.get("crop") or [])) or any(abs(x - 1) > EPS for x in (st.get("zoom") or [])):
            notes.add("crop/zoom is not supported (ignored)")
        if any(abs(x) > EPS for x in (st.get("cornerRadius") or [])):
            notes.add("rounded corners are not supported (ignored)")

        ch = Chain(f"[{k}:v]", stmts, f"v{n}")
        ch.add("setpts=(PTS-STARTPTS)/" + fmt(v["spd"]) if abs(v["spd"] - 1) > EPS else "setpts=PTS-STARTPTS",
               f"fps={fps}", f"scale={tw}:{th}:flags=lanczos", "setsar=1", "format=yuv420p")

        for e in c.get("effects", []):
            p, eid = dict(e.get("properties", [])), e.get("effectId", "")
            if eid == "builtin-adjustment":
                ch.add(*adjustment_filters(p, notes))
            elif eid == "builtin-advanced-bloom":
                ch.bloom(thr=max(0.0, min(1.0, p.get("threshold", 0.5))) * 255,
                         mult=p.get("brightness", 1) * p.get("bloomScale", 1),
                         sigma=max(0.5, p.get("blur", 8) * args.bloom_blur))
                notes.add("bloom is approximated")
            else:
                notes.add(f"effect '{eid}' is not supported (ignored)")

        peak = lambda inten: inten * W * 0.2 * args.blur_scale
        if v["id"] in t_after and args.blur_scale > 0:
            d, inten = t_after[v["id"]]
            ch.add(*linear_blur(v["len"], d, peak(inten), ramp_up=True))
        if v["id"] in t_before and args.blur_scale > 0:
            d, inten = t_before[v["id"]]
            ch.add(*linear_blur(v["len"], d, peak(inten), ramp_up=False))

        ac = c.get("animationController") or {}
        din, dout = num(ac.get("animationInDuration")), num(ac.get("animationOutDuration"))
        fade_in = is_fade(ac.get("animationDataIn"), din, "in", notes) and (args.literal_animations or not v["cont_prev"])
        fade_out = is_fade(ac.get("animationDataOut"), dout, "out", notes) and (args.literal_animations or not v["cont_next"])
        alpha = num(st.get("alpha"), 1.0)
        if fade_in or fade_out or alpha < 1 - EPS:
            ch.add("format=yuva420p")
            if fade_in:
                ch.add(f"fade=t=in:st=0:d={fmt(min(din, v['len']))}:alpha=1")
            if fade_out:
                d = min(dout, v["len"])
                ch.add(f"fade=t=out:st={fmt(v['len'] - d)}:d={fmt(d)}:alpha=1")
            if alpha < 1 - EPS:
                ch.add(f"colorchannelmixer=aa={fmt(alpha)}")
        ch.add(f"setpts=PTS+{fmt(v['start'])}/TB")
        lab = ch.flush(f"cv{n}")
        placed.append((v, lab, int(round(px - tw / 2)), int(round(py - th / 2))))

    stmts.append(f"color=c={display.get('backgroundColor', '#000000').replace('#', '0x')}:s={W}x{H}:r={fps}:d={fmt(total)}[bg0]")
    base = "bg0"
    for n, (v, lab, x, y) in enumerate(placed):
        out = f"bg{n + 1}"
        stmts.append(f"[{base}][{lab}]overlay=x={x}:y={y}:eof_action=pass:"
                     f"enable='gte(t,{fmt(v['start'])})*lt(t,{fmt(v['end'])})'[{out}]")
        base = out
    tail = ["format=yuv420p"]
    if args.preview:
        tail = [f"scale={W // 2}:{H // 2}", "fps=30", "format=yuv420p"]
    stmts.append(f"[{base}]{','.join(tail)}[vout]")

    # ---- audio -----------------------------------------------------------------
    labels = []
    for n, a in enumerate(audio):
        k = len(inputs)
        inputs.append((a["src_in"], a["len"] * a["spd"], a["path"]))
        c = a["c"]
        f = ["aresample=48000", "aformat=sample_fmts=fltp:channel_layouts=stereo", "asetpts=PTS-STARTPTS"]
        if abs(a["spd"] - 1) > EPS:
            f.append(atempo_chain(a["spd"]))
        f.append(f"volume={fmt(a['vol'])}")
        fi, fo = num(c.get("volumeFadeInDuration")), num(c.get("volumeFadeOutDuration"))
        if fi > EPS:
            f.append(f"afade=t=in:st=0:d={fmt(min(fi, a['len']))}:curve=tri")
        if fo > EPS:
            d = min(fo, a["len"])
            f.append(f"afade=t=out:st={fmt(a['len'] - d)}:d={fmt(d)}:curve=tri")
        if a["start"] > EPS:
            f.append(f"adelay=delays={fmt(a['start'] * 1000)}:all=1")
        stmts.append(f"[{k}:a]{','.join(f)}[a{n}]")
        labels.append(f"[a{n}]")
    if labels:
        master = num(project["timeline"].get("volume"), 1.0)
        mix = (f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0:duration=longest:dropout_transition=0"
               if len(labels) > 1 else f"{labels[0]}anull")
        stmts.append(f"{mix},volume={fmt(master)},alimiter=limit=0.97[aout]")
    return ";".join(stmts), inputs, bool(labels)


# ----------------------------------------------------------------------------
def print_plan(video, audio, total, missing):
    print(f"Timeline length: {total:.3f}s")
    print("\nVIDEO (in order)")
    for v in sorted(video, key=lambda v: v["start"]):
        tag = " [fade-in]" if not v["cont_prev"] and (v["c"].get("animationController") or {}).get("animationInDuration") else ""
        tag += " [fade-out]" if not v["cont_next"] and (v["c"].get("animationController") or {}).get("animationOutDuration") else ""
        fx = ",".join(e["effectId"].replace("builtin-", "") for e in v["c"].get("effects", []))
        print(f"  {v['start']:7.3f} -> {v['end']:7.3f}  {v['entry']['name']!r:14} from {v['src_in']:6.3f}s  fx=[{fx}]{tag}")
    print("\nAUDIO")
    for a in sorted(audio, key=lambda a: (a["start"], a["layer"])):
        c = a["c"]
        print(f"  {a['start']:7.3f} -> {a['end']:7.3f}  {a['entry']['name'][:42]:42} vol={a['vol']:.2f} "
              f"fadeIn={c.get('volumeFadeInDuration', 0)} fadeOut={c.get('volumeFadeOutDuration', 0)}")
    if missing:
        print("\nMISSING MEDIA (hash -> name):")
        for h, nme in missing.items():
            print(f"  {h}  {nme}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="project.json, or the folder that contains it")
    ap.add_argument("-o", "--output", help="output .mp4 (default: next to project.json)")
    ap.add_argument("--media", action="append", default=[], help="extra folder with media files (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="print the timeline and exit")
    ap.add_argument("--preview", action="store_true", help="fast half-resolution 30fps test render")
    ap.add_argument("--crf", type=int, default=18, help="quality, lower = better (default 18)")
    ap.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    ap.add_argument("--gpu", action="store_true", help="use NVIDIA NVENC instead of x264")
    ap.add_argument("--blur-scale", type=float, default=1.0, help="strength of transition blur (0 = hard cuts)")
    ap.add_argument("--bloom-blur", type=float, default=1.0, help="multiplier for bloom spread")
    ap.add_argument("--literal-animations", action="store_true",
                    help="apply fade animations on every piece, even continuation pieces of a split clip")
    ap.add_argument("--show-graph", action="store_true", help="print the ffmpeg filter graph")
    args = ap.parse_args()

    pj = find_project(args.project)
    project = json.loads(pj.read_text(encoding="utf-8"))
    files = index_files([pj.parent] + [Path(m) for m in args.media])
    notes = set()
    video, audio, total, missing = load_items(project, files, notes)
    if total <= 0:
        die("project has no clips")
    fps = int(round(project["timeline"].get("fps", 60)))

    print(f"Project: {pj}  (Medal editor version {project.get('version')})")
    print_plan(video, audio, total, missing if args.dry_run else {})
    if args.dry_run:
        return
    if missing:
        print("\nThese media files were not found:")
        for h, nme in missing.items():
            print(f"  {h}  ({nme})")
        die("put them in the project folder (or pass --media FOLDER) and run again.")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            die(f"{tool} not found on PATH. Install ffmpeg first.")

    graph, inputs, has_audio = build_graph(video, audio, total, fps, project["display"], project, args, notes)
    out = Path(args.output) if args.output else pj.with_name(pj.stem + ("_preview" if args.preview else "_rendered") + ".mp4")
    if args.show_graph:
        print("\n" + graph.replace(";", ";\n"))

    cmd = ["ffmpeg", "-y", "-hide_banner", "-v", "error", "-stats"]
    for src_in, length, path in inputs:
        cmd += ["-ss", fmt(src_in), "-t", fmt(length + 0.05), "-i", str(path)]
    cmd += ["-filter_complex", graph, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "[aout]"]
    if args.preview:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28"]
    elif args.gpu:
        cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(args.crf), "-b:v", "0"]
    else:
        cmd += ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf)]
    cmd += ["-pix_fmt", "yuv420p"]
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-t", fmt(total), "-movflags", "+faststart", str(out)]

    print(f"\nRendering {len(video)} video clips + {len(audio)} audio clips -> {out}")
    if subprocess.run(cmd).returncode != 0:
        die("ffmpeg failed (see the message above). Re-run with --show-graph and send it to Claude.")
    print(f"\nDone: {out}")
    if notes:
        print("\nNotes (not pixel-identical to Medal's own renderer):")
        for n in sorted(notes):
            print(f"  - {n}")


if __name__ == "__main__":
    main()
