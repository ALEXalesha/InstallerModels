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
import time
import traceback
from pathlib import Path

import core
import tests_matrix

BODY = bytes(range(256)) * 400  # 102400 байт
SIZE = len(BODY)
# Сколько сервер молчит в режиме "stall". Нарочно много больше таймаута клиента:
# только так видно, кто первым сдался. Если клиент дождётся закрытия соединения
# сервером, а не оборвёт молчание сам, разница будет в секундах, и её видно.
STALL = 3.0

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
    tree = []      # что отдаёт опись репозитория: список записей или код ошибки
    pages = None   # пара страниц, когда проверяем постраничную выдачу
    base = ""      # адрес самого макета, для ссылки на следующую страницу

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits += 1
        Handler.seen = dict(self.headers)

        # Опись репозитория для --sync-manifest. Отдаётся отдельной ручкой и
        # к скачиванию отношения не имеет, поэтому и разбирается до режимов.
        if self.path.startswith("/api/"):
            self.send_listing()
            return

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
        if Handler.mode == "stall":   # соединение живо, но молчит и не закрывается
            time.sleep(STALL)
            self.close_connection = True
            return
        self.wfile.write(BODY[start:])

    def send_listing(self):
        """Изображает api/models/РЕПО/tree/main. Что отдавать - в Handler.tree:
        либо список записей, либо код ошибки, либо две страницы для проверки
        постраничной выдачи."""
        page = Handler.tree
        if isinstance(page, int):
            self.reply(page)
            return
        if Handler.pages and "page=2" not in self.path:
            page, rest = Handler.pages
            body = json.dumps(page).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Link", f'<{Handler.base}/api/x?page=2>; rel="next"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if Handler.pages and "page=2" in self.path:
            page = Handler.pages[1]
        body = json.dumps(page).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


class ThreadedServer(socketserver.ThreadingTCPServer):
    """Для проверки на молчащий сервер: обработчик там спит секундами, и на
    однопоточном макете этот сон останавливал бы и очередь, и выключение."""
    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


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
def a_stalled_connection_is_cut_by_the_timeout():
    """Замолчавший сервер: соединение живо, а байт из него нет и не будет.

    В документации было написано, что такое не ловится, - и это оказалось
    неправдой. Ловит таймаут сокета: он отсчитывается от каждого чтения, и
    молчание дольше TIMEOUT прилетает обычным OSError, то есть уходит в те же
    повторы, что и обрыв. Проверить это было нечем, потому что шестьдесят
    секунд были вписаны прямо в вызов. Теперь они вынесены в core.TIMEOUT, и
    заодно стало видно, что за число и зачем оно.

    Не ловится по-прежнему другое, и разница тут существенная: сервер, который
    капает по байту раз в полминуты. Каждый байт заводит таймаут заново, файл
    честно растёт, счётчик обрывов честно обнуляется. Ограничить закачку
    целиком нельзя - файл на 30 ГиБ по медленной связи идёт часами и выглядит
    точно так же.
    """
    # Сервер тут свой, отдельный, и это не прихоть. Макет однопоточный: пока
    # обработчик спит, следующее соединение ждёт в очереди, и клиент успевает
    # бросить его по таймауту. Доезжает оно уже во время следующей проверки и
    # накручивает ей счётчик запросов - на общем сервере из-за этого падала
    # соседняя проверка, а виноватой выглядела она.
    свой = ThreadedServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=свой.serve_forever, daemon=True).start()
    адрес = f"http://127.0.0.1:{свой.server_address[1]}/model.safetensors"

    serve("stall")
    dest = TMP / "stall.bin"
    core.part_path(dest).write_bytes(BODY[:1000])
    было_timeout, было_retries = core.TIMEOUT, core.RETRIES
    core.TIMEOUT, core.RETRIES = 0.2, 2      # иначе прогон встанет на полминуты
    started = time.monotonic()
    try:
        core.fetch(адрес, dest, SIZE)
    except RuntimeError as err:
        assert "got" in str(err), err
    else:
        raise AssertionError("молчащее соединение обязано было кончиться ошибкой")
    finally:
        core.TIMEOUT, core.RETRIES = было_timeout, было_retries
        свой.shutdown()
        свой.server_close()   # роняем и очередь брошенных соединений
        serve("whole")        # счётчик общего сервера этой проверки не касается

    # Порог ниже, чем молчит сервер: уложились - значит оборвали молчание сами,
    # а не дождались, пока сервер закроет соединение. Иначе проверка проходила
    # бы и с вовсе убранным таймаутом.
    прошло = time.monotonic() - started
    assert прошло < STALL, \
        f"провисели {прошло:.1f} с при молчании {STALL} с - таймаут не сработал"
    assert core.part_path(dest).stat().st_size == 1000, "недокачанное потеряли"
    assert not dest.exists(), "молчание сервера не повод объявить файл готовым"


