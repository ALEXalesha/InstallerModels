"""Shared logic for the CLI and the GUI: manifest, sizes, resumable download."""

import errno
import hashlib
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple
from pathlib import Path
from urllib.parse import quote

RETRIES = 5
CHUNK = 1 << 20
# Отсчитывается от каждого чтения, а не от всей закачки: файл на 30 ГиБ по
# медленной связи идёт часами, и ограничивать его целиком нельзя. Замолчавший
# сервер этим и ловится - соединение живо, а байт из него нет.
TIMEOUT = 60

# Имя, версия и заголовок окна лежат тут и больше нигде.
#
# Версия была записана трижды: в build.py, в !define внутри setup.nsi и в
# команде в его же шапке. Заголовок окна - дважды, имя программы - трижды,
# считая settings_path(). За тем, чтобы копии не разъехались, следили четыре
# отдельные проверки, и каждая из них появилась после того, как копии таки
# разъехались. Проверка вида "A совпадает с B" - это симптом: один и тот же
# факт записан дважды. Сверять копии дешевле не становится - дешевле их не
# заводить. NSIS читать Python не умеет, поэтому build.py кладёт ему эти же
# три строки в version.nsh перед сборкой.
APP = "InstallerModels"
VERSION = "2.0.0"
# Установщик ищет запущенную программу по заголовку окна через FindWindow.
WINDOW_TITLE = f"{APP} - модели для ComfyUI"
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


HEX = set("0123456789abcdefABCDEF")


def need_hash(where, entry):
    """sha256 необязателен - без него всё работает как раньше. Но если он есть,
    он обязан быть настоящим: обрезанная или сбитая строка не поймает ни одной
    поломки, зато завалит закачку целого файла."""
    value = entry.get("sha256")
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or set(value) - HEX:
        raise ValueError(
            f"{where}: sha256 должен быть 64 шестнадцатеричными знаками, а там {value!r}"
        )
    return value


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
            need_hash(spot, entry)
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
    # Тот же APP, что уезжает в setup.nsi: деинсталлятор стирает этот файл по
    # пути $LOCALAPPDATA\${APP}\settings.json, и разъехаться им теперь нечем.
    return Path(base) / APP / "settings.json"


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


# Приметы папки ComfyUI, снятые с настоящей установки, а не выдуманные.
#
# Сперва я собирался искать main.py или папку comfy - и это не сработало бы даже
# на той машине, где писалось: у ComfyUI Desktop их нет вовсе, там лежат
# custom_nodes, input, models, output, temp, user. У классической установки и у
# portable-сборки есть main.py и comfy. Поэтому обязательна только models, а
# рядом достаточно любого спутника из списка.
COMFY_MARKS = ("custom_nodes", "input", "output", "user", "main.py", "comfy")


def looks_like_comfy(path):
    """Папка с именем ComfyUI - ещё не ComfyUI."""
    path = Path(path)
    try:
        if not (path / "models").is_dir():
            return False
        return any((path / mark).exists() for mark in COMFY_MARKS)
    except OSError:
        return False  # отключённый сетевой диск, нет прав - просто мимо


def comfy_candidates():
    """Обычные места, и только они. Обходить диски целиком нельзя: у человека
    с 95 ГиБ моделей это минуты работы винта на ровном месте, а угадывать надо
    лишь тогда, когда записанный путь всё равно не подошёл."""
    home = Path.home()
    места = [home / "Documents", home]
    места += [Path(f"{буква}:/") for буква in "CDEFGH"]
    for место in места:
        yield место / "ComfyUI"
        yield место / "ComfyUI_windows_portable" / "ComfyUI"


def find_comfy(candidates=None):
    for path in (candidates if candidates is not None else comfy_candidates()):
        if looks_like_comfy(path):
            return Path(path)
    return None


def comfy_root(manifest, override=None):
    """Порядок: --root, переменная окружения, папка из «Обзора», models.json.

    Папку из «Обзора» до сих пор знало только окно, а install.py каждый раз начинал
    с пути в models.json: выберешь папку мышкой - в консоли она всё равно не та,
    и одна и та же команда у окна и у консоли считала разные файлы установленными.

    Если выбранный путь никуда не ведёт, ищем ComfyUI в обычных местах. Это про
    перенос на другую машину: comfyui_root в models.json указывает в профиль
    того, кто этот файл правил, и у второго человека такой папки просто нет.
    Ничего работающего это не перебивает - поиск включается только тогда, когда
    записанный путь и так оказался пустым местом.
    """
    прямо = override or os.environ.get("COMFYUI_ROOT")
    root = Path(прямо or saved_root() or manifest["comfyui_root"]).expanduser()
    # Названное прямо - --root или COMFYUI_ROOT - не подменяем никогда, даже
    # если такой папки нет. Человек указал место; молча увести закачку на 95 ГиБ
    # в другое куда хуже, чем сказать «папка не найдена». Ищем только там, где
    # путь взялся сам: из запомненного или из models.json.
    if прямо or root.is_dir():
        return root
    return find_comfy() or root


