#!/usr/bin/env python3
"""Окно для установки моделей ComfyUI. Скачивание идёт в отдельном потоке."""

import queue
import shutil
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core import (
    Cancelled,
    app_dir,
    comfy_root,
    fetch,
    group_size,
    group_state,
    hf_url,
    human,
    load_manifest,
    manifest_path,
    needed_bytes,
    pending,
    status,
)

UNITS_RU = {"B": "Б", "KiB": "КиБ", "MiB": "МиБ", "GiB": "ГиБ", "TiB": "ТиБ"}


def size_ru(nbytes):
    text = human(nbytes)
    number, unit = text.rsplit(" ", 1)
    return f"{number} {UNITS_RU[unit]}"


def eta_text(seconds):
    seconds = max(0, seconds)
    if seconds > 48 * 3600:
        return "больше двух суток"
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин" if minutes else "меньше минуты"


STATE_LABEL = {
    "installed": ("установлено", "#1f8b4c"),
    "partial": ("частично", "#c47f00"),
    "missing": ("не установлено", "#777777"),
}


def enable_dpi_awareness():
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class GroupRow:
    def __init__(self, parent, key, group, on_toggle):
        self.key = key
        self.group = group
        self.picked = tk.BooleanVar(value=False)

        self.box = ttk.Checkbutton(parent, variable=self.picked, command=on_toggle)
        self.name = ttk.Label(parent, text=group.get("title_ru", group["title"]), anchor="w")
        self.size = ttk.Label(parent, text=size_ru(group_size(group)), anchor="e")
        self.state = ttk.Label(parent, anchor="w")

    def place(self, row):
        self.box.grid(row=row, column=0, sticky="w", padx=(8, 0), pady=2)
        self.name.grid(row=row, column=1, sticky="we", padx=4, pady=2)
        self.size.grid(row=row, column=2, sticky="e", padx=8, pady=2)
        self.state.grid(row=row, column=3, sticky="w", padx=(0, 8), pady=2)

    def refresh(self, root):
        state = group_state(self.group, root)
        text, colour = STATE_LABEL[state]
        left = sum(1 for f in self.group["files"] if status(f, root)[0] != "ok")
        if state == "partial":
            text = f"{text}, не хватает {left}"
        self.state.configure(text=text, foreground=colour)
        return state


