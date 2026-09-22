#!/usr/bin/env python3
"""Собирает папку программы, portable-архив и установщик.

Запускать через build.ps1: он создаёт .venv проекта и зовёт этот скрипт оттуда.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from core import APP, VERSION, WINDOW_TITLE
from core import check_manifest as check_manifest_shape

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
WORK = HERE / "build"

# Имя, версия и заголовок окна приходят из core.py и больше ниоткуда.
# Для NSIS они кладутся вот сюда - он читать Python не умеет.
NSH = "version.nsh"

# README ссылается на docs/ и показывает оттуда же скриншоты. Рядом с exe этой
# папки не было ни разу: у человека, который поставил программу установщиком,
# половина ссылок в README вела в пустоту, а картинки не открывались вовсе.
DOCS = "docs"
# Оба README: английский на витрине GitHub, русский - тот, по которому программой
# пользуются. Рядом с exe нужен прежде всего русский, но кладём оба: ссылки между
# ними внутри поставки тоже должны вести не в пустоту.
READMES = ("README.md", "README.ru.md")

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
    # С версии 2.0 окно на Qt: Tk больше не нужен, а из Qt нужны только
    # Core, Gui и Widgets. Остальное PyInstaller тащит «на всякий случай».
    "--exclude-module", "tkinter",
    "--exclude-module", "_tkinter",
    *[arg for name in ("QtNetwork", "QtQml", "QtQuick", "QtSql", "QtOpenGL", "QtPdf",
                       "QtSvg", "QtDBus")
      for arg in ("--exclude-module", f"PySide6.{name}")],
]

# После PyInstaller из Qt выкидываем то, что окну не нужно никогда: программный
# OpenGL (20 МБ, Qt Widgets его не зовут) и переводы на все языки, кроме русского.
QT_JUNK = ["opengl32sw.dll"]


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
    включая заголовок окна, по которому установщик ищет запущенную программу.
    Касается и подключаемого version.nsh: заголовок окна лежит именно в нём."""
    for name in ("setup.nsi", NSH):
        if not (HERE / name).read_bytes().startswith(b"\xef\xbb\xbf"):
            sys.exit(f"{name} должен быть в UTF-8 с BOM, иначе кириллица испортится")


def write_nsis_defines():
    """Кладёт рядом с setup.nsi три строки из core.py.

    Это и есть вся уборка дублей: NSIS читать Python не умеет, но умеет
    !include, а build.py умеет писать файлы. Раньше вместо этого версия жила в
    трёх местах, заголовок окна в двух, и за их совпадением следили отдельные
    проверки - каждая заведена после того, как копии таки разъехались.

    BOM обязателен: в режиме Unicode makensis определяет кодировку по нему, а в
    заголовке окна кириллица. Без BOM она превратится в мусор, установщик
    перестанет находить запущенную программу и начнёт затирать файлы под ней.
    """
    body = "\n".join([
        "; Этот файл создаёт build.py из констант в core.py.",
        "; Руками не править - перезапишется при следующей сборке.",
        f'!define APP "{APP}"',
        f'!define VERSION "{VERSION}"',
        f'!define WINTITLE "{WINDOW_TITLE}"',
        "",
    ])
    path = HERE / NSH
    path.write_bytes(b"\xef\xbb\xbf" + body.replace("\n", "\r\n").encode("utf-8"))
    return path


def check_nsi_has_no_copies():
    """setup.nsi не должен объявлять эти три штуки сам.

    Дубли имеют свойство возвращаться: подключаемого файла под рукой не
    оказалось, человек дописал !define прямо в setup.nsi - и всё снова работает,
    молча и мимо core.py. Проверка сторожит не совпадение копий, а само их
    появление.
    """
    text = (HERE / "setup.nsi").read_text(encoding="utf-8-sig")
    for name in ("APP", "VERSION", "WINTITLE"):
        if re.search(rf"^\s*!define\s+{name}\b", text, re.M):
            sys.exit(
                f"setup.nsi объявляет {name} сам, хотя должен брать его из {NSH}. "
                f"Единственный источник - константы в core.py."
            )


def check_tests():
    """Проверки гоняем до сборки, а не после. Ловят они ровно то, что уже
    ломалось: докачку на рваной связи, кривой dest, разъехавшиеся версии."""
    done = subprocess.run([sys.executable, str(HERE / "tests.py")], cwd=HERE)
    if done.returncode:
        sys.exit("проверки не прошли, сборку не начинаю")


