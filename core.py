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
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def dest_parts(dest):
    """Разбирает dest на части и заодно проверяет его. Не паранойя, а защита от
    опечатки: pathlib на Path(root) / "C:/qwe.bin" выбрасывает root целиком и
    пишет мимо ComfyUI, а ".." уводит на уровень выше. До сих пор никто не
    смотрел, что вообще написано в dest, и такая строка молча срабатывала."""
    text = str(dest).replace("\\", "/")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in text:
        raise ValueError(
            f"плохой dest в models.json: {dest!r} - "
            f"нужен относительный путь внутри папки ComfyUI, без .. и без буквы диска"
        )
    return parts


def dest_path(root, dest):
    """Куда ляжет файл. Единственный способ склеить root и dest во всей программе."""
    return Path(root).joinpath(*dest_parts(dest))


def check_manifest_paths(manifest):
    for group in manifest.get("groups", {}).values():
        for entry in group.get("files", []):
            dest_parts(entry["dest"])


def load_manifest(path=None):
    with open(path or manifest_path(), encoding="utf-8") as fh:
        manifest = json.load(fh)
    # Ловим кривой dest один раз при загрузке, а не в момент записи на диск.
    check_manifest_paths(manifest)
    return manifest


def settings_path():
    base = os.environ.get("LOCALAPPDATA") or Path.home()
    return Path(base) / "InstallerModels" / "settings.json"


def saved_root():
    """Папка, выбранная кнопкой «Обзор» в прошлый раз. До сих пор выбор жил до
    закрытия окна, и каждый запуск начинался с пути из models.json заново."""
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            value = json.load(fh).get("comfyui_root")
    except (OSError, ValueError):
        return None
    return str(value) if value else None


def remember_root(path):
    try:
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as fh:
            json.dump({"comfyui_root": str(path)}, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass  # не запомнили - не беда, работать это не мешает


def comfy_root(manifest, override=None):
    root = override or os.environ.get("COMFYUI_ROOT") or manifest["comfyui_root"]
    return Path(root).expanduser()


def hf_url(repo, path):
    return f"https://huggingface.co/{repo}/resolve/main/{quote(path)}"


def part_path(dest):
    return Path(dest).with_name(Path(dest).name + ".part")


def status(entry, root):
    """Недокачанный кусок лежит в .part, а не под настоящим именем: fetch()
    переименовывает файл только целиком. Пока сюда смотрел один dest, оборванная
    закачка числилась как "ничего нет", хотя на диске уже были гигабайты."""
    dest = dest_path(root, entry["dest"])
    if dest.exists():
        actual = dest.stat().st_size
        return ("ok" if actual == entry["size"] else "damaged"), actual
    part = part_path(dest)
    if part.exists():
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
    req = urllib.request.Request(url, headers={"User-Agent": "InstallerModels/1.0"})
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
    dest.parent.mkdir(parents=True, exist_ok=True)
    note = on_note or (lambda text: None)

    def on_disk():
        return part.stat().st_size if part.exists() else 0

    last_error = None
    from_scratch = False
    stale = 0          # неудачи ПОДРЯД, ни одна из которых не сдвинула файл
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
                best = 0  # файл сейчас обнулится, старая планка уже не про него

            started = time.monotonic()
            done = offset
            try:
                with resp, open(part, mode) as out:
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
        # потом всплываем наверх.
        if on_disk() == expected:
            os.replace(part, dest)
        raise

    actual = on_disk()
    if actual != expected:
        reason = f" (last error: {last_error})" if last_error else ""
        raise RuntimeError(f"got {human(actual)}, expected {human(expected)}{reason}")
    os.replace(part, dest)
