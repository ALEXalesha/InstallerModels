"""Shared logic for the CLI and the GUI: manifest, sizes, resumable download."""

import errno
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

RETRIES = 5
CHUNK = 1 << 20
FROZEN = getattr(sys, "frozen", False)


class Cancelled(Exception):
    """Raised when the user stops a download in progress."""


def wait_before_retry(seconds, should_stop):
    """Sleep in short steps so Cancel does not have to wait out the whole pause."""
    for _ in range(seconds * 10):
        if should_stop and should_stop():
            raise Cancelled
        time.sleep(0.1)


def app_dir():
    return Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent


def manifest_path():
    beside = app_dir() / "models.json"
    if beside.exists():
        return beside
    if FROZEN:
        return Path(sys._MEIPASS) / "models.json"
    return beside


def human(nbytes):
    size = float(nbytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        # Единицу выбираем по округлённому числу, а не по исходному. Иначе
        # 1023.6 байта печаталось как "1024 B", а 1048570 - как "1024.0 KiB":
        # число уже переросло свою единицу, а подпись рядом осталась старая.
        # Видно это было на скорости - она дробная и через human() идёт всегда.
        digits = 0 if unit == "B" else 1
        if unit == "TiB" or abs(round(size, digits)) < 1024:
            return f"{size:.{digits}f} {unit}"
        size /= 1024


# Имена, которые Windows отдаёт устройствам, а не файлам, - и с любым расширением:
# "CON.bin" это тоже консоль. Открыть такой файл получается, запись в него уходит
# в никуда и не жалуется, а на диске потом ничего нет.
DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL",
                *(f"COM{n}" for n in range(1, 10)),
                *(f"LPT{n}" for n in range(1, 10))}

# Windows их не хранит: часть просто запрещена, а хвостовые пробелы и точки он
# молча срезает - файл ложится под другим именем, и status() потом его не находит.
FORBIDDEN = set('<>:"|?*') | {chr(code) for code in range(32)}


def dest_parts(dest):
    """Разбирает dest на части и заодно проверяет его. Не паранойя, а защита от
    опечатки: pathlib на Path(root) / "C:/qwe.bin" выбрасывает root целиком и
    пишет мимо ComfyUI, а ".." уводит на уровень выше. До сих пор никто не
    смотрел, что вообще написано в dest, и такая строка молча срабатывала.

    Заодно ловим имена, которые Windows принимает, но хранит не так, как написано.
    Это не выдумка на будущее: README прямо зовёт править models.json руками, а
    файл на 30 ГиБ, ушедший в устройство CON, качается заново каждый запуск -
    ошибки нет, файла нет, и понять по программе ничего нельзя.
    """
    text = str(dest).replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in text:
        raise ValueError(
            f"плохой dest в models.json: {dest!r} - "
            f"нужен относительный путь внутри папки ComfyUI, без .. и без буквы диска"
        )
    for part in parts:
        if part.rstrip(" .") != part:
            raise ValueError(
                f"плохой dest в models.json: {dest!r} - часть пути {part!r} "
                f"кончается пробелом или точкой, Windows их срезает"
            )
        if part.split(".")[0].upper() in DEVICE_NAMES:
            raise ValueError(
                f"плохой dest в models.json: {dest!r} - {part!r} это имя устройства "
                f"Windows, а не файла: запись в него пропадает молча"
            )
        bad = sorted(FORBIDDEN & set(part))
        if bad:
            raise ValueError(
                f"плохой dest в models.json: {dest!r} - в {part!r} есть "
                f"запрещённые в именах файлов знаки: {' '.join(map(repr, bad))}"
            )
    return parts


def dest_path(root, dest):
    """Куда ляжет файл. Единственный способ склеить root и dest во всей программе."""
    return Path(root).joinpath(*dest_parts(dest))


def need_text(where, entry, field):
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: нет строки {field}")
    return value


def need_size(where, entry):
    size = entry.get("size")
    # bool - это тоже int, а True в качестве размера файла осмысленно только для Python.
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ValueError(f"{where}: size должен быть целым числом байт, а там {size!r}")
    return size


