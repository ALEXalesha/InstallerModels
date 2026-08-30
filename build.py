#!/usr/bin/env python3
"""Собирает portable exe и установщик. Запускать из папки проекта: python build.py"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
WORK = HERE / "build"
APP = "InstallerModels"
VERSION = "1.0.0"

NSIS_CANDIDATES = [
    Path(r"C:\Program Files (x86)\NSIS\makensis.exe"),
    Path(r"C:\Program Files\NSIS\makensis.exe"),
]

SHARED = [
    "--noconfirm",
    "--windowed",
    "--name", APP,
    "--icon", str(HERE / "icon.ico"),
    "--add-data", f"{HERE / 'models.json'};.",
    "--add-data", f"{HERE / 'icon.ico'};.",
    "--exclude-module", "PIL",
    "--exclude-module", "numpy",
    "--exclude-module", "pygame",
    "--exclude-module", "pytest",
]


def run(args, label):
    print(f"\n=== {label} ===")
    done = subprocess.run(args, cwd=HERE)
    if done.returncode:
        sys.exit(f"{label} упал с кодом {done.returncode}")


def pyinstaller(mode, out_dir, work_dir):
    run(
        [sys.executable, "-m", "PyInstaller", *SHARED, mode,
         "--distpath", str(out_dir), "--workpath", str(work_dir),
         "--specpath", str(work_dir), str(HERE / "gui.py")],
        f"PyInstaller {mode}",
    )


def find_nsis():
    for path in NSIS_CANDIDATES:
        if path.exists():
            return path
    found = shutil.which("makensis")
    return Path(found) if found else None


def folder_size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main():
    for old in (DIST, WORK):
        shutil.rmtree(old, ignore_errors=True)

    portable = DIST / "portable"
    pyinstaller("--onefile", portable, WORK / "onefile")
    for extra in ("models.json", "README.md"):
        shutil.copy2(HERE / extra, portable / extra)

    payload = DIST / "app"
    pyinstaller("--onedir", payload, WORK / "onedir")
    shutil.copy2(HERE / "models.json", payload / APP / "models.json")
    shutil.copy2(HERE / "README.md", payload / APP / "README.md")

    nsis = find_nsis()
    if nsis:
        run([str(nsis), f"/DVERSION={VERSION}", str(HERE / "setup.nsi")], "NSIS")
    else:
        print("\nNSIS не найден, установщик не собран. Portable готов.")

    print("\n=== готово ===")
    exe = portable / f"{APP}.exe"
    print(f"portable   {exe}  ({exe.stat().st_size / 1024**2:.1f} МБ)")
    print(f"папка app  {payload / APP}  ({folder_size(payload / APP) / 1024**2:.1f} МБ)")
    setup = DIST / f"{APP}-Setup-{VERSION}.exe"
    if setup.exists():
        print(f"установщик {setup}  ({setup.stat().st_size / 1024**2:.1f} МБ)")


if __name__ == "__main__":
    main()
