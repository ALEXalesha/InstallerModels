#!/usr/bin/env python3
"""Собирает portable exe и установщик. Запускать из папки проекта: python build.py"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from core import check_manifest as check_manifest_shape

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
WORK = HERE / "build"
APP = "InstallerModels"
VERSION = "1.0.5"

# README ссылается на docs/ и показывает оттуда же скриншоты. Рядом с exe этой
# папки не было ни разу: у человека, который поставил программу установщиком,
# половина ссылок в README вела в пустоту, а картинки не открывались вовсе.
DOCS = "docs"

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


def check_tests():
    """Проверки гоняем до сборки, а не после. Ловят они ровно то, что уже
    ломалось: докачку на рваной связи, кривой dest, разъехавшиеся версии."""
    done = subprocess.run([sys.executable, str(HERE / "tests.py")], cwd=HERE)
    if done.returncode:
        sys.exit("проверки не прошли, сборку не начинаю")


def check_pyinstaller():
    """Спрашиваем до очистки dist/ и build/. Не найдётся после - и от прошлой
    сборки ничего не осталось бы, а новая не появилась."""
    probe = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                           capture_output=True)
    if probe.returncode:
        sys.exit("PyInstaller не найден: python -m pip install pyinstaller")


def check_manifest():
    """models.json уезжает внутрь exe. Битый или неполный - программа откроется
    и сразу покажет ошибку, а узнаем мы об этом уже после сборки.

    Сама форма манифеста проверяется тем же кодом, что и при запуске программы:
    здесь лежала своя копия проверок, и копия успела отстать от оригинала. Всё,
    что остаётся снаружи, - раздел lmstudio целиком и title_ru у групп. При
    запуске без них можно жить (вкладка будет пустой, названия возьмутся из
    title), а вот в собранный exe они уезжать не должны.
    """
    try:
        manifest = json.loads((HERE / "models.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        sys.exit(f"models.json не читается: {err}")
    try:
        check_manifest_shape(manifest)
    except ValueError as err:
        sys.exit(str(err))

    if not manifest.get("lmstudio"):
        sys.exit("в models.json нет раздела lmstudio, вкладка окна будет пустой")
    for name, group in manifest["groups"].items():
        if not isinstance(group.get("title_ru"), str) or not group["title_ru"]:
            sys.exit(f"группа {name}: нет строки title_ru, окно покажет английское название")


def check_docs():
    """README едет рядом с exe и ссылается на docs/. Ссылка в никуда в самом
    видном файле поставки - это не мелочь: собранную программу читают именно
    по нему, а поправить его после сборки уже нельзя, только пересобрать."""
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    missing = sorted({
        link for link in re.findall(r"\(({}/[^)]+)\)".format(DOCS), readme)
        if not (HERE / link).exists()
    })
    if missing:
        sys.exit("README ссылается на то, чего нет: " + ", ".join(missing))


def preflight():
    """Все проверки - до сборки. Раньше кодировка setup.nsi проверялась после
    двух прогонов PyInstaller, то есть через пару минут работы впустую."""
    for name in ("gui.py", "core.py", "tests.py", "models.json",
                 "icon.ico", "setup.nsi", "README.md", DOCS):
        if not (HERE / name).exists():
            sys.exit(f"не хватает файла {name}")
    check_docs()
    check_manifest()
    check_nsi_encoding()
    check_window_title()
    check_version()
    check_tests()
    check_pyinstaller()


def folder_size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def lay_out_extras(where):
    """Кладёт рядом с exe то, что читает человек, а не программа.

    models.json ещё и перебивает встроенный в exe - на этом держится вся
    правка списка моделей без пересборки. README до сих пор клался один, без
    docs/, хотя ссылается на docs/build.md, docs/models.md и docs/lmstudio.md
    и показывает оттуда два скриншота. Установщик кладёт всю папку целиком,
    так что в собранной программе README теперь читается как в репозитории.
    """
    for name in ("models.json", "README.md"):
        shutil.copy2(HERE / name, where / name)
    shutil.copytree(HERE / DOCS, where / DOCS, dirs_exist_ok=True)


def main():
    preflight()

    for old in (DIST, WORK):
        shutil.rmtree(old, ignore_errors=True)
        if old.exists():
            sys.exit(f"не могу очистить {old} - что-то держит файлы, закрой это и повтори")

    portable = DIST / "portable"
    pyinstaller("--onefile", portable, WORK / "onefile")
    lay_out_extras(portable)

    payload = DIST / "app"
    pyinstaller("--onedir", payload, WORK / "onedir")
    lay_out_extras(payload / APP)

    # setup.nsi берёт файлы по жёстко записанному пути. Разъедется он с тем, что
    # выложил PyInstaller, - makensis соберёт установщик из воздуха и не пожалуется.
    if not (payload / APP / f"{APP}.exe").exists():
        sys.exit(f"PyInstaller не положил {APP}.exe в {payload / APP}, установщик собирать не из чего")

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