def check_pyinstaller():
    """Спрашиваем до очистки dist/ и build/. Не найдётся после - и от прошлой
    сборки ничего не осталось бы, а новая не появилась.

    PySide6 и PyInstaller живут в .venv проекта, а не в общем Python: там они
    конфликтовали бы с чужими версиями. build.ps1 создаёт .venv и ставит всё сам.
    """
    probe = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                           capture_output=True)
    if probe.returncode:
        sys.exit("PyInstaller не найден. Собирай через build.ps1 - он поставит всё в .venv")
    probe = subprocess.run([sys.executable, "-c", "import PySide6"], capture_output=True)
    if probe.returncode:
        sys.exit("PySide6 не найден. Собирай через build.ps1 - он поставит всё в .venv")


def trim_qt(app_folder):
    """Убирает из собранной папки куски Qt, которые окну не нужны."""
    qt = app_folder / "_internal" / "PySide6"
    for name in QT_JUNK:
        (qt / name).unlink(missing_ok=True)
    translations = qt / "translations"
    if translations.is_dir():
        for f in translations.iterdir():
            if f.is_file() and not f.name.startswith("qtbase_ru"):
                f.unlink()


def zip_portable(app_folder, target):
    """Portable - та же папка программы, что и в установщике, только в zip.

    До 2.0 portable был одним exe (--onefile). С Qt такой exe при каждом старте
    распаковывал бы себя во временную папку - десятки мегабайт, секунды ожидания.
    Папку распаковывают один раз.
    """
    target.unlink(missing_ok=True)
    return Path(shutil.make_archive(str(target.with_suffix("")), "zip",
                                    root_dir=app_folder.parent, base_dir=app_folder.name))


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
    по нему, а поправить его после сборки уже нельзя, только пересобрать.

    Проверяются оба: README.md английский, README.ru.md русский, и рядом с exe
    кладутся тоже оба."""
    for name in READMES:
        readme = (HERE / name).read_text(encoding="utf-8")
        missing = sorted({
            link for link in re.findall(r"\(({}/[^)]+)\)".format(DOCS), readme)
            if not (HERE / link).exists()
        })
        if missing:
            sys.exit(f"{name} ссылается на то, чего нет: " + ", ".join(missing))


def preflight():
    """Все проверки - до сборки. Раньше кодировка setup.nsi проверялась после
    двух прогонов PyInstaller, то есть через пару минут работы впустую."""
    for name in ("gui.py", "core.py", "tests.py", "tests_matrix.py", "tests_props.py",
                 "models.json",
                 "icon.ico", "setup.nsi", *READMES, DOCS):
        if not (HERE / name).exists():
            sys.exit(f"не хватает файла {name}")
    check_docs()
    check_manifest()
    # Сначала кладём version.nsh, потом проверяем кодировку обоих файлов:
    # без него setup.nsi не соберётся, а проверять нечего.
    write_nsis_defines()
    check_nsi_encoding()
    check_nsi_has_no_copies()
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
    for name in ("models.json", *READMES):
        shutil.copy2(HERE / name, where / name)
    shutil.copytree(HERE / DOCS, where / DOCS, dirs_exist_ok=True)


def main():
    preflight()

    for old in (DIST, WORK):
        shutil.rmtree(old, ignore_errors=True)
        if old.exists():
            sys.exit(f"не могу очистить {old} - что-то держит файлы, закрой это и повтори")

    payload = DIST / "app"
    pyinstaller("--onedir", payload, WORK / "onedir")
    trim_qt(payload / APP)
    lay_out_extras(payload / APP)

    # setup.nsi берёт файлы по жёстко записанному пути. Разъедется он с тем, что
    # выложил PyInstaller, - makensis соберёт установщик из воздуха и не пожалуется.
    if not (payload / APP / f"{APP}.exe").exists():
        sys.exit(f"PyInstaller не положил {APP}.exe в {payload / APP}, установщик собирать не из чего")

    portable = zip_portable(payload / APP, DIST / f"{APP}-portable-{VERSION}.zip")

    nsis = find_nsis()
    if nsis:
        # Ключа /DVERSION больше нет: имя, версия и заголовок приходят из
        # version.nsh, который положил preflight.
        run([str(nsis), str(HERE / "setup.nsi")], "NSIS")
    else:
        print("\nNSIS не найден, установщик не собран. Portable готов.")

    print("\n=== готово ===")
    print(f"portable   {portable}  ({portable.stat().st_size / 1024**2:.1f} МБ)")
    print(f"папка app  {payload / APP}  ({folder_size(payload / APP) / 1024**2:.1f} МБ)")
    setup = DIST / f"{APP}-Setup-{VERSION}.exe"
    if setup.exists():
        print(f"установщик {setup}  ({setup.stat().st_size / 1024**2:.1f} МБ)")


if __name__ == "__main__":
    main()