@case
def a_checksum_catches_what_the_size_cannot():
    """Совпадение размера перестаёт быть единственным доказательством.

    Файл, побитый на диске или собранный из двух ревизий модели с одинаковым
    размером, до сих пор проходил как целый: программа смотрела только на число
    байт. Сумма считается на лету, пока байты и так текут мимо, - отдельным
    проходом по файлу это стоило бы чтения всех 95 ГиБ.
    """
    import hashlib

    верная = hashlib.sha256(BODY).hexdigest()

    # Сходится - файл встаёт на место как обычно.
    serve("whole")
    dest = TMP / "sum-ok.bin"
    core.fetch(URL, dest, SIZE, sha256=верная)
    assert dest.read_bytes() == BODY

    # Не сходится - файл на место не встаёт, и .part не стирается: отличить
    # побитый файл от устаревшей суммы программа не может, а стереть вслепую
    # значит выбросить гигабайты по догадке.
    serve("whole")
    плохой = TMP / "sum-bad.bin"
    try:
        core.fetch(URL, плохой, SIZE, sha256="f" * 64)
    except RuntimeError as err:
        assert "sha256 не сошёлся" in str(err), err
        assert верная in str(err), "в ошибке нет того, что получилось на деле"
        assert плохой.name + ".part" in str(err), "не сказано, что удалять"
    else:
        raise AssertionError("несовпадение суммы обязано было всплыть")
    assert not плохой.exists(), "файл с чужой суммой нельзя выдавать за готовый"
    assert core.part_path(плохой).read_bytes() == BODY, "скачанное стёрли"

    # Без суммы в манифесте всё как раньше: ни проверки, ни расхода.
    serve("whole")
    просто = TMP / "sum-none.bin"
    core.fetch(URL, просто, SIZE)
    assert просто.read_bytes() == BODY


@case
def a_checksum_survives_a_resume_without_rereading_everything():
    """Докачка не должна ни ломать сумму, ни перечитывать файл каждый обрыв.

    Наивный подсчёт пересчитывал бы начало файла после каждого обрыва: на
    тридцати гигабайтах и сотне обрывов это три терабайта лишнего чтения с
    диска. Считается только то, что легло мимо нас, и ровно один раз.
    """
    import hashlib

    верная = hashlib.sha256(BODY).hexdigest()

    # Кусок от прошлого запуска через нас не проходил - его досчитывают с диска.
    serve("whole")
    dest = TMP / "sum-resume.bin"
    core.part_path(dest).write_bytes(BODY[:40000])
    core.fetch(URL, dest, SIZE, sha256=верная)
    assert dest.read_bytes() == BODY, "докачанный файл не совпал"

    # Рваная связь: пятнадцать обрывов, и сумма всё равно верная.
    serve("flaky")
    рваный = TMP / "sum-flaky.bin"
    core.fetch(URL, рваный, SIZE, sha256=верная)
    assert рваный.read_bytes() == BODY
    assert Handler.hits == 15, f"подходов {Handler.hits}, ждали 15"

    # Целый .part от прошлого запуска: качать нечего, но через нас он не
    # проходил ни байтом, и сумму надо досчитать с диска. Это единственный
    # случай, когда за неё платят лишним чтением файла.
    serve("whole")
    целый = TMP / "sum-whole.bin"
    core.part_path(целый).write_bytes(BODY)
    core.fetch(URL, целый, SIZE, sha256=верная)
    assert целый.read_bytes() == BODY, "целый .part не встал на место"
    assert Handler.hits == 0, "целый .part качать заново незачем"

    # А если содержимое чужое - размер тот же, а сумма нет, и это ловится.
    serve("whole")
    чужой = TMP / "sum-alien.bin"
    core.part_path(чужой).write_bytes(bytes(SIZE))   # нужного размера, но не тот
    try:
        core.fetch(URL, чужой, SIZE, sha256=верная)
    except RuntimeError as err:
        assert "sha256 не сошёлся" in str(err), err
    else:
        raise AssertionError("целый .part с чужим содержимым обязан был всплыть")
    assert Handler.hits == 0, "целый .part качать заново незачем"


@case
def a_hash_in_the_manifest_must_be_a_real_hash():
    """Обрезанная или сбитая строка не поймает ни одной поломки, зато завалит
    закачку целого файла - и человек пойдёт искать беду не там."""
    good = {"repo": "r", "path": "p", "dest": "models/a.bin", "size": 1}
    core.need_hash("где-то", good)                      # без суммы - можно
    core.need_hash("где-то", dict(good, sha256="A" * 64))  # заглавные - можно
    for bad in ("", "abc", "z" * 64, "a" * 63, "a" * 65, 123, True, ["a" * 64]):
        try:
            core.need_hash("где-то", dict(good, sha256=bad))
        except ValueError:
            continue
        raise AssertionError(f"кривая сумма прошла: {bad!r}")


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


ХЕШ = {"a.bin": "a" * 64, "sub/b.bin": "b" * 64, "m.gguf": "c" * 64}


def как_на_сервере(*files, lfs=True):
    """Опись репозитория в том виде, в каком её отдаёт Hugging Face.

    Верхний oid - это git-овый sha1 блоба, а не контрольная сумма файла.
    Настоящий sha256 лежит в lfs.oid, и брать можно только его: подставь
    вместо суммы sha1, и каждая закачка станет падать на сверке.
    """
    out = []
    for path, size in files:
        item = {"type": "file", "path": path, "size": size, "oid": "0" * 40}
        if lfs:
            item["lfs"] = {"oid": ХЕШ[path], "size": size}
        out.append(item)
    return out


SYNC_MANIFEST = {
    "comfyui_root": "C:/ComfyUI",
    "groups": {"g": {"title": "T", "files": [
        {"dest": "models/vae/a.bin", "repo": "автор/репо", "path": "a.bin", "size": 100},
        {"dest": "models/vae/b.bin", "repo": "автор/репо", "path": "sub/b.bin", "size": 200},
    ]}},
    "lmstudio": [{"search": "автор/gguf", "quant": "Q4_K_M",
                  "files": [{"name": "m.gguf", "size": 300}]}],
}


