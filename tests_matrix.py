#!/usr/bin/env python3
"""Перебор пространства состояний вместо примеров по одному.

`tests.py` хранит проверки на поломки, которые уже случались: каждая написана
после того, как баг нашёлся, и умеет поймать его только во второй раз. Этот
файл устроен наоборот - он не знает ни одного конкретного бага. Он перебирает
сочетания того, как может повести себя сервер и что может лежать на диске, и в
каждой клетке требует одного и того же: горсти утверждений, которые обязаны
быть верны всегда, чем бы дело ни кончилось.

Ловят как раз инварианты, а не перебор. "Наружу вылетает только RuntimeError
или Cancelled" в одиночку стоит десятка проверок на конкретные случаи: под него
попадает и сырой OSError из os.replace, и KeyError из манифеста, и то, чего ещё
никто не видел. Перебор - только способ доставки: он загоняет функцию в каждый
угол, а разбирается с углом инвариант.

Запускается сам по себе: python tests_matrix.py
"""

import copy
import http.server
import shutil
import socketserver
import sys
import tempfile
import threading
from itertools import product
from pathlib import Path

import core

# Тело маленькое нарочно: клеток в матрице под четыре сотни, и каждая лишняя
# сотня килобайт - это секунды к каждой сборке.
BODY = bytes(range(256)) * 32
SIZE = len(BODY)
PARTIAL = 2500            # сколько лежит в .part в состоянии "кусок"
DRIP = 3000               # сколько сервер успевает отдать до обрыва
WRONG_DELTA = 4           # на столько врёт размер: расхождения бывают и в 4 байта

RANGES = ("honours", "ignores", "refuses")   # как сервер относится к Range
BODIES = ("all", "part", "none")             # сколько отдаёт до обрыва
SIZES = ("true", "wrong", "absent")          # какой размер называет
PARTS = ("none", "partial", "exact", "over")  # что лежит в .part
# "locked" - файл, который держит открытым чужой процесс, обычно сам ComfyUI.
# Состояние появилось после мутационного прогона: без него можно было снять
# защиту с os.replace, и вся матрица оставалась зелёной. Папку она отсекает до
# сети, файла не того размера переименованию не мешает - а не удаться
# переименование может только тут.
DESTS = ("none", "wrong", "dir", "locked")   # что лежит на месте готового файла
CODES = (403, 404, 500)                      # ответы, после которых тела нет


class Plan:
    """Что сервер сделает со следующим запросом."""

    def __init__(self, ranges="honours", body="all", size="true", code=None):
        self.ranges, self.body, self.size, self.code = ranges, body, size, code

    def __str__(self):
        if self.code:
            return f"код={self.code}"
        return f"range={self.ranges} тело={self.body} размер={self.size}"


