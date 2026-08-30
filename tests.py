#!/usr/bin/env python3
"""Проверки на всё, что уже ломалось. Запуск: python tests.py

Зависимостей нет нарочно: build.py гоняет их перед сборкой, а сборка идёт на
голом Python. Сеть тоже не нужна - Hugging Face изображает локальный сервер,
которому можно велеть рвать соединение когда захочется.
"""

import http.server
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import core

BODY = bytes(range(256)) * 400  # 102400 байт
SIZE = len(BODY)

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# --------------------------------------------------------------- сервер-макет

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "whole"
    hits = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits += 1
        span = self.headers.get("Range")
        start = int(span.split("=")[1].split("-")[0]) if span else 0

        if Handler.mode == "gated":
            self.reply(403)
            return
        if Handler.mode == "gone":
            self.reply(404)
            return
        if Handler.mode == "norange":  # сервер не умеет Range и шлёт файл целиком
            self.reply(200, len(BODY))
            self.wfile.write(BODY)
            return
        if Handler.mode == "norangeflaky":  # Range не умеет и вдобавок рвёт связь
            self.reply(200, len(BODY))
            self.wfile.write(BODY[:7000])
            self.close_connection = True
            return
        if start >= len(BODY):
            self.reply(416)
            return

        if span:
            self.reply(206, len(BODY) - start, f"bytes {start}-{len(BODY)-1}/{len(BODY)}")
        else:
            self.reply(200, len(BODY))

        if Handler.mode == "flaky":  # рвёт после каждых 7000 байт
            self.wfile.write(BODY[start:start + 7000])
            self.close_connection = True
            return
        if Handler.mode == "silent":  # соединение есть, новых байт нет никогда
            self.close_connection = True
            return
        self.wfile.write(BODY[start:])

    def reply(self, code, length=0, span=None):
        self.send_response(code)
        if span:
            self.send_header("Content-Range", span)
        self.send_header("Content-Length", str(length))
        self.end_headers()