HF_HOST = "https://huggingface.co"

# Что сервер знает о файле. sha256 бывает пустым: у мелких файлов вне LFS его
# в описи нет, и выдумывать его неоткуда.
Remote = namedtuple("Remote", "size sha256")


def hf_url(repo, path):
    return f"{HF_HOST}/{repo}/resolve/main/{quote(path)}"


def api_url(repo):
    """Опись репозитория. recursive - потому что пути в манифесте вложенные:
    split_files/text_encoders/... без него вернулся бы только верхний уровень."""
    return f"{HF_HOST}/api/models/{quote(repo)}/tree/main?recursive=1"


def next_page(link_header):
    """Hugging Face режет длинные описи на страницы и даёт ссылку в Link.
    Без этого репозиторий на сотню файлов молча вернул бы первую сотню, а
    остальные записи манифеста выглядели бы как «файла больше нет»."""
    for part in (link_header or "").split(","):
        chunk = part.split(";")
        if len(chunk) >= 2 and 'rel="next"' in chunk[1].replace(" ", ""):
            return chunk[0].strip().strip("<>")
    return None


def repo_listing(repo):
    """Что сейчас лежит в репозитории: путь -> размер в байтах.

    Один запрос на весь репозиторий, а не на файл: README до сих пор велел
    добывать размеры руками, по одному curl на каждый, и оттого они и отставали.
    Скачивать при этом ничего не надо - опись отдаётся отдельной ручкой.
    """
    found, url = {}, api_url(repo)
    while url:
        req = urllib.request.Request(url, headers={"User-Agent": "InstallerModels/1.0",
                                                   "Accept-Encoding": "identity"})
        token = hf_token()
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                raise RuntimeError(
                    f"HTTP {err.code} - репозиторий закрыт или требует лицензии, "
                    f"прими её на странице модели и положи токен в HF_TOKEN"
                ) from None
            if err.code == 404:
                raise RuntimeError("репозиторий не найден, проверь repo в models.json") from None
            raise RuntimeError(f"Hugging Face ответил {err.code}") from None
        except NETWORK_ERRORS as err:
            raise RuntimeError(f"нет связи с Hugging Face: {err}") from None
        with resp:
            try:
                page = json.load(resp)
            except ValueError as err:
                raise RuntimeError(f"опись репозитория не разбирается: {err}") from None
            url = next_page(resp.headers.get("Link"))
        if not isinstance(page, list):
            raise RuntimeError("опись репозитория пришла не списком")
        for item in page:
            # size у LFS-файлов - настоящий размер, а не размер указателя:
            # проверено на sdxl_vae, сошлось с манифестом до байта.
            if isinstance(item, dict) and item.get("type") == "file" \
                    and isinstance(item.get("size"), int):
                # sha256 берём только из lfs.oid. Верхний oid - это git-овый
                # sha1 блоба, совсем другое число, и подставить его вместо
                # контрольной суммы значило бы завалить каждую закачку.
                lfs = item.get("lfs")
                oid = lfs.get("oid") if isinstance(lfs, dict) else None
                found[item["path"]] = Remote(
                    item["size"], oid if isinstance(oid, str) and len(oid) == 64 else None
                )
    return found


# state: "ok" - сходится, "размер" - другой размер, "нет файла" - путь исчез,
# "репозиторий" - до репозитория вообще не достучались (в now лежит причина).
# sha - контрольная сумма с сервера, если он её назвал.
Drift = namedtuple("Drift", "group dest repo path state was now sha")


def manifest_entries(manifest):
    """Все записи манифеста единым списком: и группы, и раздел lmstudio.

    У lmstudio файл лежит в корне репозитория и назван name, а не path - но
    устаревать его размер может ровно так же, и вкладка окна складывает эти
    числа в общий итог.
    """
    for name, group in manifest["groups"].items():
        for entry in group["files"]:
            yield (name, entry["dest"], entry["repo"], entry["path"],
                   entry["size"], entry.get("sha256"))
    for model in manifest.get("lmstudio", []):
        for item in model["files"]:
            yield ("lmstudio", item["name"], model["search"], item["name"],
                   item["size"], item.get("sha256"))


