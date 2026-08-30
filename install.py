#!/usr/bin/env python3
"""Restore ComfyUI models from Hugging Face. Stdlib only, resumable."""

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "models.json"
RETRIES = 5
CHUNK = 1 << 20


def human(nbytes):
    size = float(nbytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def load_manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


def comfy_root(manifest, override):
    root = override or os.environ.get("COMFYUI_ROOT") or manifest["comfyui_root"]
    return Path(root).expanduser()


def hf_url(repo, path):
    return f"https://huggingface.co/{repo}/resolve/main/{quote(path)}"


def open_stream(url, offset):
    req = urllib.request.Request(url, headers={"User-Agent": "InstallerModels/1.0"})
    if offset:
        req.add_header("Range", f"bytes={offset}-")
    resp = urllib.request.urlopen(req, timeout=60)
    return resp, resp.getcode() == 206


INTERACTIVE = sys.stdout.isatty()


class Progress:
    """Redraws one line in a terminal, prints every 10% when piped to a file."""

    def __init__(self, total, offset):
        self.total = total
        self.offset = offset
        self.started = time.monotonic()
        self.next_step = 0

    def update(self, done):
        elapsed = max(time.monotonic() - self.started, 1e-6)
        speed = (done - self.offset) / elapsed
        pct = done * 100 / self.total if self.total else 0
        eta = (self.total - done) / speed if speed > 0 else 0
        clock = f"{int(eta // 60):3d}:{int(eta % 60):02d}"

        if INTERACTIVE:
            filled = int(28 * done / self.total) if self.total else 0
            bar = "#" * filled + "-" * (28 - filled)
            sys.stdout.write(
                f"\r  [{bar}] {pct:5.1f}%  {human(done)} / {human(self.total)}"
                f"  {human(speed)}/s  ETA {clock}   "
            )
            sys.stdout.flush()
        elif pct >= self.next_step:
            print(f"  {pct:5.1f}%  {human(done)} / {human(self.total)}  {human(speed)}/s  ETA {clock}")
            self.next_step = (int(pct) // 10 + 1) * 10

    def finish(self):
        if INTERACTIVE:
            sys.stdout.write("\n")


def fetch(url, dest, expected):
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)

    if part.exists() and part.stat().st_size > expected:
        part.unlink()

    last_error = None
    for attempt in range(1, RETRIES + 1):
        offset = part.stat().st_size if part.exists() else 0
        if offset == expected:
            break

        try:
            resp, resumed = open_stream(url, offset)
        except urllib.error.HTTPError as err:
            if err.code == 416 and part.exists():
                part.unlink()
                continue
            if 400 <= err.code < 500:
                raise RuntimeError(
                    f"HTTP {err.code} from Hugging Face - file moved or renamed, "
                    f"check repo and path in models.json"
                ) from None
            last_error = err
            if attempt == RETRIES:
                break
            print(f"\n  server error {err.code}, retry {attempt}/{RETRIES - 1} in 5s")
            time.sleep(5)
            continue
        except (urllib.error.URLError, OSError) as err:
            last_error = err
            if attempt == RETRIES:
                break
            print(f"\n  no connection ({err}), retry {attempt}/{RETRIES - 1} in 5s")
            time.sleep(5)
            continue

        if offset and not resumed:
            offset = 0
        mode = "ab" if offset else "wb"

        progress = Progress(expected, offset)
        done = offset
        try:
            with resp, open(part, mode) as out:
                while True:
                    block = resp.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    done += len(block)
                    progress.update(done)
        except (urllib.error.URLError, OSError) as err:
            last_error = err
            progress.finish()
            if attempt == RETRIES:
                break
            print(f"  connection dropped ({err}), resuming in 5s")
            time.sleep(5)
            continue
        progress.finish()

        size_now = part.stat().st_size
        if size_now == expected:
            break
        if size_now == offset:
            print("  server sent nothing new, starting this file over")
            part.unlink()

    actual = part.stat().st_size if part.exists() else 0
    if actual != expected:
        reason = f" (last error: {last_error})" if last_error else ""
        raise RuntimeError(f"got {human(actual)}, expected {human(expected)}{reason}")
    os.replace(part, dest)


def status(entry, root):
    dest = root / entry["dest"]
    if not dest.exists():
        return "missing", 0
    actual = dest.stat().st_size
    return ("ok" if actual == entry["size"] else "damaged"), actual


def cmd_list(manifest, root):
    print(f"ComfyUI root: {root}\n")
    for key, group in manifest["groups"].items():
        total = sum(f["size"] for f in group["files"])
        marks = [status(f, root)[0] for f in group["files"]]
        if all(m == "ok" for m in marks):
            state = "installed"
        elif any(m == "ok" for m in marks):
            state = "partial"
        else:
            state = "not installed"
        print(f"  {key:<11} {human(total):>10}  {group['title']}")
        print(f"  {'':<11} {'':>10}  {len(group['files'])} files, {state}")
    grand = sum(f["size"] for g in manifest["groups"].values() for f in g["files"])
    print(f"\n  everything: {human(grand)}")


def cmd_check(manifest, root):
    bad = 0
    for key, group in manifest["groups"].items():
        print(f"\n{key} - {group['title']}")
        for entry in group["files"]:
            state, actual = status(entry, root)
            if state == "ok":
                print(f"  ok        {entry['dest']}")
            elif state == "missing":
                print(f"  missing   {entry['dest']}")
                bad += 1
            else:
                print(f"  DAMAGED   {entry['dest']}  ({actual} vs {entry['size']})")
                bad += 1
    print(f"\n{bad} file(s) need downloading" if bad else "\nall files present and correct")
    return 1 if bad else 0


def cmd_lmstudio(manifest):
    print("LM Studio models. Paste the name into LM Studio search, or use the lms CLI.\n")
    for model in manifest["lmstudio"]:
        total = sum(f["size"] for f in model["files"])
        print(f"  {model['search']}")
        print(f"    quant to pick: {model['quant']}   total {human(total)}")
        print(f"    CLI:  lms get {model['search']}")
        for item in model["files"]:
            print(f"    - {item['name']}  {human(item['size'])}")
        print()


def cmd_install(manifest, root, keys, dry_run):
    queue = []
    for key in keys:
        for entry in manifest["groups"][key]["files"]:
            state, _ = status(entry, root)
            if state == "ok":
                print(f"skip (already there)  {entry['dest']}")
            else:
                queue.append(entry)

    if not queue:
        print("\nnothing to download")
        return 0

    total = sum(e["size"] for e in queue)
    print(f"\n{len(queue)} file(s) to download, {human(total)}")
    if dry_run:
        for entry in queue:
            print(f"  {entry['repo']}/{entry['path']}")
            print(f"    -> {entry['dest']}  ({human(entry['size'])})")
        return 0

    free = shutil.disk_usage(root).free
    if free < total:
        print(f"not enough free space: {human(free)} available, {human(total)} needed")
        return 1

    failed = []
    for n, entry in enumerate(queue, 1):
        print(f"\n[{n}/{len(queue)}] {entry['dest']}  ({human(entry['size'])})")
        print(f"  from {entry['repo']}/{entry['path']}")
        try:
            fetch(hf_url(entry["repo"], entry["path"]), root / entry["dest"], entry["size"])
        except Exception as err:
            print(f"  FAILED: {err}")
            failed.append(entry["dest"])

    if failed:
        print(f"\n{len(failed)} file(s) failed:")
        for name in failed:
            print(f"  {name}")
        print("run the same command again to resume")
        return 1
    print("\ndone")
    return 0


def main():
    manifest = load_manifest()
    groups = list(manifest["groups"])

    parser = argparse.ArgumentParser(
        description="Restore ComfyUI models from Hugging Face.",
        epilog="groups: " + ", ".join(groups),
    )
    parser.add_argument("groups", nargs="*", help="which groups to install")
    parser.add_argument("--all", action="store_true", help="install every group")
    parser.add_argument("--list", action="store_true", help="show groups and sizes")
    parser.add_argument("--check", action="store_true", help="verify what is on disk")
    parser.add_argument("--lmstudio", action="store_true", help="show LM Studio model names")
    parser.add_argument("--dry-run", action="store_true", help="show what would download")
    parser.add_argument("--root", help="ComfyUI folder (overrides models.json)")
    args = parser.parse_args()

    if args.lmstudio:
        cmd_lmstudio(manifest)
        return 0

    root = comfy_root(manifest, args.root)

    if args.list or not (args.groups or args.all or args.check):
        cmd_list(manifest, root)
        if not args.list:
            print("\nusage:  python install.py ltx qwen   |   python install.py --all")
        return 0

    if not root.exists():
        print(f"ComfyUI folder not found: {root}")
        print("pass --root PATH or edit comfyui_root in models.json")
        return 1

    if args.check:
        return cmd_check(manifest, root)

    keys = groups if args.all else args.groups
    unknown = [k for k in keys if k not in manifest["groups"]]
    if unknown:
        print(f"unknown group(s): {', '.join(unknown)}")
        print(f"available: {', '.join(groups)}")
        return 1

    return cmd_install(manifest, root, keys, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted, partial files kept for resume")
        sys.exit(130)