def check_manifest(manifest):
    """Проверяет форму models.json целиком и жалуется только ValueError.

    Раньше отсюда смотрели только на dest, а всё остальное разбиралось уже по месту.
    Запись без поля "dest", groups в виде списка или size строкой проходили
    загрузку насквозь и вылетали KeyError, AttributeError или TypeError где-то
    дальше - мимо всех обработчиков, то есть трассировкой Python на человека, который
    этот же файл руками и правил. Проверка одна на всех: и на загрузке, и в build.py.
    """
    if not isinstance(manifest, dict):
        raise ValueError("models.json: ожидался объект в фигурных скобках")
    need_text("models.json", manifest, "comfyui_root")

    groups = manifest.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise ValueError("models.json: groups должен быть непустым объектом")

    # Один и тот же dest с разными repo/size в двух группах: качается он один
    # раз, по первой записи, и вторая группа навсегда остаётся «частично».
    seen = {}
    for name, group in groups.items():
        where = f"группа {name}"
        if not isinstance(group, dict):
            raise ValueError(f"{where}: ожидался объект")
        need_text(where, group, "title")
        files = group.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError(f"{where}: files должен быть непустым списком")
        for entry in files:
            if not isinstance(entry, dict):
                raise ValueError(f"{where}: запись файла должна быть объектом")
            dest = need_text(where, entry, "dest")
            spot = f"{where}, файл {dest}"
            need_text(spot, entry, "repo")
            need_text(spot, entry, "path")
            need_size(spot, entry)
            dest_parts(dest)
            first_group, first_entry = seen.setdefault(dest, (name, entry))
            if first_entry != entry:
                raise ValueError(f"{dest}: разные записи в группах {first_group} и {name}")

    # Вкладку LM Studio окно строит из этих же полей и складывает размеры через sum().
    models = manifest.get("lmstudio", [])
    if not isinstance(models, list):
        raise ValueError("models.json: lmstudio должен быть списком")
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("раздел lmstudio: ожидался объект")
        search = need_text("раздел lmstudio", model, "search")
        where = f"lmstudio {search}"
        need_text(where, model, "quant")
        files = model.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError(f"{where}: files должен быть непустым списком")
        for item in files:
            if not isinstance(item, dict):
                raise ValueError(f"{where}: запись файла должна быть объектом")
            need_size(f"{where}, файл {need_text(where, item, 'name')}", item)
    return manifest


def load_manifest(path=None):
    with open(path or manifest_path(), encoding="utf-8") as fh:
        manifest = json.load(fh)
    # Всю кривизну ловим один раз при загрузке, а не в момент записи на диск.
    return check_manifest(manifest)


def settings_path():
    base = os.environ.get("LOCALAPPDATA") or Path.home()
    return Path(base) / "InstallerModels" / "settings.json"


def saved_root():
    """Папка, выбранная кнопкой «Обзор» в прошлый раз. До сих пор выбор жил до
    закрытия окна, и каждый запуск начинался с пути из models.json заново.

    Ловим тут заодно AttributeError: settings.json правится руками, и если внутри
    окажется список, а не объект, то .get() падал прямо в __init__ окна - вместо
    забытой настройки человек получал окно с сообщением про нечитаемый models.json.
    """
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            value = json.load(fh).get("comfyui_root")
    except (OSError, ValueError, AttributeError):
        return None
    return str(value) if isinstance(value, str) and value else None