def manifest_drift(manifest, listing=None):
    """Сверяет каждую запись манифеста с тем, что сейчас на Hugging Face.

    Описи репозиториев берутся по одному разу: один и тот же repo встречается в
    манифесте до четырёх раз. Сорвавшийся репозиторий не роняет сверку целиком -
    он становится обычной строкой отчёта, иначе один закрытый репозиторий не дал
    бы узнать ничего про остальные двенадцать.
    """
    listing = listing or repo_listing
    drifts, shelves = [], {}
    for group, dest, repo, path, size, _sha in manifest_entries(manifest):
        if repo not in shelves:
            try:
                shelves[repo] = listing(repo)
            except RuntimeError as err:
                shelves[repo] = err
        shelf = shelves[repo]
        if isinstance(shelf, RuntimeError):
            drifts.append(Drift(group, dest, repo, path, "репозиторий", size,
                                str(shelf), None))
            continue
        now = shelf.get(path)
        if now is None:
            drifts.append(Drift(group, dest, repo, path, "нет файла", size, None, None))
        else:
            state = "ok" if now.size == size else "размер"
            drifts.append(Drift(group, dest, repo, path, state, size, now.size, now.sha256))
    return drifts


Applied = namedtuple("Applied", "sizes hashes")


def apply_drift(manifest, drifts):
    """Проставляет новые размеры и контрольные суммы.

    Размер правится только там, где он разошёлся. Путь, уехавший в никуда,
    угадывать нельзя - какой файл автор имел в виду, знает только человек, - и
    сумму по нему тоже брать неоткуда.

    Сумма проставляется всюду, где сервер её назвал, а в манифесте её нет или
    она другая. Взять её больше неоткуда: считать самому - значит скачать все
    95 ГиБ, а сервер отдаёт готовую в той же описи, за тот же один запрос.
    """
    sizes = {(d.group, d.dest): d.now for d in drifts if d.state == "размер"}
    hashes = {(d.group, d.dest): d.sha for d in drifts
              if d.state in ("ok", "размер") and d.sha}
    fixed = added = 0

    def поправить(key, entry):
        nonlocal fixed, added
        size = sizes.get(key)
        if size is not None:
            entry["size"], fixed = size, fixed + 1
        sha = hashes.get(key)
        if sha and entry.get("sha256") != sha:
            entry["sha256"], added = sha, added + 1

    for name, group in manifest["groups"].items():
        for entry in group["files"]:
            поправить((name, entry["dest"]), entry)
    for model in manifest.get("lmstudio", []):
        for item in model["files"]:
            поправить(("lmstudio", item["name"]), item)
    return Applied(fixed, added)


def save_manifest(manifest, path):
    """Пишет models.json обратно так, как он был написан руками: два пробела,
    кириллица как есть, CRLF как у всех файлов проекта. Проверено - на
    неизменённом манифесте запись даёт те же байты, что и были."""
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    Path(path).write_bytes(text.replace("\n", "\r\n").encode("utf-8"))


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


# Что пойдёт под снос. shared - другие группы, которым этот файл тоже нужен.
Doomed = namedtuple("Doomed", "dest path size shared")


def removable(manifest, keys, root):
    """Что удалится, если убрать названные группы.

    Считает только то, что действительно лежит на диске, и по путям из
    манифеста - никаких обходов папок и никакого рекурсивного удаления в коде
    нет вовсе. Барьер тут тот же, что и на записи: dest_path() отвергает "..",
    букву диска и имена устройств, так что стереть мимо папки ComfyUI нечем.

    Заодно отмечает файлы, которые нужны и другим группам: один и тот же dest
    манифест разрешает записать дважды, и снос группы А оставил бы группу Б
    навсегда неполной.
    """
    keys = unique(keys)
    чужое = {}
    for имя, группа in manifest["groups"].items():
        if имя in keys:
            continue
        for entry in группа["files"]:
            чужое.setdefault(entry["dest"], []).append(имя)

    план, видели = [], set()
    for key in keys:
        for entry in manifest["groups"][key]["files"]:
            if entry["dest"] in видели:
                continue
            видели.add(entry["dest"])
            цель = dest_path(root, entry["dest"])
            # Недокачанный кусок - тоже занятое место, и убирать его надо вместе
            # с файлом: иначе "удалил, а место не освободилось".
            for path in (цель, part_path(цель)):
                if path.is_file():
                    план.append(Doomed(entry["dest"], path, path.stat().st_size,
                                       чужое.get(entry["dest"], [])))
    return план


