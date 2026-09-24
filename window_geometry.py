"""Размер и место окна между запусками.

Один и тот же файл в Qt-проектах (CheckProg, InstallerModels, Converter, AlexGPT); работает
и с PySide6, и с PyQt6 - привязку не импортирует, берёт всё у самого окна.

Основную работу делает Qt: saveGeometry() запоминает место, размер и «развёрнуто»,
restoreGeometry() ставит окно обратно и, если того монитора больше нет, переносит окно на
имеющийся экран. Здесь - строка для файла настроек и проверка сверху: испорченная строка
или окно, заголовок которого не попал ни на один экран, оставляют окно как было (размер
по умолчанию, по центру), а не где-то за краем.
"""
import base64
import binascii

# Сколько окна должно остаться на экране, чтобы за него можно было взяться: полоса
# заголовка высотой 38 и хотя бы 80 по ширине. То же правило, что в window-state.js
# калькуляторов и Paint Pro.
GRIP_HEIGHT = 38
GRIP_WIDTH = 80


def encode(window):
    """Место, размер и «развёрнуто» окна строкой для файла настроек."""
    return base64.b64encode(bytes(window.saveGeometry())).decode("ascii")


def title_on_screen(window):
    """Полоса заголовка окна хоть на одном экране настолько, что за неё можно взяться."""
    frame = window.frameGeometry()
    screen = window.screen()
    if screen is None:
        return False
    for s in screen.virtualSiblings():
        area = s.availableGeometry()
        w = min(frame.x() + frame.width(), area.x() + area.width()) - max(frame.x(), area.x())
        h = min(frame.y() + GRIP_HEIGHT, area.y() + area.height()) - max(frame.y(), area.y())
        if w >= GRIP_WIDTH and h >= GRIP_HEIGHT / 2:
            return True
    return False


def restore(window, text):
    """Поставить окно по сохранённой строке. True - встало; False - строки нет, она
    испорчена или окно оказалось бы там, где его не видно: тогда окно как было."""
    if not isinstance(text, str) or not text:
        return False
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return False
    before = window.saveGeometry()
    if not window.restoreGeometry(type(before)(data)):
        return False
    if not window.isMaximized() and not title_on_screen(window):
        window.restoreGeometry(before)
        return False
    return True