class App(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.manifest = load_manifest()
        self.root_path = tk.StringVar(value=str(comfy_root(self.manifest)))
        self.events = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.closing = False
        self.rows = {}

        self.build()
        self.refresh()
        self.after(100, self.drain_events)

    # --- построение окна ---

    def build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.tabs = tabs = ttk.Notebook(self)
        tabs.grid(row=0, column=0, sticky="nsew")
        comfy = ttk.Frame(tabs, padding=8)
        lmstudio = ttk.Frame(tabs, padding=8)
        tabs.add(comfy, text="  ComfyUI  ")
        tabs.add(lmstudio, text="  LM Studio  ")

        self.build_comfy(comfy)
        self.build_lmstudio(lmstudio)

    def build_comfy(self, page):
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        page.rowconfigure(5, weight=1)

        top = ttk.Frame(page)
        top.grid(row=0, column=0, sticky="we", pady=(0, 8))
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Папка ComfyUI:").grid(row=0, column=0, sticky="w")
        self.root_entry = ttk.Entry(top, textvariable=self.root_path)
        self.root_entry.grid(row=0, column=1, sticky="we", padx=6)
        self.browse_button = ttk.Button(top, text="Обзор", command=self.pick_folder, width=10)
        self.browse_button.grid(row=0, column=2)
        self.disk_label = ttk.Label(top, foreground="#555555")
        self.disk_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        table = ttk.LabelFrame(page, text=" Группы моделей ", padding=6)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(1, weight=1)
        for n, (key, group) in enumerate(self.manifest["groups"].items()):
            row = GroupRow(table, key, group, self.update_selection)
            row.place(n)
            self.rows[key] = row

        picks = ttk.Frame(page)
        picks.grid(row=2, column=0, sticky="we", pady=8)
        ttk.Button(picks, text="Выделить всё", command=lambda: self.select(True)).pack(side="left")
        ttk.Button(picks, text="Снять всё", command=lambda: self.select(False)).pack(side="left", padx=6)
        ttk.Button(picks, text="Только недостающие", command=self.select_missing).pack(side="left")
        self.picked_label = ttk.Label(picks, font=("", 9, "bold"))
        self.picked_label.pack(side="right")

        bars = ttk.Frame(page)
        bars.grid(row=3, column=0, sticky="we")
        bars.columnconfigure(1, weight=1)
        self.file_label = ttk.Label(bars, text="готов к работе", anchor="w")
        self.file_label.grid(row=0, column=0, columnspan=2, sticky="we")
        ttk.Label(bars, text="файл", width=6).grid(row=1, column=0, sticky="w")
        self.file_bar = ttk.Progressbar(bars, maximum=1000)
        self.file_bar.grid(row=1, column=1, sticky="we", pady=2)
        ttk.Label(bars, text="всего", width=6).grid(row=2, column=0, sticky="w")
        self.total_bar = ttk.Progressbar(bars, maximum=1000)
        self.total_bar.grid(row=2, column=1, sticky="we", pady=2)
        self.speed_label = ttk.Label(bars, text="", anchor="w", foreground="#555555")
        self.speed_label.grid(row=3, column=0, columnspan=2, sticky="we")

        actions = ttk.Frame(page)
        actions.grid(row=4, column=0, sticky="we", pady=8)
        self.download_button = ttk.Button(actions, text="Скачать выбранное", command=self.start)
        self.download_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Отмена", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        ttk.Button(actions, text="Проверить файлы", command=self.refresh).pack(side="left")

        log_box = ttk.LabelFrame(page, text=" Лог ", padding=4)
        log_box.grid(row=5, column=0, sticky="nsew")
        log_box.columnconfigure(0, weight=1)
        log_box.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_box, height=8, wrap="word", state="disabled",
                                background="#1e1e1e", foreground="#d4d4d4", relief="flat")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_box, command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

    def build_lmstudio(self, page):
        page.columnconfigure(0, weight=1)
        ttk.Label(
            page,
            wraplength=740,
            justify="left",
            text="Эти модели программа не качает. LM Studio ведёт свой список моделей, и файлы,"
                 " положенные мимо приложения, оно может не увидеть. Скопируй название и вставь"
                 " в поиск внутри LM Studio.",
        ).grid(row=0, column=0, sticky="we", pady=(0, 10))

        for n, model in enumerate(self.manifest["lmstudio"], start=1):
            total = sum(f["size"] for f in model["files"])
            box = ttk.LabelFrame(page, text=f" {model['search'].split('/')[-1]} ", padding=8)
            box.grid(row=n, column=0, sticky="we", pady=4)
            box.columnconfigure(0, weight=1)

            field = ttk.Entry(box)
            field.insert(0, model["search"])
            field.configure(state="readonly")
            field.grid(row=0, column=0, sticky="we")
            ttk.Button(box, text="Копировать", width=12,
                       command=lambda s=model["search"]: self.copy(s)).grid(row=0, column=1, padx=(6, 0))

            names = "\n".join(f"    {f['name']}  -  {size_ru(f['size'])}" for f in model["files"])
            ttk.Label(
                box,
                justify="left",
                foreground="#555555",
                text=f"квант {model['quant']}, всего {size_ru(total)}\n{names}",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # --- действия ---

    def lock_controls(self, running):
        """Пока качаем, папку менять нельзя: поток пишет в ту, что была на старте."""
        state = "disabled" if running else "normal"
        self.root_entry.configure(state=state)
        self.browse_button.configure(state=state)
        for row in self.rows.values():
            row.box.configure(state=state)

    def copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log(f"скопировано: {text}")

    def pick_folder(self):
        chosen = filedialog.askdirectory(title="Где лежит ComfyUI", initialdir=self.root_path.get())
        if chosen:
            self.root_path.set(chosen)
            self.refresh()

    def current_root(self):
        # expanduser здесь не для красоты: comfy_root() его делает, и без него
        # набранное руками "~/ComfyUI" превращалось в "папка не найдена".
        return Path(self.root_path.get()).expanduser()

    def select(self, value):
        for row in self.rows.values():
            row.picked.set(value)
        self.update_selection()

    def select_missing(self):
        root = self.current_root()
        for row in self.rows.values():
            row.picked.set(group_state(row.group, root) != "installed")
        self.update_selection()

    def chosen_keys(self):
        return [key for key, row in self.rows.items() if row.picked.get()]

    def update_selection(self):
        root = self.current_root()
        queue_ = pending(self.manifest, self.chosen_keys(), root)
        total = sum(e["size"] for e in queue_)
        if queue_:
            self.picked_label.configure(text=f"к скачиванию: {len(queue_)} файлов, {size_ru(total)}")
        else:
            self.picked_label.configure(text="ничего не выбрано")

    def refresh(self):
        root = self.current_root()
        if not root.exists():
            self.disk_label.configure(text="папка не найдена", foreground="#c0392b")
            for row in self.rows.values():
                row.state.configure(text="путь не найден", foreground="#c0392b")
            self.picked_label.configure(text="")
            return

        # Папка может существовать и всё равно не отвечать: отключённый сетевой
        # диск, вынутая флешка. Раньше это исключение вылетало из __init__, и окно
        # показывало "models.json не читается" - диагноз мимо цели.
        try:
            free = shutil.disk_usage(root).free
        except OSError as err:
            self.disk_label.configure(text=f"диск не отвечает: {err}", foreground="#c0392b")
        else:
            self.disk_label.configure(
                text=f"свободно на диске: {size_ru(free)}", foreground="#555555"
            )
        for row in self.rows.values():
            row.refresh(root)
        self.update_selection()

    def log(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # --- скачивание ---

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        root = self.current_root()
        if not root.exists():
            messagebox.showerror("Папка не найдена", f"Нет такой папки:\n{root}")
            return

        job = pending(self.manifest, self.chosen_keys(), root)
        if not job:
            messagebox.showinfo("Нечего качать", "Выбранные группы уже полностью установлены.")
            return

        total = sum(e["size"] for e in job)
        need = needed_bytes(job, root)
        try:
            free = shutil.disk_usage(root).free
        except OSError as err:
            messagebox.showerror("Диск не отвечает", f"Не могу узнать свободное место:\n{err}")
            return
        if free < need:
            messagebox.showerror(
                "Мало места",
                f"Нужно {size_ru(need)}, свободно только {size_ru(free)}.",
            )
            return

        # Показываем и полный объём группы, и остаток: иначе после обрыва
        # окно пугает сорока гигабайтами там, где качать осталось два.
        volume = size_ru(total)
        if need < total:
            volume += f" (из них уже лежит {size_ru(total - need)})"
        if not messagebox.askyesno(
            "Начать скачивание",
            f"Файлов: {len(job)}\nОбъём: {volume}\n\nПапка: {root}\n\nНачинаем?",
        ):
            return

        self.stop_flag.clear()
        self.lock_controls(True)
        self.download_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.total_bar.configure(value=0)
        self.log(f"начинаю: {len(job)} файлов, {size_ru(total)}")

        self.worker = threading.Thread(target=self.run_job, args=(job, root, total), daemon=True)
        self.worker.start()

    def cancel(self):
        self.stop_flag.set()
        self.cancel_button.configure(state="disabled")
        self.log("отмена, дожидаюсь текущего куска")

    def run_job(self, job, root, total):
        """Работает в отдельном потоке. Общается с окном только через очередь.

        Событие done уходит через finally: без этого любая неожиданная ошибка
        оставила бы окно с заблокированной кнопкой и без единого объяснения.
        """
        put = self.events.put
        finished_bytes = 0
        failed = []
        cancelled = False

        try:
            for n, entry in enumerate(job, 1):
                put(("file", f"[{n}/{len(job)}] {entry['dest']}  ({size_ru(entry['size'])})"))
                put(("log", f"качаю {entry['dest']} из {entry['repo']}"))

                def on_progress(done, size, speed, base=finished_bytes):
                    put(("progress", (done, size, base + done, total, speed)))

                try:
                    fetch(
                        hf_url(entry["repo"], entry["path"]),
                        root / entry["dest"],
                        entry["size"],
                        on_progress=on_progress,
                        on_note=lambda text: put(("log", f"  {text}")),
                        should_stop=self.stop_flag.is_set,
                    )
                except Cancelled:
                    put(("log", "остановлено, недокачанный кусок сохранён для докачки"))
                    cancelled = True
                    return
                except Exception as err:
                    put(("log", f"ОШИБКА {entry['dest']}: {err}"))
                    failed.append(entry["dest"])
                else:
                    put(("log", f"готово {entry['dest']}"))

                finished_bytes += entry["size"]
                put(("progress", (entry["size"], entry["size"], finished_bytes, total, 0)))
        except Cancelled:
            cancelled = True
        except Exception as err:
            put(("log", f"СБОЙ ПРОГРАММЫ: {type(err).__name__}: {err}"))
            failed.append("внутренняя ошибка, смотри лог")
        finally:
            put(("done", (failed, cancelled)))

    def apply_event(self, kind, payload):
        if kind == "log":
            self.log(payload)
        elif kind == "file":
            self.file_label.configure(text=payload)
        elif kind == "progress":
            done, size, overall, total, speed = payload
            self.file_bar.configure(value=done * 1000 / size if size else 0)
            self.total_bar.configure(value=overall * 1000 / total if total else 0)
            if speed > 0:
                self.speed_label.configure(
                    text=f"{size_ru(speed)}/с   осталось всего примерно "
                         f"{eta_text((total - overall) / speed)}"
                )
            else:
                self.speed_label.configure(text="")
        elif kind == "done":
            self.finish_job(*payload)

    def drain_events(self):
        """Насос событий обязан пережить что угодно: пока он крутится, окно живо.

        Раньше исключение в любом обработчике уносило и перепланирование - окно
        оставалось с заблокированной кнопкой и замершими полосками навсегда.
        """
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    self.apply_event(kind, payload)
                except Exception as err:
                    print(f"сбой обработчика {kind}: {type(err).__name__}: {err}",
                          file=sys.stderr)
        except queue.Empty:
            pass
        except Exception as err:
            print(f"сбой насоса событий: {type(err).__name__}: {err}", file=sys.stderr)
        finally:
            if not self.closing:
                self.after(100, self.drain_events)

    def finish_job(self, failed, cancelled):
        self.lock_controls(False)
        self.download_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self.speed_label.configure(text="")
        self.file_bar.configure(value=0)
        self.refresh()

        if cancelled:
            self.file_label.configure(text="остановлено")
        elif failed:
            self.file_label.configure(text=f"не скачалось файлов: {len(failed)}")
            messagebox.showwarning(
                "Часть файлов не скачалась",
                "Не удалось скачать:\n\n" + "\n".join(failed) +
                "\n\nНажми «Скачать выбранное» ещё раз, докачается с того же места.",
            )
        else:
            self.file_label.configure(text="всё скачано")
            self.total_bar.configure(value=1000)
            messagebox.showinfo("Готово", "Все выбранные модели на месте.")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Идёт скачивание",
                "Скачивание ещё идёт. Закрыть?\n\nНедокачанное сохранится, потом продолжится с того же места.",
            ):
                return
            self.stop_flag.set()
        self.closing = True
        self.master.destroy()


def main():
    enable_dpi_awareness()
    window = tk.Tk()
    # Заголовок ищет установщик через FindWindow, чтобы не сносить запущенную
    # программу. Меняешь тут - поменяй WINTITLE в setup.nsi.
    window.title("InstallerModels - модели для ComfyUI")
    window.geometry("880x720")
    window.minsize(720, 560)
    for folder in (app_dir(), Path(getattr(sys, "_MEIPASS", app_dir()))):
        icon = folder / "icon.ico"
        if icon.exists():
            try:
                window.iconbitmap(str(icon))
            except Exception:
                pass
            break
    try:
        app = App(window)
    except Exception as err:
        window.withdraw()
        messagebox.showerror(
            "Не удалось прочитать список моделей",
            f"Файл models.json не читается:\n\n{type(err).__name__}: {err}\n\n"
            f"Ожидается тут:\n{manifest_path()}",
        )
        window.destroy()
        return 1

    window.protocol("WM_DELETE_WINDOW", app.on_close)
    window.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