def remember_root(path):
    try:
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as fh:
            json.dump({"comfyui_root": str(path)}, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass  # не запомнили - не беда, работать это не мешает


def comfy_root(manifest, override=None):
    """Порядок: --root, переменная окружения, папка из «Обзора», models.json.

    Папку из «Обзора» до сих пор знало только окно, а install.py каждый раз начинал
    с пути в models.json: выберешь папку мышкой - в консоли она всё равно не та,
    и одна и та же команда у окна и у консоли считала разные файлы установленными.
    """
    root = (override or os.environ.get("COMFYUI_ROOT") or saved_root()
            or manifest["comfyui_root"])
    return Path(root).expanduser()


def hf_url(repo, path):
    return f"https://huggingface.co/{repo}/resolve/main/{quote(path)}"


def part_path(dest):
    return Path(dest).with_name(Path(dest).name + ".part")


def status(entry, root):
    """Недокачанный кусок лежит в .part, а не под настоящим именем: fetch()
    переименовывает файл только целиком. Пока сюда смотрел один dest, оборванная
    закачка числилась как "ничего нет", хотя на диске уже были гигабайты.

    is_file(), а не exists(): папка с именем нужного файла - это не "битый файл"
    на её размер, это отсутствующий файл и занятое место. Раньше такая папка
    показывалась как «частично», .part рядом с ней не замечался вовсе, а запись
    доходила до самого конца и падала сырым OSError уже после всех гигабайтов.
    """
    dest = dest_path(root, entry["dest"])
    if dest.is_file():
        actual = dest.stat().st_size
        return ("ok" if actual == entry["size"] else "damaged"), actual
    part = part_path(dest)
    if part.is_file():
        return "partial", part.stat().st_size
    return "missing", 0


def group_size(group):
    return sum(f["size"] for f in group["files"])


def group_state(group, root):
    """Битый файл - это тоже "что-то уже лежит". Раньше группа с единственным
    недокачанным файлом показывалась как "не установлено", хотя место он занимал
    и докачивать его предстояло, а не качать с нуля."""
    marks = [status(f, root)[0] for f in group["files"]]
    if marks and all(m == "ok" for m in marks):
        return "installed"
    return "partial" if any(m != "missing" for m in marks) else "missing"


def unique(keys):
    """Порядок сохраняем, повторы убираем: install.py ltx ltx - это одна группа."""
    return list(dict.fromkeys(keys))


def pending(manifest, keys, root):
    queue, seen = [], set()
    for key in unique(keys):
        for entry in manifest["groups"][key]["files"]:
            if entry["dest"] in seen:
                continue
            seen.add(entry["dest"])
            if status(entry, root)[0] != "ok":
                queue.append(entry)
    return queue


def needed_bytes(queue, root):
    """Сколько ещё предстоит скачать. Недокачанные .part уже лежат на диске и
    места заново не просят: без этого прерванная на середине группа на 40 ГБ
    отказывалась продолжаться, пока не освободишь все 40 ГБ ещё раз."""
    total = 0
    for entry in queue:
        part = part_path(dest_path(root, entry["dest"]))
        have = part.stat().st_size if part.exists() else 0
        total += max(entry["size"] - have, 0)
    return total


# http.client.IncompleteRead и родня не наследуются от OSError, а на chunked
# ответах Hugging Face они прилетают вместо разрыва сокета. Без них один сбой
# сети ронял весь файл мимо всей логики повторов.
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError)


def hf_token():
    """Приватные и «gated» репозитории отдают файл только по токену. Берём его
    из тех же переменных, что и huggingface_hub, чтобы не заводить свою."""
    for name in ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


def open_stream(url, offset):
    # identity в Accept-Encoding - не вежливость, а условие, при котором вообще
    # работают и сверка размера, и докачка. Сожми прокси ответ на лету, и
    # Content-Length станет размером архива, а не файла: программа объявила бы
    # models.json устаревшим и назвала бы «правильный» размер, которого нет.
    req = urllib.request.Request(url, headers={"User-Agent": "InstallerModels/1.0",
                                               "Accept-Encoding": "identity"})
    token = hf_token()
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if offset:
        req.add_header("Range", f"bytes={offset}-")
    resp = urllib.request.urlopen(req, timeout=60)
    return resp, resp.getcode() == 206


def server_size(resp, resumed):
    """Полный размер файла на сервере, или None если сервер его не назвал."""
    if resumed:
        chunk = resp.headers.get("Content-Range", "").rsplit("/", 1)
        tail = chunk[-1].strip() if len(chunk) == 2 else ""
    else:
        tail = (resp.headers.get("Content-Length") or "").strip()
    return int(tail) if tail.isdigit() else None


NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


def disk_is_full(err):
    """Кончившееся место прилетает тем же OSError, что и обрыв связи, и до сих
    пор уходило в повторы: пять подходов по пять секунд, а в конце жалоба на
    сеть. Места от этого не появлялось, зато диагноз уводил в другую сторону."""
    return isinstance(err, OSError) and err.errno in NO_SPACE


NO_SPACE_MESSAGE = "no space left on the disk, free some and run the same command again"