@case
def the_manifest_can_be_checked_against_the_server():
    """Размеры добывались руками, по одному curl на файл, и оттого отставали.

    Отставший размер - это не мелочь: скачивание падает с «manifest is out of
    date» ещё до первого байта, и правильное число человек ищет сам. Опись
    репозитория отдаётся одной ручкой, и в ней есть и размеры, и пути.
    """
    import copy

    Handler.pages, Handler.hits = None, 0
    Handler.tree = как_на_сервере(("a.bin", 100), ("sub/b.bin", 200), ("m.gguf", 300))
    manifest = copy.deepcopy(SYNC_MANIFEST)
    drifts = core.manifest_drift(manifest)
    assert len(drifts) == 3, drifts
    assert {d.state for d in drifts} == {"ok"}, drifts
    # Один и тот же репозиторий у двух файлов - опись берётся один раз.
    assert Handler.hits == 2, f"запросов {Handler.hits}, ждали по одному на репозиторий"

    # Размер уехал: сверка это видит и правит, а вот исчезнувший путь не трогает -
    # какой файл автор имел в виду, знает только человек.
    Handler.tree, Handler.hits = как_на_сервере(("a.bin", 111), ("m.gguf", 300)), 0
    manifest = copy.deepcopy(SYNC_MANIFEST)
    drifts = core.manifest_drift(manifest)
    по_месту = {d.dest: d for d in drifts}
    assert по_месту["models/vae/a.bin"].state == "размер"
    assert по_месту["models/vae/a.bin"].now == 111
    assert по_месту["models/vae/b.bin"].state == "нет файла"
    assert по_месту["m.gguf"].state == "ok", "раздел lmstudio тоже надо сверять"

    сделано = core.apply_drift(manifest, drifts)
    assert сделано.sizes == 1, f"поправить надо было один размер, а вышло {сделано.sizes}"
    assert manifest["groups"]["g"]["files"][0]["size"] == 111
    assert manifest["groups"]["g"]["files"][1]["size"] == 200, "исчезнувший путь трогать нельзя"

    # Сумму сервер отдаёт в той же описи, за тот же запрос: считать её самим -
    # значит скачать все 95 ГиБ. Ставится она и там, где размер сошёлся.
    assert manifest["groups"]["g"]["files"][0]["sha256"] == ХЕШ["a.bin"]
    assert manifest["lmstudio"][0]["files"][0]["sha256"] == ХЕШ["m.gguf"]
    assert "sha256" not in manifest["groups"]["g"]["files"][1], \
        "у исчезнувшего файла сумму брать неоткуда"
    assert сделано.hashes == 2, f"сумм должно было лечь две, а легло {сделано.hashes}"

    # Второй заход по тому же манифесту ничего не меняет: суммы уже на месте.
    assert core.apply_drift(manifest, core.manifest_drift(manifest)) == (0, 0)

    # Мелкие файлы вне LFS суммы не имеют, и выдумывать её неоткуда.
    Handler.tree, Handler.hits = как_на_сервере(("a.bin", 100), lfs=False), 0
    голый = copy.deepcopy(SYNC_MANIFEST)
    assert core.apply_drift(голый, core.manifest_drift(голый)).hashes == 0
    assert "sha256" not in голый["groups"]["g"]["files"][0]


@case
def a_synced_manifest_stays_loadable_and_keeps_its_looks():
    """Запись не должна ни ломать манифест, ни перелопачивать файл.

    models.json правят руками, и diff после сверки обязан показывать только те
    числа, которые изменились, - иначе понять, что натворила команда, нельзя.
    """
    import copy

    path = TMP / "sync.json"
    core.save_manifest(copy.deepcopy(SYNC_MANIFEST), path)
    было = path.read_bytes()
    assert было.endswith(b"\r\n"), "манифест пишется с CRLF, как все файлы проекта"
    assert not было.startswith(b"\xef\xbb\xbf"), "у models.json BOM не было и не надо"

    # Перезапись без единой правки обязана дать те же байты.
    core.save_manifest(core.load_manifest(path), path)
    assert path.read_bytes() == было, "запись без правок изменила файл"

    # Правка одного размера меняет ровно одну строку. Сумм тут нет нарочно:
    # они не правят, а прибавляют, и это отдельный разговор ниже.
    Handler.pages = None
    Handler.tree = как_на_сервере(("a.bin", 999), ("sub/b.bin", 200), ("m.gguf", 300),
                                  lfs=False)
    manifest = core.load_manifest(path)
    core.apply_drift(manifest, core.manifest_drift(manifest))
    core.check_manifest(manifest)      # то, что пишем, обязано проходить загрузку
    core.save_manifest(manifest, path)
    стало = path.read_bytes()
    assert core.load_manifest(path)["groups"]["g"]["files"][0]["size"] == 999
    assert len(стало.split(b"\r\n")) == len(было.split(b"\r\n")), "число строк изменилось"
    разница = [(a, b) for a, b in zip(было.split(b"\r\n"), стало.split(b"\r\n")) if a != b]
    assert len(разница) == 1, f"изменилось строк: {len(разница)} - {разница[:4]}"

    # А контрольные суммы именно прибавляются - по строке на файл, и манифест
    # после этого обязан читаться как ни в чём не бывало.
    Handler.tree = как_на_сервере(("a.bin", 999), ("sub/b.bin", 200), ("m.gguf", 300))
    manifest = core.load_manifest(path)
    сделано = core.apply_drift(manifest, core.manifest_drift(manifest))
    core.save_manifest(manifest, path)
    выросло = len(path.read_bytes().split(b"\r\n")) - len(стало.split(b"\r\n"))
    assert сделано == (0, 3), сделано
    assert выросло == 3, f"строк прибавилось {выросло}, а сумм легло {сделано.hashes}"
    core.load_manifest(path)   # и это по-прежнему читается


