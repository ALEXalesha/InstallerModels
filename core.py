"""Shared logic for the CLI and the GUI: manifest, sizes, resumable download."""

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


def load_manifest(path=None):
    with open(path or manifest_path(), encoding="utf-8") as fh:
        return json.load(fh)


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
    dest = Path(root) / entry["dest"]
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
        part = part_path(Path(root) / entry["dest"])
        have = part.stat().st_size if part.exists() else 0
        total += max(entry["size"] - have, 0)
    return total


# http.client.IncompleteRead и родня не наследуются от OSError, а на chunked
# ответах Hugging Face они прилетают вместо разрыва сокета. Без них один сбой
# сети ронял весь файл мимо всей логики повторов.
NETWORK_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError)


def open_stream(url, offset):
    req = urllib.request.Request(url, headers={"User-Agent": "InstallerModels/1.0"})
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


def fetch(url, dest, expected, on_progress=None, on_note=None, should_stop=None):
    """Download one file, resuming a leftover .part if there is one."""
    dest = Path(dest)
    part = part_path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    note = on_note or (lambda text: None)

    last_error = None
    from_scratch = False
    try:
        for attempt in range(1, RETRIES + 1):
            if should_stop and should_stop():
                raise Cancelled

            # Кусок больше ожидаемого, или сервер отказался отдавать остаток -
            # докачать его нельзя. Стереть прямо тут тоже нельзя: если размер в
            # манифесте врёт, файл на диске как раз целый, и терять его не за что.
            # Обрежет его режим "wb" ниже - уже после того, как сервер назовёт
            # свой размер и станет ясно, что кусок и правда лишний.
            have = part.stat().st_size if part.exists() else 0
            offset = 0 if from_scratch or have > expected else have
            if offset == expected:
                break

            try:
                resp, resumed = open_stream(url, offset)
            except urllib.error.HTTPError as err:
                if err.code == 416:
                    from_scratch = True
                    continue
                if 400 <= err.code < 500:
                    raise RuntimeError(
                        f"HTTP {err.code} from Hugging Face - file moved or renamed, "
                        f"check repo and path in models.json"
                    ) from None
                last_error = err
                if attempt == RETRIES:
                    break
                note(f"server error {err.code}, retry {attempt}/{RETRIES - 1} in 5s")
                wait_before_retry(5, should_stop)
                continue
            except NETWORK_ERRORS as err:
                last_error = err
                if attempt == RETRIES:
                    break
                note(f"no connection ({err}), retry {attempt}/{RETRIES - 1} in 5s")
                wait_before_retry(5, should_stop)
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
                last_error = err
                if attempt == RETRIES:
                    break
                note(f"connection dropped ({err}), resuming in 5s")
                wait_before_retry(5, should_stop)
                continue

            size_now = part.stat().st_size
            if size_now == expected:
                break
            if attempt < RETRIES:
                # Раньше пустой ответ считался поводом качать файл заново, и .part
                # стирался. На двадцати гигабайтах это выкидывало часы работы из-за
                # одного сбойного ответа. Недокачанное теперь не трогаем никогда:
                # даже если попытки кончатся, следующий запуск продолжит с места.
                if size_now == offset:
                    last_error = "server sent no new bytes"
                    note(f"server sent nothing new, retry {attempt}/{RETRIES - 1} in 5s")
                else:
                    last_error = f"stream ended at {human(size_now)} of {human(expected)}"
                    note(f"stream ended early at {human(size_now)}, resuming in 5s")
                wait_before_retry(5, should_stop)
    except Cancelled:
        # Отмена могла прийти ровно на дописанном последнем куске. Готовый
        # файл из-за этого терять незачем - доводим его до места и только
        # потом всплываем наверх.
        if part.exists() and part.stat().st_size == expected:
            os.replace(part, dest)
        raise

    actual = part.stat().st_size if part.exists() else 0
    if actual != expected:
        reason = f" (last error: {last_error})" if last_error else ""
        raise RuntimeError(f"got {human(actual)}, expected {human(expected)}{reason}")
    os.replace(part, dest)