class Server(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # соединения рвём нарочно, ругань в консоль не нужна


def serve(mode):
    Handler.mode = mode
    Handler.hits = 0


# ----------------------------------------------------------------- сами тесты

@case
def resume_survives_a_hundred_drops():
    """Обрывы, каждый из которых сдвигает файл, кончаться не должны никогда.

    Счётчик повторов раньше считался на весь файл, и пятый обрыв убивал закачку
    даже там, где каждый честно дописывал очередной кусок. На 30 ГБ обрывов
    бывает под сотню - такой файл не докачивался вообще.
    """
    serve("flaky")
    dest = TMP / "flaky.bin"
    core.fetch(URL, dest, SIZE)
    assert dest.read_bytes() == BODY, "докачанный файл не совпал с исходным"
    assert Handler.hits == 15, f"ждали 15 подходов по 7000 байт, вышло {Handler.hits}"


@case
def dead_stream_gives_up_instead_of_looping():
    """Ноль новых байт - это не прогресс. Иначе цикл повторов был бы вечным."""
    serve("silent")
    dest = TMP / "silent.bin"
    try:
        core.fetch(URL, dest, SIZE)
    except RuntimeError as err:
        assert "server sent no new bytes" in str(err), err
    else:
        raise AssertionError("молчащий сервер обязан был кончиться ошибкой")
    assert Handler.hits == core.RETRIES, f"подходов {Handler.hits}, ждали {core.RETRIES}"


@case
def unfinished_part_is_never_thrown_away():
    """Сдались - и ладно, но недокачанное обязано дожить до следующего запуска."""
    serve("silent")
    dest = TMP / "keep.bin"
    core.part_path(dest).write_bytes(BODY[:50000])
    try:
        core.fetch(URL, dest, SIZE)
    except RuntimeError:
        pass
    assert core.part_path(dest).stat().st_size == 50000, "недокачанный кусок стёрли"


@case
def resume_continues_from_the_part():
    serve("whole")
    dest = TMP / "resume.bin"
    core.part_path(dest).write_bytes(BODY[:40000])
    core.fetch(URL, dest, SIZE)
    assert dest.read_bytes() == BODY
    assert Handler.hits == 1, f"докачка должна была уложиться в один запрос, вышло {Handler.hits}"


@case
def whole_part_needs_no_request_at_all():
    serve("whole")
    dest = TMP / "done.bin"
    core.part_path(dest).write_bytes(BODY)
    core.fetch(URL, dest, SIZE)
    assert dest.read_bytes() == BODY
    assert Handler.hits == 0, "целый .part качать заново незачем"


@case
def server_without_range_still_works():
    serve("norange")
    dest = TMP / "norange.bin"
    core.part_path(dest).write_bytes(BODY[:40000])
    core.fetch(URL, dest, SIZE)
    assert dest.read_bytes() == BODY, "сервер прислал файл целиком, а дописали в хвост"


@case
def wrong_size_in_manifest_is_named_out_loud():
    serve("whole")
    try:
        core.fetch(URL, TMP / "badsize.bin", 999)
    except RuntimeError as err:
        assert "manifest is out of date" in str(err), err
        assert "102400" in str(err), "в ошибке нет настоящего размера, чинить нечем"
    else:
        raise AssertionError("расхождение размеров обязано было всплыть")


@case
def gated_repo_does_not_blame_the_path():
    """403 - это лицензия, которую не приняли, а не переехавший файл."""
    serve("gated")
    try:
        core.fetch(URL, TMP / "gated.bin", SIZE)
    except RuntimeError as err:
        assert "gated" in str(err) and "HF_TOKEN" in str(err), err
    else:
        raise AssertionError("403 обязан был кончиться ошибкой")


@case
def missing_file_blames_the_path():
    serve("gone")
    try:
        core.fetch(URL, TMP / "gone.bin", SIZE)
    except RuntimeError as err:
        assert "models.json" in str(err), err
    else:
        raise AssertionError("404 обязан был кончиться ошибкой")


@case
def cancel_keeps_what_was_downloaded():
    serve("whole")
    dest = TMP / "cancel.bin"
    try:
        core.fetch(URL, dest, SIZE, should_stop=lambda: True)
    except core.Cancelled:
        pass
    else:
        raise AssertionError("отмена обязана была всплыть наверх")
    assert not dest.exists(), "недокачанное нельзя выдавать за готовый файл"


@case
def dest_outside_the_root_is_refused():
    """Path("C:/ComfyUI") / "C:/qwe.bin" - это просто "C:/qwe.bin". Опечатка в
    dest писала мимо папки ComfyUI, и никто этого не проверял."""
    for bad in ("C:/Windows/evil.dll", "../../evil.bin", "models/../../x", "", "."):
        try:
            core.dest_path("C:/ComfyUI", bad)
        except ValueError:
            continue
        raise AssertionError(f"кривой dest прошёл: {bad!r}")
    good = core.dest_path("C:/ComfyUI", "models/vae/x.safetensors")
    assert good == Path("C:/ComfyUI/models/vae/x.safetensors"), good


@case
def manifest_is_checked_when_it_is_read():
    bad = TMP / "bad.json"
    bad.write_text(json.dumps({
        "comfyui_root": "C:/ComfyUI",
        "groups": {"x": {"files": [{"dest": "../out.bin", "size": 1}]}},
    }), encoding="utf-8")
    try:
        core.load_manifest(bad)
    except ValueError:
        return
    raise AssertionError("кривой dest должен всплывать при чтении models.json")


@case
def shipped_manifest_is_sane():
    """Тот самый models.json, который уезжает внутрь exe.

    Форму проверяет core.check_manifest - тот же код, что и при запуске программы.
    Копия проверок жила тут и в build.py, и копии успели разойтись с оригиналом.
    Здесь остаётся то, чего при запуске можно и не иметь, а в собранном exe нельзя:
    русские названия групп и непустой раздел lmstudio - без них вкладка LM Studio
    открывается пустой, а список групп говорит по-английски.
    """
    manifest = core.load_manifest(HERE / "models.json")  # тут же и check_manifest
    assert manifest["lmstudio"], "раздел lmstudio пуст, вкладка окна будет пустой"
    for name, group in manifest["groups"].items():
        assert group.get("title_ru"), f"группа {name}: нет title_ru"


@case
def status_sees_the_part_file():
    """Оборванная закачка лежит под именем .part, и до неё раньше не смотрели:
    гигабайты на диске числились как "ничего нет"."""
    root = TMP / "root"
    entry = {"dest": "models/vae/x.bin", "size": SIZE}
    (root / "models/vae").mkdir(parents=True, exist_ok=True)
    assert core.status(entry, root) == ("missing", 0)

    core.part_path(root / entry["dest"]).write_bytes(BODY[:100])
    assert core.status(entry, root) == ("partial", 100)
    assert core.needed_bytes([entry], root) == SIZE - 100, "место под .part просят заново"

    (root / entry["dest"]).write_bytes(BODY)
    assert core.status(entry, root) == ("ok", SIZE)


@case
def sizes_read_the_way_people_expect():
    assert core.human(0) == "0 B"
    assert core.human(1024) == "1.0 KiB"
    assert core.human(SIZE) == "100.0 KiB"
    assert core.human(30 * 1024 ** 3) == "30.0 GiB"


@case
def window_title_matches_the_installer():
    """Установщик ищет запущенную программу через FindWindow по заголовку окна.
    Разъедутся строки - он молча начнёт затирать файлы под работающей программой."""
    import re
    in_gui = re.search(r'window\.title\("([^"]*)"\)', (HERE / "gui.py").read_text(encoding="utf-8"))
    in_nsi = re.search(r'!define WINTITLE "([^"]*)"',
                       (HERE / "setup.nsi").read_text(encoding="utf-8-sig"))
    assert in_gui and in_nsi, "не нашёл заголовок в gui.py или setup.nsi"
    assert in_gui.group(1) == in_nsi.group(1), f"{in_gui.group(1)!r} != {in_nsi.group(1)!r}"


@case
def versions_match_everywhere():
    import re
    in_build = re.search(r'VERSION = "([^"]*)"', (HERE / "build.py").read_text(encoding="utf-8"))
    text = (HERE / "setup.nsi").read_text(encoding="utf-8-sig")
    in_nsi = re.search(r'!define VERSION "([^"]*)"', text)
    assert in_build and in_nsi, "не нашёл версию в build.py или setup.nsi"
    assert in_build.group(1) == in_nsi.group(1), f"{in_build.group(1)} != {in_nsi.group(1)}"
    assert f"/DVERSION={in_build.group(1)} setup.nsi" in text, \
        "команда для ручной сборки в шапке setup.nsi осталась на старой версии"


@case
def nsi_keeps_its_bom():
    """Без BOM makensis читает файл как ANSI и молча портит всю кириллицу."""
    assert (HERE / "setup.nsi").read_bytes().startswith(b"\xef\xbb\xbf"), \
        "setup.nsi должен быть в UTF-8 с BOM"


@case
def a_server_without_range_cannot_loop_forever():
    """Сервер, который не умеет Range и вдобавок рвёт связь, гонял бесконечный круг.

    Счётчик обрывов обнулялся по «файл вырос», а каждый заход начинался с нуля и
    рос заново - выйти из этого круга было нечем. Окно и консоль качали один и тот
    же кусок, пока человек не нажмёт Отмену. Перезапуски теперь считаются отдельно.
    """
    serve("norangeflaky")
    try:
        core.fetch(URL, TMP / "loop.bin", SIZE)
    except RuntimeError as err:
        assert "from the start" in str(err), err
    else:
        raise AssertionError("вечный круг обязан был кончиться ошибкой")
    assert Handler.hits == core.RETRIES + 2, f"подходов {Handler.hits}, ждали {core.RETRIES + 2}"


@case
def a_write_error_is_not_a_dropped_connection():
    """Локальная ошибка записи - это не обрыв связи, и повторять её незачем.

    open() отдаёт тот же OSError, что и сокет, и папка только для чтения уходила
    в пять подходов по пять секунд, а в конце жаловалась на связь.
    """
    serve("whole")
    dest = TMP / "readonly.bin"
    core.part_path(dest).mkdir()  # в папку не запишешь, а ошибка тем же OSError
    notes = []
    try:
        core.fetch(URL, dest, SIZE, on_note=notes.append)
    except RuntimeError as err:
        assert "cannot write" in str(err), err
    else:
        raise AssertionError("невозможная запись обязана была кончиться ошибкой")
    assert not notes, f"локальную ошибку разбирали как обрыв связи: {notes}"


@case
def a_broken_manifest_never_shows_a_python_traceback():
    """Любая кривизна в models.json обязана всплывать одним ValueError.

    Запись без dest давала KeyError, groups списком - AttributeError, а size
    строкой не давал ничего и ронял окно уже на сложении размеров. Ни то, ни
    другое, ни третье не ловилось except (OSError, ValueError) в install.py:
    человек, который этот же файл руками и правил, получал трассировку Python.
    """
    import copy

    good = {
        "comfyui_root": "C:/ComfyUI",
        "lmstudio": [{"search": "s/m", "quant": "Q4", "files": [{"name": "m.gguf", "size": 1}]}],
        "groups": {"g": {"title": "t", "files": [
            {"repo": "r", "path": "p", "dest": "models/a.bin", "size": 1}]}},
    }
    core.check_manifest(good)  # эталон обязан проходить

    breakage = {
        "нет dest": lambda m: m["groups"]["g"]["files"][0].pop("dest"),
        "dest мимо папки": lambda m: m["groups"]["g"]["files"][0].update(dest="C:/evil.bin"),
        "groups списком": lambda m: m.update(groups=[]),
        "groups пустой": lambda m: m.update(groups={}),
        "size строкой": lambda m: m["groups"]["g"]["files"][0].update(size="1"),
        "size нулём": lambda m: m["groups"]["g"]["files"][0].update(size=0),
        "size логическим": lambda m: m["groups"]["g"]["files"][0].update(size=True),
        "нет repo": lambda m: m["groups"]["g"]["files"][0].pop("repo"),
        "нет comfyui_root": lambda m: m.pop("comfyui_root"),
        "нет title": lambda m: m["groups"]["g"].pop("title"),
        "пустая группа": lambda m: m["groups"]["g"].update(files=[]),
        "группа строкой": lambda m: m["groups"].update(g="ой"),
        "lmstudio без quant": lambda m: m["lmstudio"][0].pop("quant"),
        "lmstudio с size строкой": lambda m: m["lmstudio"][0]["files"][0].update(size="1"),
        "манифест списком": None,
    }
    for name, damage in breakage.items():
        broken = [] if damage is None else copy.deepcopy(good)
        if damage is not None:
            damage(broken)
        try:
            core.check_manifest(broken)
        except ValueError:
            continue
        raise AssertionError(f"кривой манифест прошёл: {name}")


@case
def the_browsed_folder_wins_over_the_manifest():
    """Папку из «Обзора» знало только окно, а install.py каждый раз начинал с пути
    в models.json: одна и та же команда у окна и у консоли считала установленными
    разные файлы. Порядок теперь один на обоих и живёт в comfy_root()."""
    manifest = {"comfyui_root": "C:/FromManifest"}
    was = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(TMP / "appdata")
    try:
        assert core.comfy_root(manifest) == Path("C:/FromManifest")
        core.remember_root("C:/FromBrowse")
        assert core.comfy_root(manifest) == Path("C:/FromBrowse")
        os.environ["COMFYUI_ROOT"] = "C:/FromEnv"
        assert core.comfy_root(manifest) == Path("C:/FromEnv")
        assert core.comfy_root(manifest, "C:/FromFlag") == Path("C:/FromFlag")
        # Мусор в settings.json правят руками, и .get() на списке ронял окно
        # прямо в __init__ - жалобой на нечитаемый models.json, где всё цело.
        core.settings_path().write_text("[1, 2]", encoding="utf-8")
        assert core.saved_root() is None
    finally:
        os.environ.pop("COMFYUI_ROOT", None)
        if was is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = was


# ------------------------------------------------------------------- прогон

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="installer-tests-"))
URL = None


def main():
    global URL
    core.wait_before_retry = lambda seconds, should_stop: None  # не ждём по пять секунд

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    URL = f"http://127.0.0.1:{server.server_address[1]}/model.safetensors"

    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"ПРОВАЛ  {fn.__name__}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            print(f"ок      {fn.__name__}")

    server.shutdown()
    # Гоняются они перед каждой сборкой, и каждый прогон оставлял в %TEMP%
    # папку на сотню килобайт. За полгода это заметная куча ни для кого.
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(CASES) - failed} из {len(CASES)} прошло")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