@case
def a_closed_repo_does_not_sink_the_whole_check():
    """Один закрытый репозиторий не должен мешать узнать про остальные.

    Их в манифесте тринадцать. Если сверка падает на первом же недоступном,
    человек не узнает ничего и про двенадцать оставшихся.
    """
    import copy

    Handler.pages = None
    for code, что_в_ответе in ((403, "HF_TOKEN"), (404, "models.json"), (500, "500")):
        Handler.tree, Handler.hits = code, 0
        manifest = copy.deepcopy(SYNC_MANIFEST)
        drifts = core.manifest_drift(manifest)
        assert {d.state for d in drifts} == {"репозиторий"}, drifts
        assert что_в_ответе in drifts[0].now, drifts[0].now
        # Причина у всех записей одна, и запрашивать репозиторий заново незачем.
        assert Handler.hits == 2, f"запросов {Handler.hits}, ждали по одному на репозиторий"

        # И править по такой сверке нечего. У недоступного репозитория в поле
        # нового размера лежит текст ошибки, а не число: подставь его в манифест,
        # и там окажется строка вместо размера. Загружаться он после этого
        # перестанет, а узнается это уже при следующем запуске программы.
        assert core.apply_drift(manifest, drifts) == (0, 0), \
            "по недоступному репозиторию правок быть не может"
        core.check_manifest(manifest)


@case
def a_long_repo_listing_is_read_to_the_end():
    """Опись длинного репозитория приходит страницами.

    Прочитать только первую - и остальные файлы выглядели бы как «пропали»,
    а сверка бодро посоветовала бы править пути, с которыми всё в порядке.
    """
    import copy

    Handler.tree = []
    Handler.base = URL.rsplit("/", 1)[0]
    Handler.pages = (как_на_сервере(("a.bin", 100)),
                     как_на_сервере(("sub/b.bin", 200), ("m.gguf", 300)))
    try:
        drifts = core.manifest_drift(copy.deepcopy(SYNC_MANIFEST))
    finally:
        Handler.pages = None
    assert {d.state for d in drifts} == {"ok"}, \
        f"вторая страница описи потерялась: {[(d.dest, d.state) for d in drifts]}"


