# Medal Project Renderer

Medal's video editor saves your edit (cuts, music, effects, fades, transitions) but
sometimes fails at the final **Render video** step. This tool reads the project that
Medal saved and renders it for you with [ffmpeg](https://ffmpeg.org), outside of Medal.

> **Unofficial.** Not made by or affiliated with Medal. "Medal" is a trademark of its
> owner. It only reads files on your own computer. Use it for your own clips.

## What you need

- **Windows, macOS or Linux** (the quick-start below is for Windows)
- **Python 3.8+** - https://www.python.org/downloads/ (tick *"Add python.exe to PATH"*)
- **ffmpeg 6.0 or newer** - on Windows: open Command Prompt and run `winget install Gyan.FFmpeg`,
  then close and reopen the window. (Tested with ffmpeg 6.1.)

Check with `python --version` and `ffmpeg -version`. If `python` isn't recognized, use `py`.

## Quick start (Windows)

1. Find your Medal **project folder**: the folder that contains a file called `project.json`
   next to your media files (they have long names made of letters and numbers, no extension).
   Not sure where it is? See [Finding the project folder](#finding-the-project-folder).
2. Copy `medal_project_render.py` into that folder.
3. In File Explorer, click the address bar, type `cmd` and press Enter.
4. Run, one at a time:

```
python medal_project_render.py . --dry-run
python medal_project_render.py . --preview
python medal_project_render.py . -o final.mp4
```

- `--dry-run` prints your timeline and lists any media files that are missing.
- `--preview` makes a fast, half-resolution test video so you can check the edit.
- the last command makes the full-quality render (1080p, project frame rate, H.264 + AAC).

**Shortcut:** instead of steps 2-4 you can drag your project folder onto `render_medal_project.bat`.

## Options

| Option | What it does |
|---|---|
| `-o FILE` | Output file name (default: `project_rendered.mp4` next to `project.json`) |
| `--media FOLDER` | Extra folder to look for media in (repeatable) |
| `--preview` | Fast half-resolution 30 fps test render |
| `--dry-run` | Print the timeline and exit |
| `--gpu` | Use NVIDIA NVENC instead of CPU x264 |
| `--crf N` / `--preset P` | Quality (lower = better, default 18) / x264 speed preset |
| `--blur-scale X` | Strength of the transition blur (`0` = hard cuts) |
| `--bloom-blur X` | Spread of the bloom glow effect |
| `--literal-animations` | Apply fade animations on every clip piece (see below) |
| `--show-graph` | Print the ffmpeg filter graph (useful for bug reports) |

## What is reproduced

Reproduced as stored in the project:
- clip order, trims and cuts, plus each clip's position, scale and opacity
- separate audio tracks: game, Discord, music, sound effects - with volume, mute and fade in/out
- color adjustment (saturation, contrast, gamma, brightness, red/green/blue)
- simple fade-in / fade-out animations

Approximated (Medal's own shaders can't run outside Medal):
- **Bloom** effect - similar glow, not pixel-identical
- **Linear blur** transitions - a sideways blur around the cut; tune with `--blur-scale`

Not supported yet (the tool prints a note and ignores them): text, stickers/elements,
subtitles, masks, rotation, crop/zoom, other effects/transitions/animations, and
non-trivial playback speed. Support is based on projects saved by Medal editor 1.15.x;
other versions may differ.

**About fades:** when a clip is split in Medal, both pieces keep a copy of its fade. The tool
applies a fade only at the real start/end of a continuous piece of footage, so you don't
get a dip to black at every cut. If your result looks different from Medal's preview, try
`--literal-animations`.

## Finding the project folder

`tools/find_medal_folders.py` scans common locations for Medal folders and writes a report of
recently changed files. Make a small edit in Medal first, then run:

```
python tools\find_medal_folders.py
```

It only reads files, skips browser caches, and blanks values for keys that look sensitive
(token, password, cookie...). Still skim `medal_report.txt` before sharing it.

## Troubleshooting

- **"These media files were not found"** - the project refers to media by a hash. Copy those files
  into the project folder or pass `--media`.
- **"ffmpeg not found"** - install ffmpeg and open a *new* terminal window.
- **Anything else** - re-run with `--show-graph` and open an issue with the error text and the
  "Notes" the tool prints. Please don't attach clips or a `project.json` you wouldn't want public.

## How it works

Medal stores a project as `project.json` (a timeline of layers and clips) plus the media files,
named by hash. Each clip is shown on the timeline from `startTime + leftTrim` to
`startTime + duration - rightTrim`, playing the source from `leftTrim`. The tool turns that into
one ffmpeg filter graph and renders it.

## License

MIT - see `LICENSE`. Provided as-is, with no warranty.
