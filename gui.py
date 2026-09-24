#!/usr/bin/env python3
"""Окно для установки моделей ComfyUI на Qt (PySide6). Скачивание идёт в отдельном потоке.

До версии 2.0 окно было на tkinter. У Tk каждый виджет - отдельное окно Windows,
и при изменении размера они перерисовывались по одному: окно заметно тормозило.
Qt рисует всё окно одним буфером.
"""

import copy
import queue
import shutil
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStyleFactory,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import window_geometry
from core import (
    Cancelled,
    QueueProgress,
    WINDOW_TITLE,
    add_lmstudio,
    add_model,
    app_dir,
    comfy_root,
    dest_path,
    fetch,
    group_size,
    group_state,
    hf_url,
    human,
    load_manifest,
    manifest_path,
    needed_bytes,
    part_path,
    pending,
    save_manifest,
    remember_root,
    remember_window,
    saved_window,
    removable,
    remove_files,
    status,
)

UNITS_RU = {"B": "Б", "KiB": "КиБ", "MiB": "МиБ", "GiB": "ГиБ", "TiB": "ТиБ"}

# Цвета подобраны так, чтобы читаться и в светлой, и в тёмной теме Windows:
# Qt сам переключает тему окна, а эти подписи красятся отдельно.
GREEN = "#2e9d5b"
AMBER = "#d08a00"
GREY = "#8a8a8a"
RED = "#d0342c"

STATE_LABEL = {
    "installed": ("установлено", GREEN),
    "partial": ("частично", AMBER),
    "missing": ("не установлено", GREY),
}

PUMP_MS = 100


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


def paint(label, text, colour=None):
    label.setText(text)
    label.setStyleSheet(f"color: {colour};" if colour else "")


class dialogs:
    """Все окна-сообщения в одном месте. Проверки подменяют эти методы:
    настоящий QMessageBox остановил бы прогон намертво."""

    @staticmethod
    def info(parent, title, text=""):
        QMessageBox.information(parent, title, text)

    @staticmethod
    def error(parent, title, text=""):
        QMessageBox.critical(parent, title, text)

    @staticmethod
    def warning(parent, title, text=""):
        QMessageBox.warning(parent, title, text)

    @staticmethod
    def yes_no(parent, title, text="", default_no=False):
        default = QMessageBox.No if default_no else QMessageBox.Yes
        answer = QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No, default)
        return answer == QMessageBox.Yes

    @staticmethod
    def pick_dir(parent, title, start):
        return QFileDialog.getExistingDirectory(parent, title, start)