# ----------------------------------------------------------------- сервер

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    plan = Plan()
    hits = 0
    bound = 99
    overrun = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        Handler.hits += 1
        plan = Handler.plan

        # Потолок держит сервер, а не проверка после возврата. Разница
        # принципиальная: зациклившаяся закачка до проверки просто не доходит -
        # она вешает весь прогон, а вместе с ним и сборку. Оборвав её здесь
        # неретраибельным 404, мы превращаем зависание в внятный провал.
        if Handler.hits > Handler.bound:
            Handler.overrun = True
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if plan.code:
            self.send_response(plan.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        span = self.headers.get("Range")
        start = int(span.split("=")[1].split("-")[0]) if span else 0
        if span and plan.ranges == "refuses":
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if start >= SIZE:
            self.send_response(416)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        # "ignores" - сервер без поддержки Range: отвечает 200 и шлёт всё с нуля,
        # сколько бы клиент ни просил продолжить.
        resumed = bool(span) and plan.ranges == "honours"
        if not resumed:
            start = 0

        declared = {"true": SIZE, "wrong": SIZE + WRONG_DELTA}.get(plan.size)
        payload = {"all": BODY[start:],
                   "part": BODY[start:start + DRIP],
                   "none": b""}[plan.body]

        self.send_response(206 if resumed else 200)
        if declared is None:
            # Без Content-Length границу тела задаёт закрытие соединения. Так
            # отвечают и настоящие серверы, и sever_size() обязан это пережить.
            self.send_header("Connection", "close")
            self.close_connection = True
        else:
            if resumed:
                self.send_header("Content-Range", f"bytes {start}-{declared - 1}/{declared}")
            self.send_header("Content-Length", str(declared - start))
        self.end_headers()
        self.wfile.write(payload)
        if plan.body != "all":
            self.close_connection = True


class Server(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        pass  # соединения рвём нарочно


# ------------------------------------------------------------- раскладка диска

def lay_out(root, part_state, dest_state):
    """Готовит папку так, как её мог оставить прошлый запуск."""
    dest = root / "models" / "vae" / "model.bin"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest_state in ("wrong", "locked"):
        dest.write_bytes(b"x" * 17)
    elif dest_state == "dir":
        dest.mkdir()
    part = core.part_path(dest)
    if part_state == "partial":
        part.write_bytes(BODY[:PARTIAL])
    elif part_state == "exact":
        part.write_bytes(BODY)
    elif part_state == "over":
        part.write_bytes(BODY + b"z" * 100)
    return dest


def snapshot(path):
    """Во что превратился путь: папка, файл с таким-то содержимым или ничего."""
    if path.is_dir():
        return ("папка", None)
    if path.is_file():
        return ("файл", path.read_bytes())
    return ("ничего", None)


# --------------------------------------------------------------- инварианты

def invariants(label, dest, part, before_dest, before_part, outcome, error,
               hits, dest_state="none"):
    """Что обязано быть верно в любой клетке матрицы. Возвращает список нарушений."""
    broken = []
    after_dest = snapshot(dest)
    after_part = snapshot(part)

    # 1. Наружу вылетает только то, что программа умеет показать человеку.
    #    Сырой OSError, HTTPError, KeyError - это трассировка Python в лицо тому,
    #    кто просто нажал кнопку. Самый ценный инвариант: он ловит и то, чего
    #    никто не предвидел, потому что не перечисляет случаи, а запрещает вид.
    if outcome == "ошибка" and not isinstance(error, (RuntimeError, core.Cancelled)):
        broken.append(f"наружу вылетел {type(error).__name__}: {error}")

    # 2. У ошибки есть текст, по которому можно понять, что случилось.
    if outcome == "ошибка" and isinstance(error, RuntimeError) and not str(error).strip():
        broken.append("ошибка без текста")

    # 3. Успех обязан кончиться готовым файлом, и ровно тем, что заказан.
    if outcome == "успех" and after_dest != ("файл", BODY):
        got = len(after_dest[1]) if after_dest[1] is not None else after_dest[0]
        broken.append(f"успех, но под настоящим именем {got}")

    # 4. Готовый файл никогда не лжёт. Сформулировано через содержимое, а не
    #    через исход: "не успех - значит ничего не двигалось" запрещало бы
    #    законный третий случай. Отмена, пришедшая ровно на дописанном файле,
    #    доводит его до места и только потом всплывает наверх - файл при этом
    #    целый, и терять его из-за отмены было бы глупо. Проверять надо не
    #    неподвижность, а правильность: под настоящим именем не должно
    #    появиться неправильного содержимого, чем бы дело ни кончилось.
    if after_dest != before_dest and after_dest != ("файл", BODY):
        broken.append(f"под настоящим именем оказалось не то: "
                      f"{before_dest[0]} -> {after_dest[0]}")

    # 5. Скачанное не пропадает. Единственный законный способ .part исчезнуть -
    #    стать готовым файлом.
    if before_part[0] == "файл" and after_part[0] != "файл" and after_dest != ("файл", BODY):
        broken.append(".part исчез, не став готовым файлом")

    # 6. Всё конечно. Вечный круг на сервере без Range уже случался: программа
    #    не сдавалась и не двигалась, и выйти из неё можно было только Отменой.
    if Handler.overrun:
        broken.append(f"запросов больше {Handler.bound} - похоже на вечный круг")

    # 7. Когда и без сети понятно, что не выйдет, в сеть не ходим. Папка на
    #    месте готового файла - это не повод выкачать тридцать гигабайт и
    #    споткнуться на последней строке. Состояние диска этого не покажет:
    #    файл-то не появился ни там, ни там. Видно только по счётчику запросов.
    if dest_state == "dir" and hits:
        broken.append(f"папка на месте файла, а в сеть сходили {hits} раз")

    return [f"{label}: {text}" for text in broken]


# ------------------------------------------------------------------ прогон

def every_case():
    """Клетки матрицы. Коды ошибок перебираем отдельно: при 404 остальные оси
    смысла не имеют, а перебирать их всё равно - это впустую потраченные секунды."""
    for ranges, body, size, part, dest in product(RANGES, BODIES, SIZES, PARTS, DESTS):
        yield Plan(ranges=ranges, body=body, size=size), part, dest
    for code, part, dest in product(CODES, PARTS, DESTS):
        yield Plan(code=code), part, dest


def run_case(url, tmp, plan, part_state, dest_state, number):
    root = tmp / f"case{number}"
    dest = lay_out(root, part_state, dest_state)
    part = core.part_path(dest)
    before_dest, before_part = snapshot(dest), snapshot(part)

    # Потолок запросов: сколько честно нужно, чтобы дотянуть файл кусками, плюс
    # запас на повторы и перезапуски. Всё, что сверху, - это уже цикл.
    Handler.plan = plan
    Handler.hits = 0
    Handler.overrun = False
    Handler.bound = -(-SIZE // DRIP) + 2 * core.RETRIES + 4
    # Держим файл открытым ровно на время закачки, как это делает запущенный
    # ComfyUI: Windows не даст переименовать поверх такого файла.
    holder = open(dest, "rb") if dest_state == "locked" else None
    try:
        core.fetch(url, dest, SIZE)
        outcome, error = "успех", None
    except Exception as err:            # noqa: BLE001 - вид исключения и проверяем
        outcome, error = "ошибка", err
    finally:
        if holder:
            holder.close()

    label = f"{plan} .part={part_state} dest={dest_state}"
    return invariants(label, dest, part, before_dest, before_part,
                      outcome, error, Handler.hits, dest_state)


def download_survives_every_corner():
    """Матрица: сервер x состояние .part x состояние готового файла."""
    core.wait_before_retry = lambda seconds, should_stop: None

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/model.safetensors"
    tmp = Path(tempfile.mkdtemp(prefix="matrix-"))

    broken, total = [], 0
    try:
        for number, (plan, part_state, dest_state) in enumerate(every_case()):
            total += 1
            broken += run_case(url, tmp, plan, part_state, dest_state, number)
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    if broken:
        shown = "\n        ".join(broken[:25])
        tail = f"\n        ... и ещё {len(broken) - 25}" if len(broken) > 25 else ""
        raise AssertionError(
            f"нарушено инвариантов: {len(broken)} на {total} клетках\n        {shown}{tail}"
        )
    return total


# ----------------------------------------------------- отмена посреди работы

def cancel_never_loses_anything():
    """Отмена - это тоже угол, и в нём те же инварианты.

    Отменять пробуем и до первого байта, и посреди куска: раньше отмена ровно на
    дописанном файле оставляла его в .part, а потом переставала оставлять - это
    место чинилось дважды, и оба раза примером, а не правилом.
    """
    core.wait_before_retry = lambda seconds, should_stop: None

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/model.safetensors"
    tmp = Path(tempfile.mkdtemp(prefix="cancel-"))

    broken, total = [], 0
    try:
        for number, (part_state, after) in enumerate(product(PARTS, (0, 1, 2))):
            total += 1
            root = tmp / f"cancel{number}"
            dest = lay_out(root, part_state, "none")
            part = core.part_path(dest)
            before_dest, before_part = snapshot(dest), snapshot(part)

            Handler.plan = Plan()
            Handler.hits = 0
            Handler.overrun = False
            Handler.bound = 2 * core.RETRIES + 4
            seen = [0]

            def stop(seen=seen, after=after):
                seen[0] += 1
                return seen[0] > after

            try:
                core.fetch(url, dest, SIZE, should_stop=stop)
                outcome, error = "успех", None
            except Exception as err:        # noqa: BLE001
                outcome, error = "ошибка", err

            label = f"отмена после {after} проверок, .part={part_state}"
            # Отмена - законный исход, и Cancelled инвариант пропускает. А вот
            # потерять из-за неё скачанное или оставить кривой файл нельзя.
            broken += invariants(label, dest, part, before_dest, before_part,
                                 outcome, error, Handler.hits)
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    if broken:
        raise AssertionError("нарушено инвариантов: " + "\n        ".join(broken[:25]))
    return total


# --------------------------------------------------- манифест: ломаем перебором

# Эталон нарочно с полным набором полей, включая необязательные: генератор
# ходит по тому, что видит, и поле, которого тут нет, он не проверит.
REFERENCE = {
    "comfyui_root": "C:/ComfyUI",
    "generated": "2026-08-31",
    "groups": {
        "ltx": {
            "title": "LTX video",
            "title_ru": "LTX видео",
            "workflow": "video.json",
            "files": [
                {"dest": "models/checkpoints/a.safetensors",
                 "repo": "Lightricks/LTX", "path": "a.safetensors", "size": 29145431166},
            ],
        },
    },
    "lmstudio": [
        {"search": "lmstudio-community/gemma-GGUF", "lms_key": "google/gemma",
         "quant": "Q4_K_M", "files": [{"name": "gemma.gguf", "size": 3427877696}]},
    ],
}

# Ноль, отрицательное, дробное, логическое, пустое, не тот контейнер - всё, чем
# поле может оказаться после правки руками или после сорвавшегося copy-paste.
JUNK = (None, True, False, 0, -1, 3.5, "", "текст", [], {}, [1, 2])


def paths_in(node, prefix=()):
    """Все места в манифесте, куда можно ткнуть пальцем."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield prefix + (key,)
            yield from paths_in(value, prefix + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield prefix + (index,)
            yield from paths_in(value, prefix + (index,))


def put(tree, path, value):
    spot = tree
    for step in path[:-1]:
        spot = spot[step]
    spot[path[-1]] = value


def drop(tree, path):
    spot = tree
    for step in path[:-1]:
        spot = spot[step]
    del spot[path[-1]]


def manifest_answers_only_with_valueerror():
    """Единственное правило: check_manifest либо принимает манифест, либо
    жалуется ValueError. Ничем другим она не падает никогда.

    Раньше тут был список из пятнадцати поломок, выписанных руками. Список
    закрывает ровно то, что в него вписали, и молчит про поле, которое добавят
    завтра. Перебор находит новые поля сам.
    """
    core.check_manifest(copy.deepcopy(REFERENCE))  # эталон обязан проходить

    broken, total = [], 0
    for path in list(paths_in(REFERENCE)):
        where = ".".join(str(step) for step in path)
        damaged = [(f"{where} := {value!r}", lambda t, p=path, v=value: put(t, p, v))
                   for value in JUNK]
        damaged.append((f"{where} убрано", lambda t, p=path: drop(t, p)))
        for label, damage in damaged:
            total += 1
            tree = copy.deepcopy(REFERENCE)
            damage(tree)
            try:
                core.check_manifest(tree)
            except ValueError:
                continue
            except Exception as err:    # noqa: BLE001 - вид исключения и проверяем
                broken.append(f"{label}: наружу вылетел {type(err).__name__}: {err}")

    if broken:
        shown = "\n        ".join(broken[:25])
        tail = f"\n        ... и ещё {len(broken) - 25}" if len(broken) > 25 else ""
        raise AssertionError(
            f"check_manifest падает не ValueError в {len(broken)} случаях из {total}"
            f"\n        {shown}{tail}"
        )
    return total


CASES = [download_survives_every_corner,
         cancel_never_loses_anything,
         manifest_answers_only_with_valueerror]


if __name__ == "__main__":
    failed = 0
    for check in CASES:
        try:
            count = check()
        except AssertionError as err:
            failed += 1
            print(f"ПРОВАЛ  {check.__name__}\n        {err}")
        else:
            print(f"ок      {check.__name__}  ({count} клеток)")
    sys.exit(1 if failed else 0)
