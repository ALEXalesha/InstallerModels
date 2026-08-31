#!/usr/bin/env python3
"""Restore ComfyUI models from Hugging Face. Command line front end."""

import argparse
import shutil
import sys

from core import (
    Cancelled,
    apply_drift,
    check_manifest,
    comfy_root,
    dest_path,
    fetch,
    group_size,
    group_state,
    hf_url,
    human,
    load_manifest,
    manifest_drift,
    manifest_path,
    needed_bytes,
    pending,
    save_manifest,
    status,
    unique,
)

INTERACTIVE = bool(sys.stdout) and sys.stdout.isatty()

STATE_WORD = {"installed": "installed", "partial": "partial", "missing": "not installed"}

MARK_WORD = {"ok": "ok", "missing": "missing", "partial": "partial", "damaged": "DAMAGED"}


class Progress:
    """Redraws one line in a terminal, prints every 10% when piped to a file."""

    def __init__(self):
        self.next_step = 0

    def update(self, done, total, speed):
        pct = done * 100 / total if total else 0
        eta = (total - done) / speed if speed > 0 else 0
        # Скорость в начале файла считается по первым килобайтам и бывает
        # смешной, а на .part done обгоняет total после правки размера в
        # манифесте. И то и другое ломало строку: "999999:00" разъезжало полоску,
        # а отрицательный остаток печатался как "-1:-30".
        eta = min(max(eta, 0), 999 * 60 + 59)
        clock = f"{int(eta // 60):3d}:{int(eta % 60):02d}"

        if INTERACTIVE:
            filled = int(28 * done / total) if total else 0
            bar = "#" * filled + "-" * (28 - filled)
            sys.stdout.write(
                f"\r  [{bar}] {pct:5.1f}%  {human(done)} / {human(total)}"
                f"  {human(speed)}/s  ETA {clock}   "
            )
            sys.stdout.flush()
        elif pct >= self.next_step:
            print(f"  {pct:5.1f}%  {human(done)} / {human(total)}  {human(speed)}/s  ETA {clock}")
            self.next_step = (int(pct) // 10 + 1) * 10

    def finish(self):
        if INTERACTIVE:
            sys.stdout.write("\n")


def cmd_list(manifest, root):
    print(f"ComfyUI root: {root}\n")
    for key, group in manifest["groups"].items():
        state = STATE_WORD[group_state(group, root)]
        count = len(group["files"])
        print(f"  {key:<11} {human(group_size(group)):>10}  {group['title']}")
        print(f"  {'':<11} {'':>10}  {count} file{'' if count == 1 else 's'}, {state}")
    grand = sum(group_size(g) for g in manifest["groups"].values())
    print(f"\n  everything: {human(grand)}")


def cmd_check(manifest, root, keys=None):
    """Без названий групп проверяет всё, с названиями - только их.

    Раньше keys сюда не доходили вовсе: "install.py --check ltx" молча проверял
    все 95 ГиБ, а "--check ltxx" с опечаткой - тоже все, и про опечатку не
    говорил никто. Проверка названий теперь идёт до разбора команды, одна на
    --check и на установку.
    """
    bad = 0
    chosen = keys or list(manifest["groups"])
    for key in chosen:
        group = manifest["groups"][key]
        print(f"\n{key} - {group['title']}")
        for entry in group["files"]:
            state, actual = status(entry, root)
            tail = ""
            if state == "partial":
                tail = f"  ({human(actual)} of {human(entry['size'])} in .part)"
            elif state == "damaged":
                tail = f"  ({actual} vs {entry['size']})"
            print(f"  {MARK_WORD[state]:<9} {entry['dest']}{tail}")
            if state != "ok":
                bad += 1
    print(f"\n{bad} file(s) need downloading" if bad else "\nall files present and correct")
    return 1 if bad else 0


def cmd_lmstudio(manifest):
    print("LM Studio models. Paste the name into LM Studio search, or use the lms CLI.\n")
    for model in manifest.get("lmstudio", []):
        total = sum(f["size"] for f in model["files"])
        print(f"  {model['search']}")
        print(f"    quant to pick: {model['quant']}   total {human(total)}")
        # lms_key лежал в models.json и был расписан в docs/lmstudio.md, а
        # программа его не показывала нигде. Именно этим ключом модель зовут
        # из "lms load" и из API, так что искать его в документации мимо
        # программы - лишний шаг ровно там, где программа и нужна.
        if model.get("lms_key"):
            print(f"    model key in LM Studio: {model['lms_key']}")
        print(f"    CLI:  lms get {model['search']}")
        for item in model["files"]:
            print(f"    - {item['name']}  {human(item['size'])}")
        print()


def cmd_sync(manifest, path, write):
    """Сверяет размеры в models.json с тем, что сейчас на Hugging Face.

    До сих пор размеры добывались руками, по одному curl на файл, и оттого
    отставали: автор пересобрал модель - и скачивание падает с «manifest is out
    of date», а чинить надо пятнадцать чисел вручную. Запросов идёт по одному на
    репозиторий, а не на файл, и качать при этом ничего не надо.
    """
    print("сверяю models.json с Hugging Face...\n")
    drifts = manifest_drift(manifest)

    stale = [d for d in drifts if d.state == "размер"]
    gone = [d for d in drifts if d.state == "нет файла"]
    unreachable = [d for d in drifts if d.state == "репозиторий"]

    for drift in stale:
        print(f"  размер   {drift.dest}")
        print(f"           в манифесте {drift.was}, на сервере {drift.now}")
    for drift in gone:
        print(f"  пропал   {drift.dest}")
        print(f"           {drift.repo}/{drift.path} - файл переименовали или убрали")
    # Один репозиторий на несколько файлов, и жаловаться на него столько же раз
    # незачем: причина у всех одна.
    for repo in dict.fromkeys(d.repo for d in unreachable):
        why = next(d.now for d in unreachable if d.repo == repo)
        print(f"  не вышло {repo}")
        print(f"           {why}")

    checked = len(drifts) - len(unreachable)
    print(f"\nсверено записей: {checked} из {len(drifts)}")
    if not (stale or gone or unreachable):
        print("всё сходится, править нечего")
        return 0

    if unreachable:
        # Записать половину и отчитаться «сверено» - худшее из возможного:
        # человек решит, что манифест теперь верен целиком.
        print("до части репозиториев не достучались, ничего не записываю")
        return 1
    if gone and not stale:
        print("размеры в порядке, но пути устарели - их надо править руками")
        return 1

    if not write:
        print(f"размеров с расхождением: {len(stale)}")
        print("повтори с --write, чтобы вписать новые числа в models.json")
        return 1

    fixed = apply_drift(manifest, drifts)
    check_manifest(manifest)   # что записываем, то и должно проходить загрузку
    save_manifest(manifest, path)
    print(f"вписано новых размеров: {fixed}")
    if gone:
        print("пути, которых больше нет, не тронуты - их надо править руками")
        return 1
    return 0


def cmd_install(manifest, root, keys, dry_run):
    queue = pending(manifest, keys, root)
    wanted = {e["dest"] for e in queue}
    shown = set()
    for key in unique(keys):
        for entry in manifest["groups"][key]["files"]:
            dest = entry["dest"]
            if dest in shown:
                continue
            shown.add(dest)
            if dest not in wanted:
                print(f"skip (already there)  {dest}")

    if not queue:
        print("\nnothing to download")
        return 0

    total = sum(e["size"] for e in queue)
    print(f"\n{len(queue)} file(s) to download, {human(total)}")
    if dry_run:
        for entry in queue:
            print(f"  {entry['repo']}/{entry['path']}")
            print(f"    -> {entry['dest']}  ({human(entry['size'])})")
        return 0

    need = needed_bytes(queue, root)
    if need < total:
        print(f"already in .part: {human(total - need)}, left to fetch: {human(need)}")
    try:
        free = shutil.disk_usage(root).free
    except OSError as err:
        print(f"cannot read free space on {root}: {err}")
        return 1
    if free < need:
        print(f"not enough free space: {human(free)} available, {human(need)} needed")
        return 1

    failed = []
    for n, entry in enumerate(queue, 1):
        print(f"\n[{n}/{len(queue)}] {entry['dest']}  ({human(entry['size'])})")
        print(f"  from {entry['repo']}/{entry['path']}")
        bar = Progress()
        try:
            fetch(
                hf_url(entry["repo"], entry["path"]),
                dest_path(root, entry["dest"]),
                entry["size"],
                on_progress=bar.update,
                on_note=lambda text: print(f"\n  {text}"),
            )
        # Ctrl+C прилетает прямо посреди строки с полоской прогресса, и
        # прощальное "interrupted" приклеивалось к ней хвостом. Перевод строки
        # ставим здесь, пока про полоску ещё есть кому вспомнить.
        except (Cancelled, KeyboardInterrupt):
            bar.finish()
            raise KeyboardInterrupt
        except Exception as err:
            bar.finish()
            print(f"  FAILED: {err}")
            failed.append(entry["dest"])
        else:
            bar.finish()

    if failed:
        print(f"\n{len(failed)} file(s) failed:")
        for name in failed:
            print(f"  {name}")
        print("run the same command again to resume")
        return 1
    print("\ndone")
    return 0


def main():
    # Кривой models.json до сих пор вываливался трассировкой Python на человека,
    # который его же руками и правил. Ошибку показываем по-человечески.
    try:
        manifest = load_manifest()
    except (OSError, ValueError) as err:
        print(f"models.json не читается: {err}")
        return 1
    groups = list(manifest["groups"])

    parser = argparse.ArgumentParser(
        description="Restore ComfyUI models from Hugging Face.",
        epilog="groups: " + ", ".join(groups),
    )
    parser.add_argument("groups", nargs="*", help="which groups to install")
    parser.add_argument("--all", action="store_true", help="install every group")
    parser.add_argument("--list", action="store_true", help="show groups and sizes")
    parser.add_argument("--check", action="store_true", help="verify what is on disk")
    parser.add_argument("--lmstudio", action="store_true", help="show LM Studio model names")
    parser.add_argument("--sync-manifest", action="store_true",
                        help="check sizes in models.json against Hugging Face")
    parser.add_argument("--write", action="store_true",
                        help="with --sync-manifest: write the new sizes into models.json")
    parser.add_argument("--dry-run", action="store_true", help="show what would download")
    parser.add_argument("--root",
                        help="ComfyUI folder (beats COMFYUI_ROOT, the remembered "
                             "folder and models.json)")
    args = parser.parse_args()

    if args.lmstudio:
        cmd_lmstudio(manifest)
        return 0

    # Сверка смотрит только в сеть и в сам манифест: папка ComfyUI ей не нужна,
    # так что и не требуем её - чинить манифест можно и не имея моделей.
    if args.sync_manifest:
        return cmd_sync(manifest, manifest_path(), args.write)
    if args.write:
        print("--write работает только вместе с --sync-manifest")
        return 1

    root = comfy_root(manifest, args.root)

    if args.list or not (args.groups or args.all or args.check):
        cmd_list(manifest, root)
        if not args.list:
            print("\nusage:  python install.py ltx qwen   |   python install.py --all")
        return 0

    # is_dir(), а не exists(): файл с именем папки проходил проверку насквозь,
    # и спотыкалась об него уже запись первого куска - сырым NotADirectoryError.
    if not root.is_dir():
        print(f"ComfyUI folder not found: {root}")
        print("pass --root PATH, set COMFYUI_ROOT or edit comfyui_root in models.json")
        return 1

    keys = groups if args.all else unique(args.groups)
    unknown = [k for k in keys if k not in manifest["groups"]]
    if unknown:
        print(f"unknown group(s): {', '.join(unknown)}")
        print(f"available: {', '.join(groups)}")
        return 1

    if args.check:
        return cmd_check(manifest, root, keys)

    return cmd_install(manifest, root, keys, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted, partial files kept for resume")
        sys.exit(130)
