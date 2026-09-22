#!/usr/bin/env python3
"""Restore ComfyUI models from Hugging Face. Command line front end."""

import argparse
import copy
import shutil
import sys

from core import (
    Cancelled,
    add_lmstudio,
    add_model,
    apply_drift,
    check_manifest,
    comfy_root,
    dest_path,
    fetch,
    file_sha256,
    group_size,
    group_state,
    hf_url,
    human,
    load_manifest,
    manifest_drift,
    manifest_path,
    needed_bytes,
    pending,
    removable,
    remove_files,
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

    # Сколько всего изменится - считаем на копии: решение записывать ещё не
    # принято, а трогать настоящий манифест до него нельзя.
    можно = apply_drift(copy.deepcopy(manifest), drifts)

    if not (stale or gone or unreachable or можно.hashes):
        print("всё сходится, править нечего")
        return 0

    if unreachable:
        # Записать половину и отчитаться «сверено» - худшее из возможного:
        # человек решит, что манифест теперь верен целиком.
        print("до части репозиториев не достучались, ничего не записываю")
        return 1

    if можно.hashes:
        print(f"контрольных сумм можно добавить: {можно.hashes}")
    if gone and not (stale or можно.hashes):
        print("размеры в порядке, но пути устарели - их надо править руками")
        return 1

    if not write:
        if stale:
            print(f"размеров с расхождением: {len(stale)}")
        print("повтори с --write, чтобы вписать это в models.json")
        return 1

    сделано = apply_drift(manifest, drifts)
    check_manifest(manifest)   # что записываем, то и должно проходить загрузку
    save_manifest(manifest, path)
    print(f"вписано: размеров {сделано.sizes}, контрольных сумм {сделано.hashes}")
    if gone:
        print("пути, которых больше нет, не тронуты - их надо править руками")
        return 1
    return 0


def cmd_add(manifest, path, link, group=None, dest=None, lmstudio=False, dry_run=False):
    """Добавляет свою модель в models.json по ссылке на Hugging Face.

    До этой команды свой файл добавлялся только правкой models.json руками, и
    руками же приходилось добывать точный размер в байтах: ошибся на четыре
    байта - и скачивание останавливается на «manifest is out of date». Размер и
    контрольную сумму программа и так умела спрашивать у сервера (--sync-manifest),
    не умела она только одного: принять ссылку.

    Всё выведенное догадкой - папка внутри ComfyUI, выбранный квант, спутник
    mmproj, ключ для «lms load» - печатается до записи и помечено словом
    «догадка»: угадывать молча в файле, который потом читает программа, нельзя.
    """
    try:
        if lmstudio:
            added = add_lmstudio(manifest, link)
            print(f"модель   {added.search}")
            print(f"квант    {added.quant}")
            if added.key:
                print(f"ключ     {added.key}  (для lms load)")
            for name in added.files:
                print(f"файл     {name}")
            print(f"объём    {human(added.total)}")
        else:
            added = add_model(manifest, link, group=group, dest=dest)
            print(f"файл     {added.repo}/{added.path}")
            print(f"кладу в  {added.dest}")
            print(f"объём    {human(added.size)}")
            print(f"sha256   {added.sha256 or 'сервер её не назвал - сверять будем по размеру'}")
            print(f"группа   {added.group}{' (создана)' if added.new_group else ''}")
    except ValueError as err:
        print(f"не добавил: {err}")
        return 1
    except RuntimeError as err:          # сеть и ответы Hugging Face
        print(f"не добавил: {err}")
        return 1

    догадки = (tuple(added.guessed) if lmstudio
               else ("папка внутри ComfyUI",) if added.guessed
               else ())
    if догадки:
        print("догадка: " + ", ".join(догадки) + " - проверь и поправь, если не так")

    if dry_run:
        print("\n--dry-run: в models.json ничего не записано")
        return 0
    save_manifest(manifest, path)
    print(f"\nзаписано в {path}")
    return 0


def cmd_verify(manifest, root, keys=None):
    """Пересчитывает sha256 у того, что уже лежит на диске.

    Сумма сверяется в момент скачивания, но у файлов, скачанных раньше, её не
    проверял никто. Между тем порча диска - ровно тот случай, ради которого
    суммы и заводят, и заметна она только так.

    Отдельной командой, а не внутри --check: тот бегает по размерам за долю
    секунды, а этот читает все 95 ГиБ и займёт минут двадцать.
    """
    bad = incomplete = unchecked = 0
    for key in keys or list(manifest["groups"]):
        group = manifest["groups"][key]
        print(f"\n{key} - {group['title']}")
        for entry in group["files"]:
            state, actual = status(entry, root)
            if state != "ok":
                # Не дочитано или не дошло - считать сумму не по чему, и это
                # забота --check, а не наша. Но промолчать тоже нельзя.
                print(f"  {MARK_WORD[state]:<9} {entry['dest']}")
                incomplete += 1
                continue
            want = entry.get("sha256")
            if not want:
                print(f"  нет суммы {entry['dest']}")
                unchecked += 1
                continue

            print(f"  считаю    {entry['dest']}  ({human(entry['size'])})")
            bar = Progress()
            try:
                got = file_sha256(dest_path(root, entry["dest"]), on_progress=bar.update)
            except (Cancelled, KeyboardInterrupt):
                bar.finish()
                raise KeyboardInterrupt
            except OSError as err:
                bar.finish()
                print(f"  НЕ ПРОЧЁЛ {entry['dest']}: {err}")
                bad += 1
                continue
            bar.finish()
            if got == want.lower():
                print(f"  ok        {entry['dest']}")
            else:
                print(f"  БИТЫЙ     {entry['dest']}")
                print(f"            в models.json {want}")
                print(f"            на диске      {got}")
                bad += 1

    print()
    if unchecked:
        print(f"без суммы в models.json: {unchecked} - проверить их нечем, "
              f"загляни в --sync-manifest")
    if incomplete:
        print(f"не скачано целиком: {incomplete}")
    if bad:
        print(f"БИТЫХ ФАЙЛОВ: {bad}")
        print("удали их и запусти установку заново - размер у них верный, "
              "так что сами по себе они не перекачаются")
    elif not incomplete:
        print("всё, что можно было проверить, сошлось побайтно")
    return 1 if (bad or incomplete) else 0


def cmd_remove(manifest, root, keys, yes):
    """Удаляет модели названных групп.

    Раньше программа этого не делала намеренно: удалять 95 ГиБ должен человек
    руками. Но освобождать место всё равно приходится, а руками это пятнадцать
    путей по разным папкам, и промахнуться там проще, чем кажется.

    Поэтому: без --yes ничего не стирается, показывается только список. Под
    снос идут ровно те пути, что записаны в манифесте, папки не трогаются
    вовсе, а файл, нужный и другой группе, пропускается.
    """
    план = removable(manifest, keys, root)
    if not план:
        print("нечего удалять - этих файлов на диске нет")
        return 0

    общий = [d for d in план if d.shared]
    свои = [d for d in план if not d.shared]

    for doomed in свои:
        print(f"  {human(doomed.size):>10}  {doomed.path.name}")
    for doomed in общий:
        print(f"  {'пропуск':>10}  {doomed.path.name}")
        print(f"  {'':>10}  нужен ещё группам: {', '.join(doomed.shared)}")

    место = sum(d.size for d in свои)
    print(f"\nфайлов под удаление: {len(свои)}, освободится {human(место)}")
    if общий:
        print(f"пропущено как общих с другими группами: {len(общий)}")
    if not свои:
        print("удалять нечего: всё перечисленное нужно другим группам")
        return 0

    if not yes:
        print("это только список, ничего не удалено")
        print("повтори с --yes, чтобы удалить на самом деле")
        return 1

    ушло, осталось = remove_files(свои)
    print(f"\nудалено файлов: {len(ушло)}, освобождено {human(sum(d.size for d in ушло))}")
    for doomed, почему in осталось:
        print(f"  НЕ УДАЛОСЬ {doomed.path.name}: {почему}")
    if осталось:
        print("закрой ComfyUI, если он держит эти файлы открытыми, и повтори")
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
                sha256=entry.get("sha256"),
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
    parser.add_argument("--verify", action="store_true",
                        help="recompute sha256 of downloaded files (slow, reads them all)")
    parser.add_argument("--remove", action="store_true",
                        help="delete downloaded models of the named groups")
    parser.add_argument("--yes", action="store_true",
                        help="with --remove: actually delete, not just list")
    parser.add_argument("--lmstudio", action="store_true", help="show LM Studio model names")
    parser.add_argument("--sync-manifest", action="store_true",
                        help="check sizes in models.json against Hugging Face")
    parser.add_argument("--write", action="store_true",
                        help="with --sync-manifest: write the new sizes into models.json")
    parser.add_argument("--add", metavar="LINK",
                        help="add your own model to models.json by a Hugging Face link")
    parser.add_argument("--group", metavar="NAME",
                        help="with --add: which group to put it in (default: custom)")
    parser.add_argument("--dest", metavar="PATH",
                        help="with --add: where inside ComfyUI to put the file "
                             "(default: guessed from the path in the repo)")
    parser.add_argument("--dry-run", action="store_true", help="show what would download")
    parser.add_argument("--root",
                        help="ComfyUI folder (beats COMFYUI_ROOT, the remembered "
                             "folder and models.json)")
    args = parser.parse_args()

    # Добавление смотрит в сеть и в сам манифест: папка ComfyUI ему не нужна,
    # как и сверке. Разбирается оно до --lmstudio, потому что «--add ... --lmstudio»
    # это «добавь в раздел LM Studio», а не «покажи его список»: в обратном
    # порядке команда молча печатала список и ничего не добавляла.
    if args.add:
        return cmd_add(manifest, manifest_path(), args.add, group=args.group,
                       dest=args.dest, lmstudio=args.lmstudio, dry_run=args.dry_run)

    if args.lmstudio:
        cmd_lmstudio(manifest)
        return 0
    for имя, значение in (("--group", args.group), ("--dest", args.dest)):
        if значение:
            print(f"{имя} работает только вместе с --add")
            return 1

    # Сверка смотрит только в сеть и в сам манифест: папка ComfyUI ей не нужна,
    # так что и не требуем её - чинить манифест можно и не имея моделей.
    if args.sync_manifest:
        return cmd_sync(manifest, manifest_path(), args.write)
    if args.write:
        print("--write работает только вместе с --sync-manifest")
        return 1

    root = comfy_root(manifest, args.root)

    if args.list or not (args.groups or args.all or args.check or args.verify
                         or args.remove):
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

    if args.verify:
        return cmd_verify(manifest, root, keys)

    if args.remove:
        # Удалять всё скопом по случайной команде нельзя: назови группы или
        # скажи --all явно. Это единственная необратимая команда программы.
        if not keys:
            print("--remove требует названий групп или --all")
            return 1
        return cmd_remove(manifest, root, keys, args.yes)

    return cmd_install(manifest, root, keys, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted, partial files kept for resume")
        sys.exit(130)