@case
def one_source_for_the_name_version_and_title():
    """Имя, версия и заголовок окна записаны ровно один раз - в core.py.

    Раньше версия жила в трёх местах (build.py, !define в setup.nsi и команда в
    его же шапке), заголовок окна в двух, имя программы в трёх. За тем, чтобы
    копии не разъехались, следили четыре отдельные проверки, и каждая из них
    появилась после того, как копии таки разъехались. Проверка вида «A совпадает
    с B» - это симптом: один и тот же факт записан дважды.

    Проверки сравнения теперь не нужны и убраны. Вместо них одна, сторожащая не
    совпадение копий, а само их появление: дубли имеют свойство возвращаться -
    подключаемого файла не оказалось под рукой, человек дописал !define прямо в
    setup.nsi, и всё снова работает, молча и мимо core.py.
    """
    import re

    import build

    исходники = ["core.py", "gui.py", "install.py", "build.py", "tests.py",
                 "tests_matrix.py", "setup.nsi"]
    for name in исходники:
        text = (HERE / name).read_text(encoding="utf-8-sig")
        # Заголовок окна core.py собирает из APP, так что целиком строка не
        # встречается нигде, включая сам core.py.
        assert core.WINDOW_TITLE not in text, f"{name}: заголовок окна записан строкой"
        if name != "core.py":
            assert f'"{core.VERSION}"' not in text, f"{name}: версия записана строкой"

    text = (HERE / "setup.nsi").read_text(encoding="utf-8-sig")
    for name in ("APP", "VERSION", "WINTITLE"):
        assert not re.search(rf"^\s*!define\s+{name}\b", text, re.M), \
            f"setup.nsi объявляет {name} сам, а должен брать из version.nsh"
    assert '!include "version.nsh"' in text, "setup.nsi не подключает version.nsh"

    # Сам порождаемый файл: три строки, значения из core.py, и обязательно BOM -
    # без него makensis прочтёт кириллицу в заголовке как ANSI и испортит её,
    # а установщик перестанет находить запущенную программу.
    где = TMP / "nsh"
    где.mkdir(exist_ok=True)
    было = build.HERE
    try:
        build.HERE = где
        путь = build.write_nsis_defines()
    finally:
        build.HERE = было
    raw = путь.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "version.nsh без BOM, кириллица испортится"
    body = raw.decode("utf-8-sig")
    assert f'!define APP "{core.APP}"' in body, body
    assert f'!define VERSION "{core.VERSION}"' in body, body
    assert f'!define WINTITLE "{core.WINDOW_TITLE}"' in body, body

    # Окно ставит себе тот же заголовок, который уезжает в установщик.
    assert "window.title(WINDOW_TITLE)" in (HERE / "gui.py").read_text(encoding="utf-8"), \
        "окно берёт заголовок не из core.WINDOW_TITLE"


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
def a_missing_folder_makes_the_program_look_for_comfyui():
    """Перенос на другую машину: путь из models.json указывает в профиль того,
    кто этот файл правил, и у второго человека такой папки просто нет. Раньше
    окно на этом показывало «папка не найдена» и ждало, пока ткнут «Обзор».

    Приметы сняты с настоящей установки, а не выдуманы. Сперва искались main.py
    и папка comfy - и это не сработало бы даже на той машине, где писалось: у
    ComfyUI Desktop их нет, там custom_nodes, input, models, output, temp, user.
    """
    гнездо = TMP / "поиск"
    гнездо.mkdir(exist_ok=True)

    настоящий = гнездо / "ComfyUI"
    (настоящий / "models" / "checkpoints").mkdir(parents=True, exist_ok=True)
    (настоящий / "custom_nodes").mkdir(exist_ok=True)      # раскладка Desktop
    assert core.looks_like_comfy(настоящий)

    классический = гнездо / "Классический"
    (классический / "models").mkdir(parents=True, exist_ok=True)
    (классический / "main.py").write_text("", encoding="utf-8")
    assert core.looks_like_comfy(классический), "классическая установка не опознана"

    # А вот что папкой ComfyUI считаться не должно.
    пустая = гнездо / "Пустая"
    пустая.mkdir(exist_ok=True)
    голые_модели = гнездо / "ГолыеМодели"
    (голые_модели / "models").mkdir(parents=True, exist_ok=True)
    файл = гнездо / "ЭтоФайл"
    файл.write_text("", encoding="utf-8")
    for мимо in (пустая, голые_модели, файл, гнездо / "Нет такой"):
        assert not core.looks_like_comfy(мимо), f"опознал как ComfyUI: {мимо.name}"

    assert core.find_comfy([пустая, голые_модели, настоящий]) == настоящий
    assert core.find_comfy([пустая, голые_модели]) is None

    # Записанный путь никуда не ведёт - ищем. Ведёт - не трогаем ничего.
    #
    # Поиск подменяем: настоящий обходит обычные места, и на машине, где ComfyUI
    # стоит, нашёл бы его. Проверка от этого зависеть не должна - она про то,
    # подключён ли поиск и когда именно, а не про то, что лежит у меня на диске.
    нет_такой = гнездо / "нет-такой-папки"
    было_env = os.environ.pop("COMFYUI_ROOT", None)
    было_find = core.find_comfy
    try:
        core.find_comfy = lambda candidates=None: настоящий
        assert core.comfy_root({"comfyui_root": str(нет_такой)}) == настоящий, \
            "путь никуда не ведёт, а поиск не включился"
        assert core.comfy_root({"comfyui_root": str(классический)}) == классический, \
            "работающий путь перебивать поиском нельзя"

        # Ничего не нашлось - показываем записанный путь, как и раньше, чтобы
        # человек увидел в окне именно то, что чинить.
        core.find_comfy = lambda candidates=None: None
        assert core.comfy_root({"comfyui_root": str(нет_такой)}) == нет_такой

        # Названное прямо не подменяется никогда, даже когда поиск что-то нашёл.
        # Иначе опечатка в --root молча увела бы закачку на 95 ГиБ в чужую папку,
        # а человек узнал бы об этом по кончившемуся месту на диске.
        core.find_comfy = lambda candidates=None: настоящий
        assert core.comfy_root({"comfyui_root": "x"}, str(нет_такой)) == нет_такой, \
            "--root подменили автопоиском"
        os.environ["COMFYUI_ROOT"] = str(нет_такой)
        assert core.comfy_root({"comfyui_root": "x"}) == нет_такой, \
            "COMFYUI_ROOT подменили автопоиском"
        os.environ.pop("COMFYUI_ROOT", None)
    finally:
        core.find_comfy = было_find
        if было_env is not None:
            os.environ["COMFYUI_ROOT"] = было_env


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
    # Проверка про порядок источников, а не про автопоиск: путей тут нарочно
    # несуществующих, и на машине, где ComfyUI стоит, поиск подставлял бы его.
    было_find = core.find_comfy
    core.find_comfy = lambda candidates=None: None
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
        core.find_comfy = было_find
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
        # Ничего не скачано, так что считать нечего - но команда обязана
        # отработать, а не свалиться на первом же отсутствующем файле.
        (["--verify", "sdxl"],   1, ["не скачано целиком"],               ["БИТЫХ"]),
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
def the_sync_command_reports_and_refuses_in_the_right_order():
    """Сама команда сверки не вызывалась ни одной проверкой.

    Отдельно проверялись manifest_drift, apply_drift и save_manifest, а код,
    который их связывает - отчёт, отказ записывать и три разных кода возврата, -
    не проходился ни разу. Отказ записывать при недоступном репозитории тут
    самое важное: записать половину и отчитаться «сверено» хуже, чем не делать
    ничего, потому что человек решит, что манифест теперь верен целиком.
    """
    import contextlib
    import copy
    import io

    import install

    путь = TMP / "sync-cli.json"

    def прогнать(write=False):
        core.save_manifest(copy.deepcopy(SYNC_MANIFEST), путь)
        manifest = core.load_manifest(путь)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = install.cmd_sync(manifest, путь, write)
        return code, out.getvalue()

    Handler.pages = None

    # Всё сходится и суммы уже на месте: править нечего.
    целый = copy.deepcopy(SYNC_MANIFEST)
    for файл, имя in ((целый["groups"]["g"]["files"][0], "a.bin"),
                      (целый["groups"]["g"]["files"][1], "sub/b.bin"),
                      (целый["lmstudio"][0]["files"][0], "m.gguf")):
        файл["sha256"] = ХЕШ[имя]
    core.save_manifest(целый, путь)
    Handler.tree = как_на_сервере(("a.bin", 100), ("sub/b.bin", 200), ("m.gguf", 300))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.cmd_sync(core.load_manifest(путь), путь, False)
    assert code == 0 and "всё сходится" in out.getvalue(), out.getvalue()

    # Размер разошёлся: доложить, но без --write ничего не писать.
    Handler.tree = как_на_сервере(("a.bin", 555), ("sub/b.bin", 200), ("m.gguf", 300))
    code, text = прогнать()
    assert code == 1, text
    assert "555" in text and "--write" in text, text
    assert core.load_manifest(путь)["groups"]["g"]["files"][0]["size"] == 100, \
        "без --write манифест трогать нельзя"

    # С --write - вписать и доложить, сколько чего.
    code, text = прогнать(write=True)
    assert code == 0, text
    свежий = core.load_manifest(путь)
    assert свежий["groups"]["g"]["files"][0]["size"] == 555
    assert свежий["groups"]["g"]["files"][0]["sha256"] == ХЕШ["a.bin"]
    assert "размеров 1" in text and "сумм 3" in text, text

    # Путь исчез: сказать про него и не выдумывать ничего.
    Handler.tree = как_на_сервере(("a.bin", 100), ("m.gguf", 300))
    code, text = прогнать(write=True)
    assert code == 1, text
    assert "пропал" in text and "руками" in text, text
    assert core.load_manifest(путь)["groups"]["g"]["files"][1]["size"] == 200

    # Репозиторий недоступен: не писать ничего, даже про уцелевшие записи.
    Handler.tree = 500
    code, text = прогнать(write=True)
    assert code == 1, text
    assert "не вышло" in text and "ничего не записываю" in text, text
    нетронутый = core.load_manifest(путь)
    assert "sha256" not in нетронутый["groups"]["g"]["files"][0], \
        "при недоступном репозитории записано быть ничего не должно"


