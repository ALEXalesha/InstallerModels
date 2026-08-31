#!/usr/bin/env python3
"""Проверки на всё, что уже ломалось. Запуск: python tests.py

Зависимостей нет нарочно: build.py гоняет их перед сборкой, а сборка идёт на
голом Python. Сеть тоже не нужна - Hugging Face изображает локальный сервер,
которому можно велеть рвать соединение когда захочется.
"""

import gc
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
import tests_matrix

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
    seen = {}   # заголовки последнего запроса
    # Потолок с большим запасом: самая длинная честная проверка тут - докачка
    # кусками по 7000 байт, это 15 подходов.
    bound = 40

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits += 1
        Handler.seen = dict(self.headers)

        # Сломай в core.py счётчик перезапусков - и проверка на вечный круг
        # закрутится вечно сама. Это хуже проваленной проверки: build.py гоняет
        # прогон перед сборкой и повис бы навсегда, без единого сообщения.
        # У зависания нет кода возврата. Обрываем неретраибельным 404 - и
        # зависание превращается во внятный провал.
        if Handler.hits > Handler.bound:
            self.reply(404)
            return

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
    Handler.seen = {}


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
def a_size_never_outgrows_the_unit_next_to_it():
    """Единица выбиралась по неокруглённому числу, а печаталось округлённое.

    1023.6 байта выходило как "1024 B", 1048570 - как "1024.0 KiB": число уже
    переросло свою подпись. Видно это было на скорости - она дробная и через
    human() идёт всегда, и в окне, и в консоли.
    """
    assert core.human(1023.6) == "1.0 KiB", core.human(1023.6)
    assert core.human(1048570) == "1.0 MiB", core.human(1048570)
    assert core.human(1024 ** 3 - 1) == "1.0 GiB", core.human(1024 ** 3 - 1)
    for value in (0, 1, 1023, 1024, 5.5, SIZE, 30 * 1024 ** 3):
        number = float(core.human(value).split()[0])
        assert abs(number) < 1024, f"{value} напечаталось как {core.human(value)}"


@case
def a_dest_windows_stores_under_another_name_is_refused():
    """Windows принимает такие имена, но хранит их не так, как написано.

    "CON.bin" - это консоль, а не файл: запись уходит в никуда и не жалуется, на
    диске потом ничего нет, и файл на 30 ГиБ качается заново каждый запуск.
    Хвостовые пробелы и точки Windows молча срезает - файл ложится под другим
    именем, и status() его не находит. Ни то, ни другое не выдумка на будущее:
    README прямо зовёт править models.json руками.
    """
    bad = ["models/CON.bin", "models/nul", "models/COM1.safetensors",
           "models/x.bin ", "models/x.bin.", "models/lora?.bin",
           "models/a|b.bin", "models/lpt9.gguf"]
    for dest in bad:
        try:
            core.dest_parts(dest)
        except ValueError:
            continue
        raise AssertionError(f"кривой dest прошёл: {dest!r}")
    # А обычные имена трогать нельзя: точки внутри имени - это норма.
    for dest in ("models/vae/qwen_image_vae.safetensors",
                 "models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors",
                 "models/loras/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors",
                 "models/unet/console.gguf"):
        core.dest_parts(dest)


@case
def check_can_be_asked_about_one_group():
    """"--check ltx" молча проверял все 95 ГиБ, а "--check ltxx" с опечаткой -
    тоже все, и про опечатку не говорил никто: keys до cmd_check не доходили."""
    import contextlib
    import io

    import install

    manifest = core.load_manifest(HERE / "models.json")
    root = TMP / "empty-comfy"
    root.mkdir(exist_ok=True)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        install.cmd_check(manifest, root, ["sdxl"])
    text = out.getvalue()
    assert "sdxl" in text, text
    assert "hunyuan3d" not in text, "спросили про одну группу, проверил все"

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        install.cmd_check(manifest, root)
    for name in manifest["groups"]:
        assert name in out.getvalue(), f"без названий групп {name} обязан попасть в отчёт"


