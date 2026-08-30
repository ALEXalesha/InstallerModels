#!/usr/bin/env python3
"""Собирает portable exe и установщик. Запускать из папки проекта: python build.py"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
WORK = HERE / "build"
APP = "InstallerModels"
VERSION = "1.0.2"

TITLE_IN_GUI = r'window\.title\("([^"]*)"\)'
TITLE_IN_NSI = r'!define WINTITLE "([^"]*)"'
VERSION_IN_NSI = r'!define VERSION "([^"]*)"'

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


def check_nsi_encoding():
    """Без BOM makensis читает файл как ANSI и молча портит всю кириллицу,
    включая заголовок окна, по которому установщик ищет запущенную программу."""
    if not (HERE / "setup.nsi").read_bytes().startswith(b"\xef\xbb\xbf"):
        sys.exit("setup.nsi должен быть в UTF-8 с BOM, иначе кириллица испортится")


def check_window_title():
    """Установщик ищет запущенную программу через FindWindow по заголовку окна.
    Разъедутся строки - он молча перестанет её находить и начнёт затирать файлы
    под работающей программой. В обоих файлах об этом написано предупреждение,
    но до сих пор ничто не мешало поправить одну строку и забыть про вторую."""
    in_gui = re.search(TITLE_IN_GUI, (HERE / "gui.py").read_text(encoding="utf-8"))
    in_nsi = re.search(TITLE_IN_NSI, (HERE / "setup.nsi").read_text(encoding="utf-8-sig"))
    if not in_gui or not in_nsi:
        sys.exit("не нашёл window.title() в gui.py или WINTITLE в setup.nsi")
    if in_gui.group(1) != in_nsi.group(1):
        sys.exit(
            "заголовок окна разъехался, установщик не найдёт запущенную программу:"
            f"\n  gui.py:    {in_gui.group(1)}"
            f"\n  setup.nsi: {in_nsi.group(1)}"
        )


def check_version():
    """setup.nsi собирают и руками, по строке из комментария в его шапке. Тогда
    сработает !ifndef и версия возьмётся оттуда, а не отсюда. Разъедутся - и
    установщик выйдет с чужим номером в «Программах и компонентах»."""
    found = re.search(VERSION_IN_NSI, (HERE / "setup.nsi").read_text(encoding="utf-8-sig"))
    if not found or found.group(1) != VERSION:
        sys.exit(
            "версия разъехалась:"
            f"\n  build.py:  {VERSION}"
            f"\n  setup.nsi: {found.group(1) if found else 'нет !define VERSION'}"
        )


def check_pyinstaller():
    """Зовут его уже после того, как dist/ и build/ стёрты. Не найдётся - и от
    прошлой сборки ничего не останется, а новая не появится."""
    probe = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                           capture_output=True)
    if probe.returncode:
        sys.exit("PyInstaller не найден: python -m pip install pyinstaller")


def check_fields(where, entry, fields):
    """Кавычки внутри f-строки намеренно не вкладываются: так файл читается и
    Python 3.8, который в доках обещан как нижняя граница. Вложенные заработали
    только с 3.12, и на 3.11 сборка падала не с понятной ошибкой, а SyntaxError
    ещё до первой строчки работы."""
    missing = [f for f in fields if f not in entry]
    if missing:
        sys.exit(f"{where}: нет полей " + ", ".join(missing))
    if "size" in fields:
        size = entry["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            sys.exit(f"{where}: size должен быть целым числом байт, а там {size!r}")


def check_manifest():
    """models.json уезжает внутрь exe. Битый или неполный - программа откроется
    и сразу покажет ошибку, а узнаем мы об этом уже после сборки."""
    try:
        manifest = json.loads((HERE / "models.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        sys.exit(f"models.json не читается: {err}")
    for key in ("comfyui_root", "groups", "lmstudio"):
        if key not in manifest:
            sys.exit(f"в models.json нет ключа {key}")

    # Один и тот же dest с разными repo/size в двух группах: качается он один
    # раз, по первой записи, и вторая группа навсегда остаётся «частично».
    seen = {}
    for name, group in manifest["groups"].items():
        check_fields(f"группа {name}", group, ("title", "title_ru", "files"))
        for entry in group["files"]:
            check_fields(f"группа {name}, файл {entry.get('dest', '?')}",
                         entry, ("repo", "path", "dest", "size"))
            first_group, first_entry = seen.setdefault(entry["dest"], (name, entry))
            if first_entry != entry:
                sys.exit(f"{entry['dest']}: разные записи в группах {first_group} и {name}")

    # Вкладку LM Studio окно строит из этих же полей, а проверял их до сих пор
    # никто: опечатка ловилась уже запущенным exe, то есть после всей сборки.
    for model in manifest["lmstudio"]:
        check_fields("раздел lmstudio", model, ("search", "quant", "files"))
        for item in model["files"]:
            check_fields(f"lmstudio {model['search']}", item, ("name", "size"))


def preflight():
    """Все проверки - до сборки. Раньше кодировка setup.nsi проверялась после
    двух прогонов PyInstaller, то есть через пару минут работы впустую."""
    for name in ("gui.py", "core.py", "models.json", "icon.ico", "setup.nsi", "README.md"):
        if not (HERE / name).exists():
            sys.exit(f"не хватает файла {name}")
    check_manifest()
    check_nsi_encoding()
    check_window_title()
    check_version()
    check_pyinstaller()


def folder_size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main():
    preflight()

    for old in (DIST, WORK):
        shutil.rmtree(old, ignore_errors=True)
        if old.exists():
            sys.exit(f"не могу очистить {old} - что-то держит файлы, закрой это и повтори")

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