@case
def verify_catches_rot_that_size_cannot_see():
    """Сумма сверялась только в момент скачивания.

    У человека, у которого 95 ГиБ уже лежат с прошлого месяца, способа их
    проверить не было никакого: --check смотрит на размер, а размер у побитого
    диском файла тот же самый. Порча диска - ровно тот случай, ради которого
    суммы и заводят, и заметить её можно только пройдя по файлам.
    """
    import contextlib
    import hashlib
    import io

    import install

    root = TMP / "verify-root"
    (root / "models/vae").mkdir(parents=True, exist_ok=True)
    целый, битый, безсуммы = (root / "models/vae/целый.bin",
                              root / "models/vae/битый.bin",
                              root / "models/vae/безсуммы.bin")
    for path in (целый, битый, безсуммы):
        path.write_bytes(BODY)
    # Тот же размер, другое содержимое - ровно то, чего размер не видит.
    битый.write_bytes(bytes(SIZE))

    сумма = hashlib.sha256(BODY).hexdigest()
    manifest = {"comfyui_root": str(root), "groups": {"g": {"title": "T", "files": [
        {"dest": "models/vae/целый.bin", "repo": "r", "path": "p",
         "size": SIZE, "sha256": сумма},
        {"dest": "models/vae/битый.bin", "repo": "r", "path": "p",
         "size": SIZE, "sha256": сумма},
        {"dest": "models/vae/безсуммы.bin", "repo": "r", "path": "p", "size": SIZE},
        {"dest": "models/vae/нету.bin", "repo": "r", "path": "p",
         "size": SIZE, "sha256": сумма},
    ]}}}
    core.check_manifest(manifest)

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.cmd_verify(manifest, root)
    text = out.getvalue()

    assert code == 1, text
    assert "ok        models/vae/целый.bin" in text, text
    assert "БИТЫЙ     models/vae/битый.bin" in text, text
    assert сумма in text, "в отчёте нет того, что ожидалось"
    assert "нет суммы models/vae/безсуммы.bin" in text, text
    assert "missing   models/vae/нету.bin" in text, text
    assert "БИТЫХ ФАЙЛОВ: 1" in text, text
    # Битый файл сам по себе не перекачается: размер у него верный, и
    # --install его пропустит как готовый. Об этом надо сказать прямо.
    assert "размер у них верный" in text, text

    # Всё цело - и это отдельный исход, а не отсутствие жалоб.
    целиком = {"comfyui_root": str(root), "groups": {"g": {"title": "T", "files": [
        {"dest": "models/vae/целый.bin", "repo": "r", "path": "p",
         "size": SIZE, "sha256": сумма.upper()},   # регистр значения не имеет
    ]}}}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.cmd_verify(целиком, root)
    assert code == 0 and "сошлось побайтно" in out.getvalue(), out.getvalue()