def remove_files(plan):
    """Стирает по готовому списку. Возвращает, что ушло и что не поддалось.

    Не поддаться может запросто: Windows не даёт удалить файл, который держит
    открытым запущенный ComfyUI. Это не повод бросать остальные - убираем что
    можем и честно перечисляем, что осталось.
    """
    ушло, осталось = [], []
    for item in plan:
        try:
            item.path.unlink()
            ушло.append(item)
        except OSError as err:
            осталось.append((item, str(err)))
    return ушло, осталось


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


Frame = namedtuple("Frame", "done size overall total left")


class QueueProgress:
    """Арифметика двух полосок: по текущему файлу и по всей очереди.

    Жила внутри метода потока в gui.py - там её нельзя было ни вызвать, ни
    проверить, и чинилась она дважды по живому. Сначала в поток уезжал один
    полный объём очереди, и после обрыва полоска «всего» начинала с нуля там,
    где на диске лежало почти всё, а время до конца считалось по всему объёму и
    обещало часы вместо минут. Потом оказалось, что сломавшийся файл заливает
    свою полоску до конца ровно там, где в логе написано ОШИБКА.

    Считает она пять чисел и ничего не знает ни про tkinter, ни про потоки:

      done, size - сколько лежит у текущего файла и сколько ему положено;
      overall, total - то же по всей очереди, вместе с уже лежавшим в .part;
      left - сколько ещё лететь по сети. Не то же, что total - overall:
             недокачанное уже на диске и времени больше не займёт.
    """

    def __init__(self, sizes, on_disk):
        self.sizes = list(sizes)
        self.on_disk = list(on_disk)
        self.total = sum(self.sizes)
        self.need = sum(max(s - d, 0) for s, d in zip(self.sizes, self.on_disk))
        # ahead[i] - сколько уже лежит в .part у файлов ПОСЛЕ i-го. Снимается
        # один раз на старте: очередь до них ещё не дошла, но место они занимают
        # и в полоске «всего» участвуют с первой секунды.
        self.ahead, tail = [], 0
        for have in reversed(self.on_disk):
            self.ahead.append(tail)
            tail += have
        self.ahead.reverse()
        self.index = 0
        self.finished = 0   # объём файлов, с которыми очередь уже закончила
        self.fetched = 0    # байты, вытянутые из сети за этот заход
        self.had = self.on_disk[0] if self.on_disk else 0

    def start_file(self, index):
        self.index = index
        self.had = self.on_disk[index]

    def advance(self, done):
        """Пришёл кусок. done - сколько всего лежит у текущего файла.

        Из сети за этот заход взято ровно done - had: остальное лежало и раньше.
        При перезапуске файла с нуля done становится меньше had, и тогда это ноль.
        """
        return self._frame(done, self.sizes[self.index],
                           self.finished + self.ahead[self.index] + done,
                           self.fetched + max(done - self.had, 0))

    def finish_file(self):
        size = self.sizes[self.index]
        self.fetched += max(size - self.had, 0)
        self.finished += size
        return self._frame(size, size, self.finished + self.ahead[self.index], self.fetched)

    def fail_file(self, got):
        """Файл не дорос до своего размера, и засчитывать его целиком нельзя."""
        self.fetched += max(got - self.had, 0)
        self.finished += got
        return self._frame(got, self.sizes[self.index],
                           self.finished + self.ahead[self.index], self.fetched)

    def cancel_file(self, got):
        self.fetched += max(got - self.had, 0)

    def _frame(self, done, size, overall, pulled):
        # Оба потолка не украшение: полоска, уехавшая за свой максимум, и
        # отрицательный остаток, из которого потом считают время, - это то, что
        # человек видит в окне. Пусть лучше упрётся, чем покажет чепуху.
        return Frame(done, size, min(overall, self.total), self.total,
                     max(self.need - pulled, 0))


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
    resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    return resp, resp.getcode() == 206


def server_size(resp, resumed):
    """Полный размер файла на сервере, или None если сервер его не назвал."""
    if resumed:
        chunk = resp.headers.get("Content-Range", "").rsplit("/", 1)
        tail = chunk[-1].strip() if len(chunk) == 2 else ""
    else:
        tail = (resp.headers.get("Content-Length") or "").strip()
    return int(tail) if tail.isdigit() else None