class GroupRow:
    """Строка группы: галочка с названием, объём и состояние на диске.

    Название - текст самой галочки, так что щелчок по нему тоже её ставит.
    """

    def __init__(self, key, group, on_toggle):
        self.key = key
        self.group = group
        self.box = QCheckBox(group.get("title_ru", group["title"]))
        self.box.toggled.connect(lambda _on: on_toggle())
        self.size = QLabel(size_ru(group_size(group)))
        self.size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.state = QLabel()

    @property
    def picked(self):
        return self.box.isChecked()

    def set_picked(self, value):
        # Без сигнала: «Выделить всё» иначе пересчитывало бы очередь на каждую
        # галочку, а пересчёт ходит на диск за каждым файлом.
        self.box.blockSignals(True)
        self.box.setChecked(bool(value))
        self.box.blockSignals(False)

    def place(self, grid, row):
        grid.addWidget(self.box, row, 0)
        grid.addWidget(self.size, row, 1)
        grid.addWidget(self.state, row, 2)

    def refresh(self, root):
        state = group_state(self.group, root)
        text, colour = STATE_LABEL[state]
        left = sum(1 for f in self.group["files"] if status(f, root)[0] != "ok")
        if state == "partial":
            text = f"{text}, не хватает {left}"
        paint(self.state, text, colour)
        return state


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.manifest = load_manifest()
        self.events = queue.Queue()
        self.stop_flag = threading.Event()
        self.worker = None
        self.closing = False
        self.rows = {}
        # Поля «добавить свою модель» по одному на вкладку, и общий признак
        # «запрос в сети уже идёт»: второе нажатие до ответа добавило бы модель
        # дважды, а третье - трижды.
        self.add_edits = {}
        self.add_buttons = {}
        self.adding = False

        # Заголовок ищет установщик через FindWindow, чтобы не сносить запущенную
        # программу. Строка одна на обоих: отсюда она же уезжает в version.nsh,
        # который build.py кладёт рядом с setup.nsi.
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(880, 720)
        self.setMinimumSize(720, 560)
        # Окно открывается там и такого размера, где его закрыли (2.2.0).
        window_geometry.restore(self, saved_window())
        self.build()
        # Папку, выбранную «Обзором», помним между запусками. Порядок источников
        # один на окно и на консоль и живёт в comfy_root().
        self.root_edit.setText(str(comfy_root(self.manifest)))
        self.refresh()

        # Насос событий из потока скачивания. Таймер принадлежит окну и умирает
        # вместе с ним, так что сработать на разрушенном окне он не может.
        self.pump = QTimer(self)
        self.pump.setInterval(PUMP_MS)
        self.pump.timeout.connect(self.drain_events)
        self.pump.start()

    # --- построение окна ---

    def build(self):
        self.tabs = QTabWidget()
        comfy = QWidget()
        lmstudio = QWidget()
        self.tabs.addTab(comfy, "ComfyUI")
        self.tabs.addTab(lmstudio, "LM Studio")
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.addWidget(self.tabs)
        self.setCentralWidget(central)

        self.build_comfy(comfy)
        self.build_lmstudio(lmstudio)

    def build_comfy(self, page):
        lay = QVBoxLayout(page)

        top = QGridLayout()
        top.addWidget(QLabel("Папка ComfyUI:"), 0, 0)
        self.root_edit = QLineEdit()
        # Путь можно и набрать руками, а не только выбрать «Обзором»: Enter
        # перечитывает папку и запоминает её.
        self.root_edit.returnPressed.connect(self.apply_typed_root)
        top.addWidget(self.root_edit, 0, 1)
        self.browse_button = QPushButton("Обзор")
        self.browse_button.clicked.connect(self.pick_folder)
        top.addWidget(self.browse_button, 0, 2)
        self.disk_label = QLabel()
        top.addWidget(self.disk_label, 1, 0, 1, 3)
        top.setColumnStretch(1, 1)
        lay.addLayout(top)

        table = QGroupBox("Группы моделей")
        self.groups_grid = QGridLayout(table)
        self.groups_grid.setColumnStretch(0, 1)
        self.groups_grid.setHorizontalSpacing(16)
        self.fill_groups()
        lay.addWidget(table)

        picks = QHBoxLayout()
        self.pick_buttons = [
            QPushButton("Выделить всё"),
            QPushButton("Снять всё"),
            QPushButton("Только недостающие"),
        ]
        self.pick_buttons[0].clicked.connect(lambda: self.select(True))
        self.pick_buttons[1].clicked.connect(lambda: self.select(False))
        self.pick_buttons[2].clicked.connect(self.select_missing)
        for button in self.pick_buttons:
            picks.addWidget(button)
        picks.addStretch(1)
        self.picked_label = QLabel()
        bold = QFont(self.picked_label.font())
        bold.setBold(True)
        self.picked_label.setFont(bold)
        picks.addWidget(self.picked_label)
        lay.addLayout(picks)

        lay.addLayout(self.build_add_row(
            "comfy",
            "Своя модель: ссылка на файл в Hugging Face",
            "https://huggingface.co/автор/репозиторий/resolve/main/файл.safetensors",
        ))

        bars = QGridLayout()
        self.file_label = QLabel("готов к работе")
        bars.addWidget(self.file_label, 0, 0, 1, 2)
        bars.addWidget(QLabel("файл"), 1, 0)
        self.file_bar = QProgressBar()
        bars.addWidget(self.file_bar, 1, 1)
        bars.addWidget(QLabel("всего"), 2, 0)
        self.total_bar = QProgressBar()
        bars.addWidget(self.total_bar, 2, 1)
        for bar in (self.file_bar, self.total_bar):
            bar.setRange(0, 1000)
            bar.setValue(0)  # новая полоска у Qt стоит на -1, «ни одного значения»
            bar.setTextVisible(False)
        self.speed_label = QLabel()
        paint(self.speed_label, "", GREY)
        bars.addWidget(self.speed_label, 3, 0, 1, 2)
        bars.setColumnStretch(1, 1)
        lay.addLayout(bars)

        actions = QHBoxLayout()
        self.download_button = QPushButton("Скачать выбранное")
        self.download_button.clicked.connect(self.start)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.setEnabled(False)
        self.check_button = QPushButton("Проверить файлы")
        self.check_button.clicked.connect(self.refresh)
        # Единственная необратимая кнопка окна, поэтому и стоит поодаль от
        # остальных, и спрашивает подтверждение с названным объёмом.
        self.remove_button = QPushButton("Удалить выбранное")
        self.remove_button.clicked.connect(self.remove)
        for button in (self.download_button, self.cancel_button, self.check_button):
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.remove_button)
        lay.addLayout(actions)

        log_box = QGroupBox("Лог")
        log_lay = QVBoxLayout(log_box)
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(5000)
        self.log_text.setStyleSheet("QPlainTextEdit { background: #1e1e1e; color: #d4d4d4; border: none; }")
        log_lay.addWidget(self.log_text)
        lay.addWidget(log_box, 1)

    def fill_groups(self):
        """Заново раскладывает строки групп. Зовётся и при сборке окна, и после
        того, как человек добавил свою модель: список групп после этого другой."""
        grid = self.groups_grid
        while grid.count():
            виджет = grid.takeAt(0).widget()
            if виджет is not None:
                виджет.setParent(None)
                виджет.deleteLater()
        self.rows = {}
        for n, (key, group) in enumerate(self.manifest["groups"].items()):
            row = GroupRow(key, group, self.update_selection)
            row.place(grid, n)
            self.rows[key] = row

    def build_add_row(self, kind, label, placeholder):
        """Поле «добавить свою модель по ссылке» - одинаковое на обеих вкладках.

        Раньше свой файл добавлялся только правкой models.json руками, а точный
        размер в байтах приходилось добывать самому: ошибка на четыре байта
        останавливает скачивание сообщением про устаревший манифест. Размер и
        контрольную сумму программа спрашивает у Hugging Face сама.
        """
        box = QVBoxLayout()
        box.addWidget(QLabel(label))
        line = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.returnPressed.connect(lambda k=kind: self.add_by_link(k))
        button = QPushButton("Добавить")
        button.clicked.connect(lambda _c=False, k=kind: self.add_by_link(k))
        line.addWidget(edit, 1)
        line.addWidget(button)
        box.addLayout(line)
        self.add_edits[kind] = edit
        self.add_buttons[kind] = button
        return box

    def lock_adding(self, busy):
        for button in self.add_buttons.values():
            button.setEnabled(not busy)

    def add_by_link(self, kind):
        """Спрашивает у Hugging Face всё про файл по ссылке и дописывает в манифест.

        Запрос уходит в поток: сеть отвечает не мгновенно, а замерший на десять
        секунд интерфейс выглядит как зависшая программа. Ответ приезжает через
        ту же очередь событий, что и прогресс закачки, - другого пути с потока
        в окно тут нет и быть не должно.
        """
        if self.adding or self.running():
            return
        link = self.add_edits[kind].text().strip()
        if not link:
            dialogs.info(self, "Нужна ссылка",
                         "Вставь ссылку на файл в Hugging Face - ту, что в адресной "
                         "строке браузера или под кнопкой download.")
            return
        self.adding = True
        self.lock_adding(True)
        self.log(f"спрашиваю Hugging Face про {link}")
        threading.Thread(target=self.run_add, args=(kind, link), daemon=True).start()

    def run_add(self, kind, link):
        """Поток: манифест правится на копии, и только удачная правка едет в окно."""
        копия = copy.deepcopy(self.manifest)
        try:
            if kind == "lmstudio":
                added = add_lmstudio(копия, link)
            else:
                added = add_model(копия, link)
            save_manifest(копия, manifest_path())
        except (ValueError, RuntimeError) as err:
            self.events.put(("added", (kind, None, None, str(err))))
        except OSError as err:
            # Манифест лежит рядом с exe: в папке только для чтения запись не
            # пройдёт, и сказать об этом надо про файл, а не про сеть.
            self.events.put(("added", (kind, None, None,
                                       f"не записалось в models.json: {err}")))
        else:
            self.events.put(("added", (kind, added, копия, None)))

    def finish_add(self, kind, added, manifest, error):
        """Ответ из потока. Манифест в окне меняется только здесь и только целиком:
        в потоке правилась копия, и неудачная правка до окна не доезжает вовсе."""
        self.adding = False
        self.lock_adding(self.running())
        if error:
            self.log(f"не добавил: {error}")
            dialogs.error(self, "Не добавил", error)
            return
        self.manifest = manifest
        self.add_edits[kind].clear()
        if kind == "lmstudio":
            self.fill_lmstudio()
            догадки = ", ".join(added.guessed)
            строки = [added.search,
                      f"квант {added.quant}, всего {size_ru(added.total)}",
                      *added.files]
            if added.key:
                строки.append(f"ключ модели в LM Studio: {added.key}")
        else:
            self.fill_groups()
            self.refresh()
            догадки = "папка внутри ComfyUI" if added.guessed else ""
            строки = [f"{added.repo}/{added.path}",
                      f"кладу в {added.dest}",
                      f"объём {size_ru(added.size)}",
                      f"группа {added.group}" + (" (создана)" if added.new_group else "")]
        текст = "\n".join(строки)
        if догадки:
            текст += f"\n\nдогадка: {догадки} - проверь и поправь в models.json, если не так"
        self.log(f"добавлено: {строки[0]}")
        dialogs.info(self, "Добавлено", текст)

    def build_lmstudio(self, page):
        outer = QVBoxLayout(page)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.lmstudio_lay = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        intro = QLabel(
            "Эти модели программа не качает. LM Studio ведёт свой список моделей, и файлы,"
            " положенные мимо приложения, оно может не увидеть. Скопируй название и вставь"
            " в поиск внутри LM Studio."
        )
        intro.setWordWrap(True)
        self.lmstudio_lay.addWidget(intro)
        self.fill_lmstudio()

        outer.addLayout(self.build_add_row(
            "lmstudio",
            "Своя модель: ссылка на GGUF-репозиторий в Hugging Face",
            "https://huggingface.co/lmstudio-community/имя-GGUF",
        ))

    def fill_lmstudio(self):
        """Заново раскладывает карточки моделей LM Studio.

        Вступление наверху вкладки трогать нельзя - оно не про модели, поэтому
        снимаются только виджеты после него.
        """
        lay = self.lmstudio_lay
        while lay.count() > 1:
            item = lay.takeAt(1)
            виджет = item.widget()
            if виджет is not None:
                виджет.setParent(None)
                виджет.deleteLater()

        self.copy_buttons = []
        for model in self.manifest.get("lmstudio", []):
            total = sum(f["size"] for f in model["files"])
            box = QGroupBox(model["search"].split("/")[-1])
            grid = QGridLayout(box)
            field = QLineEdit(model["search"])
            field.setReadOnly(True)
            grid.addWidget(field, 0, 0)
            button = QPushButton("Копировать")
            button.clicked.connect(lambda _c=False, s=model["search"]: self.copy(s))
            self.copy_buttons.append(button)
            grid.addWidget(button, 0, 1)

            names = "\n".join(f"    {f['name']}  -  {size_ru(f['size'])}" for f in model["files"])
            # lms_key - ключ, которым модель зовут из "lms load" и из API.
            head = f"квант {model['quant']}, всего {size_ru(total)}"
            if model.get("lms_key"):
                head += f"\nключ модели в LM Studio: {model['lms_key']}"
            details = QLabel(f"{head}\n{names}")
            details.setTextInteractionFlags(Qt.TextSelectableByMouse)
            paint(details, details.text(), GREY)
            grid.addWidget(details, 1, 0, 1, 2)
            grid.setColumnStretch(0, 1)
            lay.addWidget(box)
        lay.addStretch(1)

    # --- действия ---

    def running(self):
        return self.worker is not None and self.worker.is_alive()

    def lock_controls(self, running):
        """Пока качаем, папку менять нельзя: поток пишет в ту, что была на старте.

        Кнопки выбора запираются вместе с галочками: иначе «Выделить всё» посреди
        закачки меняло бы отметки в обход замка, а очередь в потоке - нет.
        """
        for widget in [self.root_edit, self.browse_button, self.remove_button, *self.pick_buttons]:
            widget.setEnabled(not running)
        for row in self.rows.values():
            row.box.setEnabled(not running)
        # Добавление правит манифест, а очередь в потоке собрана по старому:
        # пока качаем, добавлять нельзя.
        self.lock_adding(running or self.adding)

    def copy(self, text):
        # Qt при выходе сам отдаёт буфер обмена системе (OleFlushClipboard), так
        # что «скопировал и закрыл окно» работает: вставить в LM Studio можно и
        # после закрытия программы.
        QGuiApplication.clipboard().setText(text)
        self.log(f"скопировано: {text}")

    def pick_folder(self):
        chosen = dialogs.pick_dir(self, "Где лежит ComfyUI", self.root_edit.text())
        if chosen:
            self.root_edit.setText(str(Path(chosen)))
            remember_root(chosen)
            self.refresh()

    def apply_typed_root(self):
        """Enter в поле пути: перечитать папку и запомнить её, как после «Обзора»."""
        root = self.current_root()
        if root.is_dir():
            remember_root(root)
        self.refresh()

    def current_root(self):
        # expanduser: comfy_root() его делает, и без него набранное руками
        # "~/ComfyUI" превращалось в "папка не найдена".
        return Path(self.root_edit.text().strip()).expanduser()

    def select(self, value):
        for row in self.rows.values():
            row.set_picked(value)
        self.update_selection()

    def select_missing(self):
        root = self.current_root()
        for row in self.rows.values():
            row.set_picked(group_state(row.group, root) != "installed")
        self.update_selection()

    def chosen_keys(self):
        return [key for key, row in self.rows.items() if row.picked]

    def update_selection(self):
        root = self.current_root()
        if not root.is_dir():
            self.picked_label.setText("")
            return
        queue_ = pending(self.manifest, self.chosen_keys(), root)
        total = sum(e["size"] for e in queue_)
        if queue_:
            self.picked_label.setText(f"к скачиванию: {len(queue_)} файлов, {size_ru(total)}")
        else:
            self.picked_label.setText("ничего не выбрано")

    def refresh(self):
        root = self.current_root()
        # is_dir(), а не exists(): файл с именем папки иначе проходил проверку
        # насквозь, и спотыкалась об него уже запись первого куска.
        if not root.is_dir():
            paint(self.disk_label, "папка не найдена", RED)
            for row in self.rows.values():
                paint(row.state, "путь не найден", RED)
            self.picked_label.setText("")
            return

        # Папка может существовать и всё равно не отвечать: отключённый сетевой
        # диск, вынутая флешка.
        try:
            free = shutil.disk_usage(root).free
        except OSError as err:
            paint(self.disk_label, f"диск не отвечает: {err}", RED)
        else:
            paint(self.disk_label, f"свободно на диске: {size_ru(free)}", GREY)
        for row in self.rows.values():
            row.refresh(root)
        self.update_selection()

    def log(self, text):
        self.log_text.appendPlainText(text)

    # --- скачивание ---

    def start(self):
        if self.running():
            return
        root = self.current_root()
        if not root.is_dir():
            dialogs.error(self, "Папка не найдена", f"Нет такой папки:\n{root}")
            return

        job = pending(self.manifest, self.chosen_keys(), root)
        if not job:
            dialogs.info(self, "Нечего качать", "Выбранные группы уже полностью установлены.")
            return

        total = sum(e["size"] for e in job)
        need = needed_bytes(job, root)
        try:
            free = shutil.disk_usage(root).free
        except OSError as err:
            dialogs.error(self, "Диск не отвечает", f"Не могу узнать свободное место:\n{err}")
            return
        if free < need:
            dialogs.error(self, "Мало места", f"Нужно {size_ru(need)}, свободно только {size_ru(free)}.")
            return

        # Показываем и полный объём группы, и остаток: иначе после обрыва
        # окно пугает сорока гигабайтами там, где качать осталось два.
        volume = size_ru(total)
        if need < total:
            volume += f" (из них уже лежит {size_ru(total - need)})"
        if not dialogs.yes_no(
            self,
            "Начать скачивание",
            f"Файлов: {len(job)}\nОбъём: {volume}\n\nПапка: {root}\n\nНачинаем?",
        ):
            return

        remember_root(root)
        self.stop_flag.clear()
        self.lock_controls(True)
        self.download_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.total_bar.setValue(0)
        self.log(f"начинаю: {len(job)} файлов, {size_ru(total)}")

        self.worker = threading.Thread(target=self.run_job, args=(job, root), daemon=True)
        self.worker.start()

    def remove(self):
        """Удаляет модели выбранных групп. Необратимо, поэтому осторожно.

        Стираются ровно те пути, что записаны в манифесте: папки не трогаются,
        рекурсивного удаления тут нет вовсе. Файл, нужный ещё и другой группе,
        пропускается - иначе снос одной группы оставил бы вторую навсегда
        неполной.
        """
        if self.running():
            dialogs.info(self, "Идёт скачивание", "Дождись конца или нажми «Отмена».")
            return
        root = self.current_root()
        if not root.is_dir():
            dialogs.error(self, "Папка не найдена", f"Нет такой папки:\n{root}")
            return

        план = removable(self.manifest, self.chosen_keys(), root)
        свои = [d for d in план if not d.shared]
        общих = len(план) - len(свои)
        if not свои:
            dialogs.info(
                self,
                "Нечего удалять",
                "Выбранных файлов на диске нет."
                + ("\n\nОстальные нужны другим группам." if общих else ""),
            )
            return

        место = sum(d.size for d in свои)
        хвост = f"\n\nПропущено как общих с другими группами: {общих}" if общих else ""
        if not dialogs.yes_no(
            self,
            "Удалить модели?",
            f"Будет удалено файлов: {len(свои)}\nОсвободится: {size_ru(место)}"
            f"\n\nПапка: {root}{хвост}"
            f"\n\nЭто необратимо. Скачивать их потом заново - "
            f"{size_ru(место)} трафика.",
            default_no=True,
        ):
            return

        ушло, осталось = remove_files(свои)
        self.log(f"удалено файлов: {len(ушло)}, освобождено "
                 f"{size_ru(sum(d.size for d in ушло))}")
        for doomed, почему in осталось:
            self.log(f"НЕ УДАЛОСЬ {doomed.dest}: {почему}")
        self.refresh()
        if осталось:
            dialogs.warning(
                self,
                "Часть файлов осталась",
                "Не удалось удалить:\n\n"
                + "\n".join(d.dest for d, _ in осталось)
                + "\n\nОбычно их держит запущенный ComfyUI. Закрой его и повтори.",
            )

    def cancel(self):
        self.stop_flag.set()
        self.cancel_button.setEnabled(False)
        self.log("отмена, дожидаюсь текущего куска")

    @staticmethod
    def part_size(root, entry):
        part = part_path(dest_path(root, entry["dest"]))
        return part.stat().st_size if part.exists() else 0

    def run_job(self, job, root):
        """Работает в отдельном потоке. Общается с окном только через очередь.

        Виджеты Qt из чужого потока трогать нельзя, поэтому всё, что видно в
        окне, уходит событиями и применяется насосом в главном потоке.

        Событие done уходит через finally: без этого любая неожиданная ошибка
        оставила бы окно с заблокированной кнопкой и без единого объяснения.
        """
        put = self.events.put
        failed = []
        cancelled = False

        try:
            # Внутри try: part_size() ходит на диск, а диск умеет исчезать.
            bars = QueueProgress([e["size"] for e in job],
                                 [self.part_size(root, e) for e in job])
            for n, entry in enumerate(job, 1):
                put(("file", f"[{n}/{len(job)}] {entry['dest']}  ({size_ru(entry['size'])})"))
                put(("log", f"качаю {entry['dest']} из {entry['repo']}"))
                bars.start_file(n - 1)

                def on_progress(done, size, speed):
                    put(("progress", bars.advance(done) + (speed,)))

                try:
                    fetch(
                        hf_url(entry["repo"], entry["path"]),
                        dest_path(root, entry["dest"]),
                        entry["size"],
                        sha256=entry.get("sha256"),
                        on_progress=on_progress,
                        on_note=lambda text: put(("log", f"  {text}")),
                        should_stop=self.stop_flag.is_set,
                    )
                except Cancelled:
                    put(("log", "остановлено, недокачанный кусок сохранён для докачки"))
                    bars.cancel_file(self.part_size(root, entry))
                    cancelled = True
                    break
                except Exception as err:
                    put(("log", f"ОШИБКА {entry['dest']}: {err}"))
                    failed.append(entry["dest"])
                    put(("progress", bars.fail_file(self.part_size(root, entry)) + (0,)))
                    continue

                put(("log", f"готово {entry['dest']}"))
                put(("progress", bars.finish_file() + (0,)))
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
            self.file_label.setText(payload)
        elif kind == "progress":
            # Порядок полей задаёт core.Frame, скорость приклеивается последней:
            # её знает не арифметика очереди, а fetch().
            done, size, overall, total, left, speed = payload
            self.file_bar.setValue(round(done * 1000 / size) if size else 0)
            self.total_bar.setValue(round(overall * 1000 / total) if total else 0)
            if speed > 0:
                # Время считаем по тому, что ещё лететь по сети, а не по остатку
                # полоски: недокачанное уже на диске и времени больше не займёт.
                self.speed_label.setText(
                    f"{size_ru(speed)}/с   осталось всего примерно {eta_text(left / speed)}"
                )
            else:
                self.speed_label.setText("")
        elif kind == "added":
            self.finish_add(*payload)
        elif kind == "done":
            self.finish_job(*payload)

    def drain_events(self):
        """Насос событий обязан пережить что угодно: пока он крутится, окно живо.

        Исключение в обработчике печатается и глотается: иначе одно кривое
        событие оставило бы окно с заблокированной кнопкой и замершими полосками.
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

    def finish_job(self, failed, cancelled):
        self.lock_controls(False)
        self.download_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.speed_label.setText("")
        self.file_bar.setValue(0)
        self.refresh()

        again = "\n\nНажми «Скачать выбранное» ещё раз, докачается с того же места."
        if cancelled:
            self.file_label.setText("остановлено")
            # Отмена не должна перебивать показ уже сломавшихся файлов.
            if failed:
                dialogs.warning(self, "Часть файлов не скачалась",
                                "До остановки не удалось скачать:\n\n" + "\n".join(failed) + again)
        elif failed:
            self.file_label.setText(f"не скачалось файлов: {len(failed)}")
            dialogs.warning(self, "Часть файлов не скачалась",
                            "Не удалось скачать:\n\n" + "\n".join(failed) + again)
        else:
            self.file_label.setText("всё скачано")
            self.total_bar.setValue(1000)
            dialogs.info(self, "Готово", "Все выбранные модели на месте.")

    def closeEvent(self, event):
        if self.running():
            if not dialogs.yes_no(
                self,
                "Идёт скачивание",
                "Скачивание ещё идёт. Закрыть?\n\nНедокачанное сохранится, потом продолжится с того же места.",
                default_no=True,
            ):
                event.ignore()
                return
            self.stop_flag.set()
            # Даём потоку дописать текущий кусок: иначе поток-демон умирал бы
            # прямо на write(), и последние мегабайты буфера пропадали.
            self.worker.join(timeout=5)
        self.closing = True
        remember_window(window_geometry.encode(self))
        self.pump.stop()
        event.accept()


def find_icon():
    for folder in (app_dir(), Path(getattr(sys, "_MEIPASS", app_dir()))):
        icon = folder / "icon.ico"
        if icon.exists():
            return icon
    return None


def create_app():
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator

    app = QApplication.instance() or QApplication(sys.argv[:1])
    if "windows11" in [k.lower() for k in QStyleFactory.keys()]:
        app.setStyle("windows11")  # на Windows 10 Qt сам возьмёт windowsvista
    icon = find_icon()
    if icon:
        app.setWindowIcon(QIcon(str(icon)))
    # Русские подписи на стандартных кнопках («Да», «Нет», «Отмена»).
    translator = QTranslator(app)
    if translator.load(QLocale("ru_RU"), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.TranslationsPath)):
        app.installTranslator(translator)
    return app


def main():
    app = create_app()
    try:
        window = App()
    except Exception as err:
        dialogs.error(
            None,
            "Не удалось прочитать список моделей",
            f"Файл models.json не читается:\n\n{type(err).__name__}: {err}\n\n"
            f"Ожидается тут:\n{manifest_path()}",
        )
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