@case
def the_checks_never_touch_the_real_settings():
    """Прогон обязан быть безвредным для машины, на которой идёт.

    Он таким не был: проверка окна дёргает apply_typed_root(), тот зовёт
    remember_root(), и запомненная папка ComfyUI уезжала в настоящий
    %LOCALAPPDATA%. Каждый прогон молча переставлял человеку путь к моделям на
    временную папку, которую сам же потом и стирал, - а при следующем запуске
    программа говорила «папка не найдена», и виноватой выглядела она.

    Сторожим не место записи, а то, что оно уведено в песочницу: LOCALAPPDATA
    на весь прогон смотрит в TMP. Забыть подмену в новой проверке слишком
    легко, а заметно это станет не тут, а через неделю у человека.
    """
    песочница = core.settings_path()
    assert TMP in песочница.parents, \
        f"настройки пишутся мимо песочницы, в {песочница} - прогон портит машину"

    # И запись туда действительно доходит, иначе сторож охранял бы пустоту.
    core.remember_root("D:/Проверочная")
    assert core.saved_root() == "D:/Проверочная"
    assert песочница.is_file()


@case
def hashing_a_file_on_disk_reports_progress():
    """Чтение 95 ГиБ идёт минут двадцать, и молчать всё это время нельзя."""
    path = TMP / "hash-me.bin"
    path.write_bytes(BODY)
    шаги = []
    import hashlib
    got = core.file_sha256(path, on_progress=lambda d, t, s: шаги.append((d, t, s)))
    assert got == hashlib.sha256(BODY).hexdigest()
    assert шаги, "ни одного шага прогресса"
    assert шаги[-1][0] == шаги[-1][1] == SIZE, шаги[-1]
    assert all(s >= 0 for _, _, s in шаги), "скорость ушла в минус"

    # Отмена обязана всплывать наверх, а не возвращать полусумму.
    try:
        core.file_sha256(path, should_stop=lambda: True)
    except core.Cancelled:
        pass
    else:
        raise AssertionError("отмена обязана была всплыть")


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
        build.write_nsis_defines()   # его же кладёт preflight перед проверками
        for check in (build.check_docs, build.check_nsi_encoding,
                      build.check_nsi_has_no_copies, build.check_manifest):
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
            ("version.nsh", lambda raw: raw[3:],
             build.check_nsi_encoding, "у version.nsh отняли BOM"),
            ("setup.nsi", lambda raw: raw.replace(
                b'!define PUBLISH', b'!define VERSION "0.0.0"\r\n!define PUBLISH'),
             build.check_nsi_has_no_copies, "версию снова вписали в setup.nsi"),
            ("setup.nsi", lambda raw: raw.replace(
                b'!define PUBLISH', b'!define WINTITLE "\xd0\xa7\xd1\x83\xd0\xb6\xd0\xbe\xd0\xb5"'
                                    b'\r\n!define PUBLISH'),
             build.check_nsi_has_no_copies, "заголовок окна снова вписали в setup.nsi"),
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

        # «Обзор»: выбор папки запоминается так же, как набранный руками.
        было_диалог = gui.filedialog
        gui.filedialog = type("Ф", (), {"askdirectory": staticmethod(
            lambda **kw: str(root))})
        try:
            app.pick_folder()
        finally:
            gui.filedialog = было_диалог
        assert app.root_path.get() == str(root)

        # «Отмена»: поднимает флаг для потока и запирает себя, чтобы второй раз
        # не нажали. Сам поток при этом дожимает текущий кусок.
        app.stop_flag.clear()
        app.cancel_button.configure(state="normal")
        app.cancel()
        assert app.stop_flag.is_set(), "флаг отмены не поднялся, поток не остановится"
        assert str(app.cancel_button.cget("state")) == "disabled"

        # Закрытие: без живого потока обязано пройти без вопросов и погасить
        # насос событий, иначе он сработает на уже разрушенном окне.
        показано.clear()
        app.rows.clear()
        app.root_path = None
        app.on_close()
        assert показано == [], f"закрытие без закачки не должно ничего спрашивать: {показано}"
        assert app.closing is True
        window = None    # окно уже разрушено самим on_close
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
def the_window_download_loop_handles_every_ending():
    """Цикл скачивания в потоке окна не вызывался ни одной проверкой.

    Арифметика полосок перебиралась отдельно, а код, который её крутит, - нет:
    очередь файлов, ветка ошибки, ветка отмены, аварийный перехват. Он живёт с
    первой версии и до сих пор проверялся только глазами.

    Гоняем прямо, а не в потоке: поток тут ничего не меняет, а прогон от него
    стал бы зависеть от расписания.
    """
    import threading as нити

    import gui

    root = TMP / "job-root"
    root.mkdir(exist_ok=True)
    job = [{"dest": "models/vae/один.bin", "repo": "r", "path": "p", "size": SIZE},
           {"dest": "models/vae/два.bin", "repo": "r", "path": "p", "size": SIZE}]

    class Окно:
        """Всё, чего run_job касается снаружи: очередь событий и флаг отмены."""

        def __init__(self):
            self.events = __import__("queue").Queue()
            self.stop_flag = нити.Event()

        part_size = staticmethod(gui.App.part_size)
        run_job = gui.App.run_job

        def выгрести(self):
            out = []
            while not self.events.empty():
                out.append(self.events.get_nowait())
            return out

    # Оба файла скачались.
    serve("whole")
    окно = Окно()
    окно.run_job(job, root)
    события = окно.выгрести()
    failed, cancelled = dict(события)["done"]
    assert (failed, cancelled) == ([], False), (failed, cancelled)
    assert (root / "models/vae/один.bin").read_bytes() == BODY
    assert (root / "models/vae/два.bin").read_bytes() == BODY
    полоски = [payload for kind, payload in события if kind == "progress"]
    assert полоски[-1][2] == полоски[-1][3], "в конце полоска «всего» обязана быть полной"
    assert полоски[-1][4] == 0, "и лететь по сети больше нечему"

    # Оба сорвались: очередь не бросается на первом же, и в конце список неудач.
    serve("gone")
    for имя in ("один.bin", "два.bin"):
        (root / "models/vae" / имя).unlink()
    окно = Окно()
    окно.run_job(job, root)
    события = окно.выгрести()
    failed, cancelled = dict(события)["done"]
    assert len(failed) == 2 and not cancelled, (failed, cancelled)
    assert any("ОШИБКА" in p for k, p in события if k == "log"), события

    # Отмена: очередь обрывается, и это не ошибка.
    serve("whole")
    окно = Окно()
    окно.stop_flag.set()
    окно.run_job(job, root)
    failed, cancelled = dict(окно.выгрести())["done"]
    assert cancelled and failed == [], (failed, cancelled)

    # Что бы ни случилось внутри, событие done обязано уйти: без него окно
    # осталось бы с заблокированной кнопкой и без единого объяснения.
    окно = Окно()
    окно.run_job([{"dest": "models/vae/х.bin", "repo": "r"}], root)   # нет path и size
    события = dict(окно.выгрести())
    assert "done" in события, "done не ушло, окно осталось бы запертым навсегда"
    assert события["done"][0], "внутренняя ошибка обязана попасть в список неудач"