def fetch(url, dest, expected, on_progress=None, on_note=None, should_stop=None):
    """Download one file, resuming a leftover .part if there is one."""
    dest = Path(dest)
    part = part_path(dest)

    # Всё, что мешает положить файл на место, выясняем до первого байта из сети.
    # Папка с именем файла, файл на месте папки models, том только для чтения -
    # каждое из этого раньше всплывало последней строкой функции, сырым OSError
    # и уже после того, как тридцать гигабайт скачаны впустую.
    for path, what in ((dest, "destination"), (part, "part file")):
        if path.exists() and not path.is_file():
            raise RuntimeError(f"{what} {path} is not a file, move it out of the way")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise RuntimeError(f"cannot create folder {dest.parent}: {err}") from None

    note = on_note or (lambda text: None)

    def on_disk():
        return part.stat().st_size if part.exists() else 0

    last_error = None
    from_scratch = False
    stale = 0          # неудачи ПОДРЯД, ни одна из которых не сдвинула файл
    restarts = 0       # сколько раз файл пришлось начинать с нуля
    best = on_disk()   # самый большой размер .part, который мы вообще видели

    def keep_trying(reason, text):
        """Один разбор любого обрыва. Сдвинулся файл - счётчик неудач обнуляем.

        Раньше попытки считались на весь файл: пятый обрыв убивал закачку, даже
        если каждый из них честно дописывал очередной кусок. На тридцати
        гигабайтах по дрожащей связи обрывов бывает под сотню, и такой файл не
        докачивался никогда. Сравниваем не с прошлым размером, а с самым большим
        виденным: иначе перезапуск с нуля (режим "wb") каждый раз выглядел бы
        прогрессом, счётчик обнулялся бы вечно и цикл не кончился бы никогда.
        """
        nonlocal stale, best, last_error
        last_error = reason
        size_now = on_disk()
        if size_now > best:
            best, stale = size_now, 0
        else:
            stale += 1
        if stale >= RETRIES:
            return False
        note(f"{text}, retry in 5s ({stale}/{RETRIES} failures in a row)")
        wait_before_retry(5, should_stop)
        return True

    try:
        while True:
            if should_stop and should_stop():
                raise Cancelled

            # Кусок больше ожидаемого, или сервер отказался отдавать остаток -
            # докачать его нельзя. Стереть прямо тут тоже нельзя: если размер в
            # манифесте врёт, файл на диске как раз целый, и терять его не за что.
            # Обрежет его режим "wb" ниже - уже после того, как сервер назовёт
            # свой размер и станет ясно, что кусок и правда лишний.
            have = on_disk()
            offset = 0 if from_scratch or have > expected else have
            from_scratch = False  # ровно на один заход, иначе Range больше не спросим
            if offset == expected:
                break

            try:
                resp, resumed = open_stream(url, offset)
            except urllib.error.HTTPError as err:
                if err.code == 416:
                    from_scratch = True
                    if not keep_trying(err, f"server refused the range ({err.code})"):
                        break
                    continue
                # 401 и 403 - это не "файл переехал", а закрытый репозиторий.
                # Прошлое сообщение отправляло чинить путь в models.json, где всё
                # верно, вместо страницы модели, где надо принять лицензию.
                if err.code in (401, 403):
                    raise RuntimeError(
                        f"HTTP {err.code} from Hugging Face - repo is private or gated, "
                        f"accept the licence on the model page and put your token into "
                        f"the HF_TOKEN environment variable"
                    ) from None
                if 400 <= err.code < 500:
                    raise RuntimeError(
                        f"HTTP {err.code} from Hugging Face - file moved or renamed, "
                        f"check repo and path in models.json"
                    ) from None
                if not keep_trying(err, f"server error {err.code}"):
                    break
                continue
            except NETWORK_ERRORS as err:
                if disk_is_full(err):
                    raise RuntimeError(NO_SPACE_MESSAGE) from None
                if not keep_trying(err, f"no connection ({err})"):
                    break
                continue

            remote = server_size(resp, resumed)
            # Ноль в Content-Length сервер отдаёт и когда у него самого сбой, так что
            # о манифесте это не говорит ничего - такой ответ уходит в обычный повтор.
            # Размер печатаем в байтах: в КиБ 10240 и 10244 выглядят одинаково, а
            # чинить по этому сообщению предстоит именно точное число.
            if remote and remote != expected:
                resp.close()
                raise RuntimeError(
                    f"server has {remote} bytes, models.json says {expected} - "
                    f"the manifest is out of date, fix size for this file"
                )

            if offset and not resumed:
                offset = 0
            mode = "ab" if offset else "wb"
            if mode == "wb":
                # Начать файл заново - это не прогресс, сколько бы байт ни пришло
                # потом. Планка best тут обнуляется вместе с файлом, а значит
                # счётчик обрывов подряд обнулялся бы после каждого захода: сервер
                # без поддержки Range на рваной связи гонял этот круг вечно, и ни
                # окно, ни консоль из него уже не выходили - только Отмена или Ctrl+C.
                # Перезапуски считаем отдельно, и их запас тоже кончается.
                if have:
                    restarts += 1
                    if restarts > RETRIES:
                        resp.close()
                        last_error = "server keeps sending the file from the start"
                        break
                    note(f"starting over from zero ({restarts}/{RETRIES})")
                best = 0  # файл сейчас обнулится, старая планка уже не про него

            # Открываем файл до сетевого try: PermissionError и NotADirectoryError -
            # это тоже OSError, и они попадали в разбор обрывов связи. Двадцать секунд
            # повторов и жалоба на связь там, где мешала папка только для чтения.
            try:
                out = open(part, mode)
            except OSError as err:
                resp.close()
                if disk_is_full(err):
                    raise RuntimeError(NO_SPACE_MESSAGE) from None
                raise RuntimeError(f"cannot write {part}: {err}") from None

            started = time.monotonic()
            done = offset
            try:
                with resp, out:
                    while True:
                        if should_stop and should_stop():
                            raise Cancelled
                        block = resp.read(CHUNK)
                        if not block:
                            break
                        out.write(block)
                        done += len(block)
                        if on_progress:
                            elapsed = max(time.monotonic() - started, 1e-6)
                            on_progress(done, expected, (done - offset) / elapsed)
            except Cancelled:
                raise
            except NETWORK_ERRORS as err:
                if disk_is_full(err):
                    raise RuntimeError(NO_SPACE_MESSAGE) from None
                if not keep_trying(err, f"connection dropped ({err})"):
                    break
                continue

            size_now = on_disk()
            if size_now == expected:
                break
            # Раньше пустой ответ считался поводом качать файл заново, и .part
            # стирался. На двадцати гигабайтах это выкидывало часы работы из-за
            # одного сбойного ответа. Недокачанное теперь не трогаем никогда:
            # даже если попытки кончатся, следующий запуск продолжит с места.
            if size_now == offset:
                alive = keep_trying("server sent no new bytes", "server sent nothing new")
            else:
                alive = keep_trying(
                    f"stream ended at {human(size_now)} of {human(expected)}",
                    f"stream ended early at {human(size_now)}",
                )
            if not alive:
                break
    except Cancelled:
        # Отмена могла прийти ровно на дописанном последнем куске. Готовый
        # файл из-за этого терять незачем - доводим его до места и только
        # потом всплываем наверх. Не вышло переименовать - и ладно: .part
        # никуда не денется, а разбираться с этим посреди отмены не время.
        if on_disk() == expected:
            try:
                os.replace(part, dest)
            except OSError:
                pass
        raise

    actual = on_disk()
    if actual != expected:
        reason = f" (last error: {last_error})" if last_error else ""
        raise RuntimeError(f"got {human(actual)}, expected {human(expected)}{reason}")
    # Единственное место, где программа трогает файл под настоящим именем, и до
    # сих пор оно было ничем не прикрыто. Windows не даёт переименовать поверх
    # файла, который кто-то держит открытым, а держит его обычно запущенный
    # ComfyUI: скачанные гигабайты кончались строкой [WinError 5] без единого
    # слова о том, что закрыть. Файл при этом цел и лежит в .part.
    try:
        os.replace(part, dest)
    except OSError as err:
        raise RuntimeError(
            f"downloaded in full, but cannot put it in place: {err} - "
            f"close whatever keeps {dest.name} open (ComfyUI) and run again, "
            f"nothing is lost"
        ) from None