class Digest:
    """Считает sha256 по ходу закачки, пока байты и так текут мимо.

    Отдельным проходом по файлу это стоило бы чтения всех 95 ГиБ, поэтому в
    README и было написано, что контрольных сумм не будет. На лету - почти
    даром: байты всё равно проходят через память.

    Помнит, докуда уже досчитано. Без этого каждый обрыв связи заставлял бы
    перечитывать начало файла заново: на тридцати гигабайтах и сотне обрывов
    вышло бы три терабайта лишнего чтения с диска. Перечитывается только то,
    что легло мимо нас - остаток .part от прошлого запуска, и ровно один раз.

    Без ожидаемой суммы не делает ничего и ничего не стоит.
    """

    def __init__(self, want):
        self.want = (want or "").strip().lower()
        self.sum = hashlib.sha256() if self.want else None
        self.upto = 0

    def restart(self):
        """Файл начинается с нуля - и счёт вместе с ним."""
        if self.sum:
            self.sum, self.upto = hashlib.sha256(), 0

    def catch_up(self, path, upto):
        """Досчитывает то, что уже лежит на диске, но через нас не проходило."""
        if not self.sum or self.upto >= upto:
            return
        with open(path, "rb") as fh:
            fh.seek(self.upto)
            while self.upto < upto:
                block = fh.read(min(CHUNK, upto - self.upto))
                if not block:
                    break
                self.sum.update(block)
                self.upto += len(block)

    def add(self, block):
        if self.sum:
            self.sum.update(block)
            self.upto += len(block)

    def mismatch(self):
        """Что получилось, если оно не то. Пусто - значит сошлось или не проверяли."""
        if not self.sum:
            return None
        got = self.sum.hexdigest()
        return None if got == self.want else got


def file_sha256(path, on_progress=None, should_stop=None):
    """Считает sha256 у файла, который уже лежит на диске.

    Сумма из манифеста сверяется в момент скачивания, а у файлов, скачанных
    раньше, её не проверял никто и никогда. Порча диска - ровно тот случай,
    ради которого суммы и заводят, и заметить её можно только пройдя по файлам.

    Дорого: это чтение всего файла. Оттого и отдельной командой, а не внутри
    --check, который бегает по размерам за долю секунды.
    """
    path = Path(path)
    total = path.stat().st_size
    digest = hashlib.sha256()
    done, started = 0, time.monotonic()
    with open(path, "rb") as fh:
        while True:
            if should_stop and should_stop():
                raise Cancelled
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
            done += len(block)
            if on_progress:
                elapsed = max(time.monotonic() - started, 1e-6)
                on_progress(done, total, done / elapsed)
    return digest.hexdigest()


NO_SPACE = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


def disk_is_full(err):
    """Кончившееся место прилетает тем же OSError, что и обрыв связи, и до сих
    пор уходило в повторы: пять подходов по пять секунд, а в конце жалоба на
    сеть. Места от этого не появлялось, зато диагноз уводил в другую сторону."""
    return isinstance(err, OSError) and err.errno in NO_SPACE


NO_SPACE_MESSAGE = "no space left on the disk, free some and run the same command again"


def fetch(url, dest, expected, on_progress=None, on_note=None, should_stop=None,
          sha256=None):
    """Download one file, resuming a leftover .part if there is one.

    sha256 необязателен: без него всё работает ровно как раньше и ничего не
    стоит. С ним совпадение размера перестаёт быть единственным доказательством
    целостности - файл, побитый на диске или собранный из двух ревизий модели с
    одинаковым размером, до сих пор проходил как целый.
    """
    dest = Path(dest)
    part = part_path(dest)
    digest = Digest(sha256)

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
                digest.restart()
            else:
                # Дописываем в хвост: то, что лежит в .part с прошлого запуска,
                # через нас не проходило и в сумму не попало. Досчитываем его
                # один раз - обрыв посреди файла сюда уже не возвращается.
                digest.catch_up(part, offset)

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
                        digest.add(block)
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

    # Целый .part с прошлого запуска мог не пройти через нас ни разу - тогда
    # досчитываем его тут, уже с диска. Это единственный случай, когда за сумму
    # приходится платить лишним чтением файла.
    digest.catch_up(part, actual)
    wrong = digest.mismatch()
    if wrong:
        # Не стираем. Отличить побитый файл от устаревшей суммы в models.json
        # программа не может, а стереть вслепую - это выбросить гигабайты по
        # догадке. Молча качать заново тоже нельзя: если врёт манифест, круг
        # будет вечным. Поэтому останавливаемся и говорим, что именно решать.
        raise RuntimeError(
            f"sha256 не сошёлся: в models.json {sha256}, а у скачанного {wrong}. "
            f"Размер при этом верный, так что дело либо в побитом файле, либо в "
            f"устаревшей сумме. Файл оставлен в {part.name}: убедись, что "
            f"models.json верен, удали его и запусти заново"
        )
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
