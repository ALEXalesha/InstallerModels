#!/usr/bin/env python3
r"""Снимки окна для README: собираются программой, а не руками.

    .venv\Scripts\python.exe tools\make_screenshots.py

Скрипт поднимает настоящее окно, отмечает две группы, проигрывает через ту же
очередь событий, что и живая закачка, несколько кадров прогресса и снимает виджет
через QWidget.grab() - по кадру на вкладку.

Снимок области экрана не годится дважды. Окно может оказаться позади других, и в
кадр попадёт чужое содержимое. А ещё в прошлых снимках стояла папка D:\ComfyUI и
«свободно 131.2 ГиБ» - диска D на машине нет, то есть картинка показывала то,
чего программа показать не могла.

Настоящую сеть скрипт не трогает: кадры прогресса считает core.QueueProgress по
размерам из манифеста, ничего не скачивая. LOCALAPPDATA подменяется на временную
папку, поэтому settings.json с выбранной папкой у автора не меняется.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs"
sys.path.insert(0, str(ROOT))

# Папка ComfyUI для снимка: настоящая, пустая и с коротким именем. Настоящая -
# потому что окно спрашивает у неё свободное место и состояние каждой группы, а у
# несуществующей честно пишет «папка не найдена» красным.
SHOT_ROOT = Path("C:/ComfyUI")

PICKED = ("ltx", "sdxl")


def prepare_env(tmp: str) -> None:
    os.environ["LOCALAPPDATA"] = tmp
    os.environ["COMFYUI_ROOT"] = str(SHOT_ROOT)


def play_download(app, core) -> None:
    """Прогоняет начало закачки: те же события, что кладёт в очередь поток."""
    from gui import size_ru
    from core import pending

    job = pending(app.manifest, list(PICKED), SHOT_ROOT)
    bars = core.QueueProgress([e["size"] for e in job], [0] * len(job))
    first = job[0]

    app.lock_controls(True)
    app.download_button.setEnabled(False)
    app.cancel_button.setEnabled(True)
    app.log(f"начинаю: {len(job)} файлов, {size_ru(sum(e['size'] for e in job))}")
    app.log(f"качаю {first['dest']}")
    app.events.put(("file", f"[1/{len(job)}] {first['dest']}  ({size_ru(first['size'] // 3)})"))
    app.events.put(("progress", bars.advance(first["size"] // 3) + (43.7 * 1024 * 1024,)))
    app.drain_events()


def main() -> None:
    made_root = not SHOT_ROOT.exists()
    if made_root:
        SHOT_ROOT.mkdir(parents=True)
    tmp = tempfile.mkdtemp(prefix="installer-models-shots-")
    try:
        prepare_env(tmp)
        import core
        from gui import App, create_app

        app = create_app()
        win = App()
        win.resize(880, 720)
        win.show()
        app.processEvents()

        for key in PICKED:
            win.rows[key].set_picked(True)
        win.update_selection()
        play_download(win, core)
        for _ in range(5):
            app.processEvents()

        for index, name in ((0, "screen-comfyui.png"), (1, "screen-lmstudio.png")):
            win.tabs.setCurrentIndex(index)
            for _ in range(5):
                app.processEvents()
            shot = OUT / name
            win.grab().save(str(shot))
            print(f"  {name} ({shot.stat().st_size // 1024} КБ)")

        win.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        # Убираем за собой только то, что сами и создали: у человека в C:\ComfyUI
        # может стоять настоящий ComfyUI на сотню гигабайт.
        if made_root and SHOT_ROOT.is_dir() and not any(SHOT_ROOT.iterdir()):
            SHOT_ROOT.rmdir()
    print(f"Готово: {OUT}")


if __name__ == "__main__":
    main()
