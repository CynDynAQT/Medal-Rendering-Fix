#!/usr/bin/env python3
"""
medal_find_project.py - find where Medal stores your editor project and write a report.

1. In Medal, make a small edit (or open your project) and let it autosave.
2. Run:  python medal_find_project.py
3. Open medal_report.txt, skim it, and send it to Claude.

It only READS files. Values for keys that look sensitive (token, password,
cookie, auth, secret, session, key) are replaced with <redacted>, and browser
cache/cookie folders are skipped. Please still skim the report before sharing.
Options:  --minutes 180   how far back to look for recently changed files
          --root PATH     extra folder to scan (repeatable)
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

SKIP_DIRS = {"cache", "code cache", "gpucache", "dawncache", "shadercache", "grshadercache",
             "crashpad", "network", "cookies", "blob_storage", "service worker",
             "session storage", "sessions", "node_modules", "__pycache__"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4a", ".mp3", ".wav", ".png", ".jpg", ".jpeg", ".gif"}
SENSITIVE = re.compile(r"token|secret|passw|cookie|auth|session|key|credential", re.I)
MAX_FILES = 40000


def candidate_roots(extra):
    roots = []
    home = Path.home()
    bases = [os.environ.get("APPDATA"), os.environ.get("LOCALAPPDATA"),
             home / "Documents", home / "Videos", home / "Desktop", home / "AppData" / "Local",
             home / "AppData" / "Roaming", home / "Library" / "Application Support"]
    for b in bases:
        if not b:
            continue
        b = Path(b)
        if not b.is_dir():
            continue
        try:
            for child in b.iterdir():
                if child.is_dir() and "medal" in child.name.lower():
                    roots.append(child)
        except OSError:
            pass
    roots += [Path(r) for r in extra]
    seen, out = set(), []
    for r in roots:
        rp = str(r.resolve())
        if rp not in seen:
            seen.add(rp)
            out.append(r)
    return out


def redact(obj):
    if isinstance(obj, dict):
        return {k: ("<redacted>" if SENSITIVE.search(str(k)) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj[:60]]
    return obj


def describe(path: Path, limit=6000):
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError as e:
        return f"  (cannot read: {e})"
    if head.startswith(b"SQLite format 3"):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            lines = ["  SQLite database:"]
            for (name,) in con.execute("select name from sqlite_master where type='table'"):
                cols = [c[1] for c in con.execute(f'pragma table_info("{name}")')]
                n = con.execute(f'select count(*) from "{name}"').fetchone()[0]
                lines.append(f"    table {name} ({n} rows): {', '.join(cols)}")
                if n and not SENSITIVE.search(name):
                    for row in con.execute(f'select * from "{name}" limit 2'):
                        shown = [("<redacted>" if SENSITIVE.search(c) else str(v)[:120]) for c, v in zip(cols, row)]
                        lines.append(f"      e.g. {shown}")
            con.close()
            return "\n".join(lines)
        except sqlite3.Error as e:
            return f"  SQLite file (could not open: {e})"
    if head.startswith(b"PK"):
        return "  zip-style archive (could be a project bundle; send it separately if small)"
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return f"  binary file, first bytes: {head[:16].hex()}"
    try:
        data = redact(json.loads(text))
        dumped = json.dumps(data, indent=1)
        return "  JSON:\n" + "\n".join("    " + l for l in dumped[:limit].splitlines())
    except json.JSONDecodeError:
        snippet = text[:1500]
        return "  text:\n" + "\n".join("    " + l for l in snippet.splitlines())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=180)
    ap.add_argument("--root", action="append", default=[])
    ap.add_argument("-o", "--output", default="medal_report.txt")
    args = ap.parse_args()

    roots = candidate_roots(args.root)
    if not roots:
        sys.exit("Could not find any folder with 'Medal' in its name. "
                 "Re-run with --root \"C:\\path\\to\\Medal\\folder\" (Medal > Settings > Storage shows your clips folder).")

    files = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
                if len(files) >= MAX_FILES:
                    break
    files.sort(reverse=True)

    cutoff = time.time() - args.minutes * 60
    recent = [f for f in files if f[0] >= cutoff]
    shown = recent if recent else files[:40]

    lines = [f"Scanned folders:"] + [f"  {r}" for r in roots]
    lines.append(f"\n{len(files)} files found. Showing "
                 f"{'files changed in the last %d min' % args.minutes if recent else '40 most recently changed files (nothing changed in the window)'}:\n")
    for mt, size, p in shown[:80]:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mt))
        lines.append(f"{stamp}  {size:>12,} B  {p}")
    lines.append("\n===== CONTENTS OF NON-MEDIA FILES =====")
    for mt, size, p in shown[:25]:
        if p.suffix.lower() in VIDEO_EXT or size > 50_000_000:
            continue
        lines.append(f"\n--- {p} ({size:,} B)")
        lines.append(describe(p))

    Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.output} ({len(shown)} files listed). Skim it, then send it to Claude.")


if __name__ == "__main__":
    main()