@case
def the_readme_links_to_docs_that_exist():
    """README едет рядом с exe и ссылается на docs/. Раньше туда клался он один,
    без docs/, и у человека с установленной программой половина ссылок вела в
    пустоту, а скриншоты не открывались. Теперь папку кладёт build.py - и здесь
    же проверяется, что класть есть что."""
    import re
    readme = (HERE / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\((docs/[^)]+)\)", readme)
    assert links, "в README не осталось ссылок на docs/ - проверка потеряла смысл"
    missing = [link for link in links if not (HERE / link).exists()]
    assert not missing, f"README ссылается на то, чего нет: {missing}"


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

    Теперь такая помеха видна ещё раньше: до сети программа смотрит, во что
    вообще собирается писать, и на сервер не ходит вовсе.
    """
    serve("whole")
    dest = TMP / "readonly.bin"
    core.part_path(dest).mkdir()  # в папку не запишешь, а ошибка тем же OSError
    notes = []
    try:
        core.fetch(URL, dest, SIZE, on_note=notes.append)
    except RuntimeError as err:
        assert "is not a file" in str(err), err
    else:
        raise AssertionError("невозможная запись обязана была кончиться ошибкой")
    assert not notes, f"локальную ошибку разбирали как обрыв связи: {notes}"
    assert Handler.hits == 0, "за помехой на диске незачем ходить в сеть"


@case
def a_folder_named_like_the_file_is_not_a_damaged_file():
    """Папка с именем нужного файла - это не «битый файл» на её размер.

    exists() отвечал «да» и на папку, и status() выдавал ("damaged", 0). Лежащий
    рядом .part при этом не замечался вовсе, а вся закачка доходила до самой
    последней строки и падала сырым OSError из os.replace - после того как
    тридцать гигабайт уже скачаны. Теперь помеха видна сразу и по имени.
    """
    root = TMP / "blocked"
    entry = {"dest": "models/vae/busy.bin", "size": SIZE}
    (root / "models/vae/busy.bin").mkdir(parents=True)

    assert core.status(entry, root) == ("missing", 0), "папка выдавалась за файл"
    core.part_path(root / entry["dest"]).write_bytes(BODY[:300])
    assert core.status(entry, root) == ("partial", 300), ".part за папкой не разглядели"

    serve("whole")
    try:
        core.fetch(URL, root / entry["dest"], SIZE)
    except RuntimeError as err:
        assert "is not a file" in str(err), err
    else:
        raise AssertionError("папка на месте файла обязана была кончиться ошибкой")
    assert Handler.hits == 0, "за помехой на диске незачем ходить в сеть"


@case
def a_file_where_a_folder_belongs_is_named_out_loud():
    """models - файл, а не папка: mkdir падал сырым OSError мимо всех сообщений."""
    root = TMP / "notadir"
    root.mkdir()
    (root / "models").write_text("я не папка", encoding="utf-8")
    serve("whole")
    try:
        core.fetch(URL, root / "models/vae/x.bin", SIZE)
    except RuntimeError as err:
        assert "cannot create folder" in str(err), err
    else:
        raise AssertionError("файл на месте папки обязан был кончиться ошибкой")
    assert Handler.hits == 0, "за помехой на диске незачем ходить в сеть"


@case
def an_open_destination_does_not_lose_the_download():
    """Запущенный ComfyUI держит .safetensors открытым, и Windows не даёт
    переименовать поверх него. Это была единственная незакрытая строка функции:
    скачанные гигабайты кончались сырым [WinError 5] без единого слова о том,
    что закрыть, а на глаз выглядело как «скачалось и пропало». Файл при этом
    цел, лежит в .part, и следующий запуск обязан его подхватить.
    """
    serve("whole")
    dest = TMP / "busy.bin"
    dest.write_bytes(BODY[:10])          # старый файл на месте, его и держат
    core.part_path(dest).write_bytes(BODY)  # а новый уже скачан целиком

    holder = open(dest, "rb")
    try:
        core.fetch(URL, dest, SIZE)
    except RuntimeError as err:
        assert "cannot put it in place" in str(err), err
        assert "ComfyUI" in str(err), "в сообщении не сказано, что закрывать"
    else:
        raise AssertionError("занятый файл обязан был кончиться ошибкой")
    finally:
        holder.close()

    assert core.part_path(dest).read_bytes() == BODY, "скачанное потеряли"
    assert Handler.hits == 0, "целый .part качать заново незачем"

    core.fetch(URL, dest, SIZE)  # ComfyUI закрыли - второй заход обязан доложить
    assert dest.read_bytes() == BODY


@case
def the_download_asks_for_no_compression():
    """Сожми прокси ответ на лету - и Content-Length станет размером архива.
    Программа объявила бы models.json устаревшим и назвала бы «правильный»
    размер, которого нет, а докачка по Range поехала бы по чужим смещениям."""
    serve("whole")
    core.fetch(URL, TMP / "plain.bin", SIZE)
    assert Handler.seen.get("Accept-Encoding") == "identity", Handler.seen


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


# ------------------------------------------------------- консоль, сборка, NSIS

@case
def the_command_line_answers_every_flag():
    """Разбор команды не проверялся никак: под проверкой была пятая часть
    install.py, и вся она приходилась на общий с окном core.py. Коды возврата
    расписаны в README, по ним пишут скрипты - а сверял их до сих пор никто."""
    import contextlib
    import io

    import install

    root = TMP / "cli-root"
    root.mkdir(exist_ok=True)
    было_argv, было_root = sys.argv, os.environ.get("COMFYUI_ROOT")
    os.environ["COMFYUI_ROOT"] = str(root)

    # команда -> код возврата, что обязано быть в выводе, чего быть не должно
    table = [
        ([],                     0, ["usage:", "everything:"],            []),
        (["--list"],             0, ["everything:", "ltx"],               []),
        (["--lmstudio"],         0, ["lms get", "model key in LM Studio"], []),
        (["--check"],            1, ["sdxl", "hunyuan3d", "need downloading"], []),
        (["--check", "sdxl"],    1, ["sdxl"],                             ["hunyuan3d"]),
        (["--check", "ltxx"],    1, ["unknown group"],                    []),
        (["--dry-run", "sdxl"],  0, ["to download", "sdxl_vae"],          []),
        (["нетакой"],            1, ["unknown group", "available:"],      []),
        (["--root", str(TMP / "нет-папки"), "sdxl"], 1, ["folder not found"], []),
    ]
    try:
        for argv, code, must, must_not in table:
            out = io.StringIO()
            sys.argv = ["install.py"] + argv
            with contextlib.redirect_stdout(out):
                got = install.main()
            text = out.getvalue()
            assert got == code, f"{argv}: код {got}, ждали {code}\n{text}"
            for needle in must:
                assert needle in text, f"{argv}: в выводе нет {needle!r}\n{text}"
            for needle in must_not:
                assert needle not in text, f"{argv}: в выводе лишнее {needle!r}\n{text}"
    finally:
        sys.argv = было_argv
        if было_root is None:
            os.environ.pop("COMFYUI_ROOT", None)
        else:
            os.environ["COMFYUI_ROOT"] = было_root


@case
def the_command_line_downloads_reports_and_stops_early():
    """Установка из консоли от начала до конца: скачали, повторили, сломали.

    Раньше эта дорога не проходилась ни разу: проверялся core.fetch, а всё, что
    вокруг него в install.py - очередь, пропуск уже готового, проверка места,
    список неудачных и код возврата - держалось на честном слове.
    """
    import contextlib
    import io

    import install

    root = TMP / "cli-install"
    root.mkdir(exist_ok=True)
    manifest = {"comfyui_root": str(root), "groups": {"g": {"title": "T", "files": [
        {"dest": "models/vae/cli.bin", "repo": "r", "path": "p", "size": SIZE}]}}}
    было_url, было_shutil = install.hf_url, install.shutil

    def run(dry=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install.cmd_install(manifest, root, ["g"], dry)
        return code, out.getvalue()

    try:
        install.hf_url = lambda repo, path: URL

        serve("whole")
        code, text = run()
        assert code == 0, text
        assert (root / "models/vae/cli.bin").read_bytes() == BODY, "файл не лёг на место"

        # Второй заход по той же группе: качать нечего, и это не ошибка.
        code, text = run()
        assert code == 0 and "nothing to download" in text, text
        assert "skip (already there)" in text, text

        # Сломанный репозиторий: код 1, список неудачных и совет повторить.
        serve("gone")
        (root / "models/vae/cli.bin").unlink()
        code, text = run()
        assert code == 1, text
        assert "FAILED" in text and "run the same command again" in text, text

        # Мало места - в сеть не идём вовсе.
        serve("whole")
        Handler.hits = 0

        class Полный:
            @staticmethod
            def disk_usage(path):
                return type("U", (), {"free": 0})()

        install.shutil = Полный
        code, text = run()
        assert code == 1 and "not enough free space" in text, text
        assert Handler.hits == 0, "места нет, а в сеть всё равно сходили"
    finally:
        install.hf_url, install.shutil = было_url, было_shutil


@case
def the_progress_line_never_breaks():
    """Полоска в консоли рисуется поверх самой себя, и её ширина - часть
    рисунка. "999999:00" при смешной скорости в начале файла и "-1:-30", когда
    done обгонял total, разъезжали строку и оставляли на экране мусор."""
    import contextlib
    import io
    import re as regex

    import install

    bar = install.Progress()
    hard = [(0, SIZE, 0.001),        # скорость по первым байтам, время в сутках
            (SIZE, SIZE, 1e9),       # мгновенно
            (SIZE + 5000, SIZE, 100),  # .part больше, чем сказано в манифесте
            (0, 0, 0),               # пустая очередь
            (50, 100, -5)]           # отрицательная скорость
    for done, total, speed in hard:
        bar.next_step = 0            # заставляем печатать каждый раз
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bar.update(done, total, speed)
        line = out.getvalue()
        assert "ETA" in line, f"{done}/{total}: строка без ETA: {line!r}"
        clock = line.split("ETA")[1].strip().split()[0]
        assert regex.match(r"^\d{1,3}:\d{2}$", clock), \
            f"{done}/{total} на скорости {speed}: часы {clock!r} разъедут строку"


@case
def the_build_refuses_a_broken_project():
    """Проверки перед сборкой сами не проверялись ничем.

    Их шесть, и каждая стоит между кривым файлом и собранным exe. Проверка,
    которая молча пропускает то, что должна ловить, хуже отсутствующей: на неё
    рассчитывают. Тут они гоняются на целой копии проекта, а потом на такой же,
    но подпорченной ровно в том месте, за которое каждая отвечает.
    """
    import build

    good = TMP / "проект"
    shutil.rmtree(good, ignore_errors=True)
    shutil.copytree(HERE, good, ignore=shutil.ignore_patterns(
        "dist", "build", "__pycache__", ".git", ".remember"))

    было = build.HERE
    try:
        build.HERE = good
        for check in (build.check_docs, build.check_nsi_encoding,
                      build.check_window_title, build.check_version,
                      build.check_manifest):
            check()   # на целом проекте все обязаны молчать

        def без_title_ru(raw):
            tree = json.loads(raw.decode("utf-8"))
            for group in tree["groups"].values():
                group.pop("title_ru", None)
            return json.dumps(tree, ensure_ascii=False).encode("utf-8")

        def без_lmstudio(raw):
            tree = json.loads(raw.decode("utf-8"))
            tree.pop("lmstudio", None)
            return json.dumps(tree, ensure_ascii=False).encode("utf-8")

        damage = [
            ("README.md", lambda raw: raw + "\n[дыра](docs/нет-такого.md)\n".encode("utf-8"),
             build.check_docs, "ссылка из README в никуда"),
            ("setup.nsi", lambda raw: raw[3:],
             build.check_nsi_encoding, "у setup.nsi отняли BOM"),
            ("setup.nsi", lambda raw: raw.replace(
                f'!define VERSION "{build.VERSION}"'.encode("utf-8"),
                b'!define VERSION "0.0.0"'),
             build.check_version, "версии разъехались"),
            ("gui.py", lambda raw: raw.replace(
                b'window.title("', b'window.title("\xd0\xa7\xd1\x83\xd0\xb6\xd0\xbe\xd0\xb5 '),
             build.check_window_title, "заголовок окна разъехался с установщиком"),
            ("models.json", без_title_ru, build.check_manifest, "у групп нет title_ru"),
            ("models.json", без_lmstudio, build.check_manifest, "пропал раздел lmstudio"),
        ]
        for name, mangle, check, what in damage:
            path = good / name
            original = path.read_bytes()
            try:
                path.write_bytes(mangle(original))
                try:
                    check()
                except SystemExit:
                    continue
                raise AssertionError(f"{what}: проверка промолчала и пустила бы это в exe")
            finally:
                path.write_bytes(original)
    finally:
        build.HERE = было
        shutil.rmtree(good, ignore_errors=True)


@case
def the_window_builds_and_survives_every_event():
    """Окно не проверялось ни одной строкой: 332 строки, ноль.

    Арифметика полосок уехала в core.QueueProgress и перебирается отдельно, а
    тут остаётся обвязка: сборка виджетов, насос событий, три исхода закачки и
    замок на кнопках. Ломается она молча - окно просто не открывается или
    остаётся с заблокированной кнопкой, и узнать об этом можно было только
    запустив exe руками после сборки.

    Диалоги подменяем: настоящий messagebox остановил бы прогон намертво.
    """
    import tkinter as tk

    import gui

    показано = []

    class Диалоги:
        @staticmethod
        def showinfo(title, text=""):
            показано.append(("инфо", title))

        @staticmethod
        def showerror(title, text=""):
            показано.append(("ошибка", title))

        @staticmethod
        def showwarning(title, text=""):
            показано.append(("предупреждение", title))

        @staticmethod
        def askyesno(title, text=""):
            показано.append(("вопрос", title))
            return False

    root = TMP / "gui-root"
    root.mkdir(exist_ok=True)
    было_окно, было_root = gui.messagebox, os.environ.get("COMFYUI_ROOT")
    os.environ["COMFYUI_ROOT"] = str(root)
    gui.messagebox = Диалоги

    window = None
    try:
        try:
            window = tk.Tk()
        except tk.TclError as err:
            raise AssertionError(f"Tk не поднялся, окно не собрать: {err}") from None
        window.withdraw()
        app = gui.App(window)

        # Собралось ли то, что описано в models.json.
        assert set(app.rows) == set(app.manifest["groups"]), "строки групп разъехались"
        app.select(True)
        assert app.chosen_keys() == list(app.manifest["groups"])
        app.select_missing()
        app.select(False)
        assert app.chosen_keys() == []

        # Папка есть, папки нет - оба вида должны переживаться без исключений.
        app.refresh()
        assert "свободно" in app.disk_label.cget("text")
        app.root_path.set(str(TMP / "нет-такой-папки"))
        app.apply_typed_root()
        assert app.disk_label.cget("text") == "папка не найдена"
        app.root_path.set(str(root))
        app.apply_typed_root()

        # Насос событий обязан пережить что угодно: пока он крутится, окно живо.
        bars = core.QueueProgress([SIZE, SIZE], [0, 0])
        bars.start_file(0)
        app.events.put(("log", "строка в лог"))
        app.events.put(("file", "какой-то файл"))
        app.events.put(("progress", bars.advance(SIZE // 2) + (1000.0,)))
        app.events.put(("мусор, которого не бывает", None))
        app.drain_events()
        assert app.file_bar["value"] == 500, app.file_bar["value"]
        assert "осталось" in app.speed_label.cget("text")

        # Три исхода закачки: успех, часть не скачалась, отмена с провалами.
        # Перед каждым запираем окно ровно так, как это делает start(). Без
        # этого проверка «кнопка разблокировалась» ничего не значит: она и не
        # была заперта, и убери из finish_job строку, которая её отпускает, -
        # проверка всё равно останется зелёной. Так и вышло с первого раза.
        показано.clear()
        for failed, cancelled in (([], False),
                                  (["models/vae/x.bin"], False),
                                  (["models/vae/x.bin"], True)):
            app.lock_controls(True)
            app.download_button.configure(state="disabled")
            app.cancel_button.configure(state="normal")
            app.finish_job(failed, cancelled)
            # str() тут обязателен: ttk отдаёт из cget не строку, а объект Tcl,
            # и сравнение с "normal" молча оказывается ложным всегда.
            assert str(app.download_button.cget("state")) == "normal", \
                "кнопка «Скачать» осталась запертой - окно больше ничего не умеет"
            assert str(app.cancel_button.cget("state")) == "disabled", \
                "«Отмена» осталась живой, хотя качать уже нечего"
        assert [kind for kind, _ in показано] == ["инфо", "предупреждение", "предупреждение"], \
            показано

        # Замок на время закачки: галочки и кнопки выбора запираются вместе.
        app.lock_controls(True)
        assert str(app.browse_button.cget("state")) == "disabled"
        assert all(str(r.box.cget("state")) == "disabled" for r in app.rows.values())
        app.lock_controls(False)
        assert str(app.browse_button.cget("state")) == "normal"

        # Старт без выбора и старт в несуществующую папку: оба обязаны
        # объясниться диалогом, а не уйти качать.
        показано.clear()
        app.start()
        assert показано == [("инфо", "Нечего качать")], показано
        показано.clear()
        app.select(True)
        app.root_path.set(str(TMP / "нет-такой-папки"))
        app.start()
        assert показано == [("ошибка", "Папка не найдена")], показано
        assert app.worker is None, "ушёл качать в несуществующую папку"

        app.closing = True
    finally:
        gui.messagebox = было_окно
        if было_root is None:
            os.environ.pop("COMFYUI_ROOT", None)
        else:
            os.environ["COMFYUI_ROOT"] = было_root
        if window is not None:
            # Переменные Tk (BooleanVar галочек, StringVar пути) на разрушении
            # окна не исчезают - их прибирает сборщик мусора, уже когда Tk
            # мёртв, и каждая печатает "main thread is not in main loop".
            # Прогон от этого не падает, но экран засыпается трассировками, а
            # код возврата становится ненадёжным - и сборка отказывается идти.
            # Роняем их руками, пока Tk ещё жив.
            for row in app.rows.values():
                row.picked = None
            app.rows.clear()
            app.root_path = None
            gc.collect()
            window.destroy()
            gc.collect()


@case
def the_installer_script_holds_together():
    """setup.nsi сверялся на BOM, заголовок и версию - три строки из ста
    девяноста. Остальное держалось на том, что makensis не ругается, а он и не
    обязан: зовущий несуществующую функцию скрипт собирается молча.
    """
    import re as regex

    text = (HERE / "setup.nsi").read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    # Каждый Call обязан кому-то соответствовать. Макрос RunningCheck порождает
    # сразу две функции - обычную и un.-шную, для деинсталлятора.
    defined = set()
    for name in regex.findall(r"^Function\s+(\S+)", text, regex.M):
        if "${un}" in name:
            defined |= {name.replace("${un}", ""), name.replace("${un}", "un.")}
        else:
            defined.add(name)
    called = set(regex.findall(r"^\s*Call\s+(\S+)", text, regex.M))
    assert called and called <= defined, f"зовут несуществующее: {sorted(called - defined)}"

    # Рекурсивное удаление папки установки обязано стоять под защитой: $INSTDIR
    # приходит из реестра, и RMDir /r по чужому пути - это уже не удаление
    # программы. Проверяем не текст комментария, а строку прямо перед ним.
    guarded = 0
    for n, line in enumerate(lines):
        if line.strip() != 'RMDir /r "$INSTDIR"':
            continue
        before = ""
        for previous in reversed(lines[:n]):
            if previous.strip() and not previous.strip().startswith(";"):
                before = previous.strip()
                break
        assert before.startswith('IfFileExists "$INSTDIR\\${APP}.exe"'), \
            f"строка {n + 1}: RMDir /r по $INSTDIR без проверки, что папка наша: {before!r}"
        guarded += 1
    assert guarded == 1, f"ожидали одно защищённое RMDir /r по $INSTDIR, нашли {guarded}"

    # Страница выбора папки обязана иметь проверку, иначе установщика пустят в
    # чужую непустую папку, а деинсталлятор потом снесёт её целиком.
    заметки = [ln.strip() for ln in lines
               if ln.strip() and not ln.strip().startswith(";")]
    where = заметки.index("!insertmacro MUI_PAGE_DIRECTORY")
    assert заметки[where - 1] == "!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckInstallDir", \
        f"перед страницей выбора папки нет проверки, а есть {заметки[where - 1]!r}"

    # Описания у разделов: и раздел, и строка перевода обязаны существовать.
    sections = set(regex.findall(r'^Section\s+"[^"]*"\s+(\w+)', text, regex.M))
    described = set(regex.findall(r"MUI_DESCRIPTION_TEXT \$\{(\w+)\}", text))
    assert described <= sections, f"описание у несуществующего раздела: {described - sections}"
    langstrings = set(regex.findall(r"^LangString\s+(\w+)", text, regex.M))
    used = {name for name in regex.findall(r"\$\((\w+)\)", text) if name.startswith("DESC_")}
    assert used <= langstrings, f"нет перевода для: {sorted(used - langstrings)}"

    # Запомненная папка ComfyUI лежит вне $INSTDIR, и деинсталлятор стирает её
    # по жёстко записанному пути. Поменяется settings_path() в core.py - файл
    # переживёт удаление и всплывёт при следующей установке как чужая настройка.
    # Обе стороны выводим, а не вписываем: иначе проверка сама и разъедется.
    app = regex.search(r'!define APP\s+"([^"]*)"', text).group(1)
    было = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = "C:/AppData"
    try:
        tail = core.settings_path().as_posix()[len("C:/AppData/"):]
    finally:
        if было is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = было
    assert tail == f"{app}/settings.json", f"core.py пишет настройки в {tail}"
    assert 'Delete "$LOCALAPPDATA\\${APP}\\settings.json"' in text, \
        "деинсталлятор не стирает запомненную папку ComfyUI"


# ------------------------------------------------------------------- прогон

# Матрица лежит отдельно, потому что устроена наоборот: тут список поломок,
# которые уже случались, там перебор пространства с проверкой инвариантов.
# Регистрируем её теми же case(), чтобы прогон и отчёт остались одни на всех.
for check in tests_matrix.CASES:
    case(check)

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
            checked = fn()
        except Exception:
            failed += 1
            print(f"ПРОВАЛ  {fn.__name__}")
            print("        " + traceback.format_exc().strip().replace("\n", "\n        "))
        else:
            # Проверки из матрицы возвращают, сколько клеток обошли. Без этого
            # три строчки в отчёте выглядели бы как три проверки, хотя за ними
            # стоит больше семисот.
            print(f"ок      {fn.__name__}" + (f"  ({checked} клеток)"
                                              if isinstance(checked, int) else ""))

    server.shutdown()
    # Гоняются они перед каждой сборкой, и каждый прогон оставлял в %TEMP%
    # папку на сотню килобайт. За полгода это заметная куча ни для кого.
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(CASES) - failed} из {len(CASES)} прошло")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