@case
def the_clipboard_survives_the_window_closing():
    """Tk отдаёт буфер обмена по запросу и только пока окно живо.

    Кнопка «Копировать» рассчитана ровно на то, чтобы скопировать название и
    уйти в LM Studio, то есть закрыв окно. Починка была, а сторожа у неё не
    было: проверялась она черновым скриптом, в прогон не попала.
    """
    import tkinter as tk

    import gui

    window = None
    try:
        try:
            window = tk.Tk()
        except tk.TclError as err:
            raise AssertionError(f"Tk не поднялся: {err}") from None
        window.withdraw()
        app = gui.App(window)
        app.copy("lmstudio-community/gemma-4-E2B-it-GGUF")
        assert window.clipboard_get() == "lmstudio-community/gemma-4-E2B-it-GGUF"
        assert "скопировано" in app.log_text.get("1.0", "end")
        app.rows.clear()
        app.root_path = None
        app.closing = True
    finally:
        if window is not None:
            gc.collect()
            window.destroy()
            gc.collect()


@case
def the_build_lays_out_what_people_read():
    """Рядом с exe кладутся models.json, README и папка docs.

    models.json там ещё и перебивает встроенный в exe - на этом держится вся
    правка списка моделей без пересборки. Раньше README клался один, и половина
    ссылок из него вела в пустоту.
    """
    import build

    куда = TMP / "рядом-с-exe"
    куда.mkdir(exist_ok=True)
    build.lay_out_extras(куда)

    assert (куда / "models.json").read_bytes() == (HERE / "models.json").read_bytes()
    assert (куда / "README.md").exists()
    for name in ("build.md", "models.md", "lmstudio.md", "tests.md"):
        assert (куда / "docs" / name).exists(), f"docs/{name} не доехал"
    # Каждая ссылка из README обязана разрешаться и там, куда мы это положили.
    import re as regex
    readme = (куда / "README.md").read_text(encoding="utf-8")
    for link in regex.findall(r"\((docs/[^)]+)\)", readme):
        assert (куда / link).exists(), f"рядом с exe нет {link}"
    assert build.folder_size(куда) > 0

    # Повторная раскладка поверх готовой папки обязана проходить: установка
    # поверх старой версии делает ровно это.
    build.lay_out_extras(куда)


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
    # Имя берём из core.py: в setup.nsi его больше нет, оно приходит туда через
    # version.nsh - из той же константы, что и settings_path().
    app = core.APP
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

    # Настройки уводим в песочницу на весь прогон.
    #
    # Это не предосторожность на будущее, а починка: проверка окна дёргает
    # apply_typed_root(), тот честно зовёт remember_root(), и запомненная папка
    # ComfyUI уезжала в НАСТОЯЩИЙ %LOCALAPPDATA%\InstallerModels\settings.json.
    # То есть каждый прогон проверок молча переставлял человеку путь к моделям -
    # на временную папку, которую сам же потом и стирал. При следующем запуске
    # программа показывала "папка не найдена", и виноватой выглядела она.
    #
    # Отдельные проверки подменяли LOCALAPPDATA сами, но полагаться на это
    # нельзя: забыть подмену в новой проверке слишком легко, а заметно это
    # станет не в прогоне, а через неделю у человека.
    было_appdata = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = str(TMP / "appdata")

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    порт = server.server_address[1]
    URL = f"http://127.0.0.1:{порт}/model.safetensors"
    # Сверка манифеста ходит на api/models/..., и без этой строки она пошла бы в
    # настоящий Hugging Face. Проверкам сеть не нужна ни на байт.
    core.HF_HOST = f"http://127.0.0.1:{порт}"

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
    if было_appdata is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = было_appdata
    # Гоняются они перед каждой сборкой, и каждый прогон оставлял в %TEMP%
    # папку на сотню килобайт. За полгода это заметная куча ни для кого.
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{len(CASES) - failed} из {len(CASES)} прошло")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
