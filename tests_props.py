#!/usr/bin/env python3
"""Свойства добавления моделей по ссылке: гипотезы вместо примеров по одному.

`tests.py` хранит проверки на поломки, которые уже случались. `tests_matrix.py`
перебирает пространство состояний скачивания. Этот файл - третий способ: он не
знает ни одного конкретного входа. Он описывает, каким вход бывает вообще, а
hypothesis сама ищет тот, на котором правило ломается, и, найдя, ужимает его до
кратчайшего.

Правила тут ровно те, которые обязаны держаться всегда:

  * разбор ссылки обратим: из какого бы вида ссылки ни собрали адрес, из него
    вынимаются тот же репозиторий и тот же путь;
  * догадка о папке внутри ComfyUI всегда даёт dest, который принимает
    dest_parts, и всегда сохраняет имя файла;
  * добавление либо проходит целиком, либо не меняет манифест ни на байт -
    третьего не дано; после удачного манифест обязан проходить check_manifest;
  * наружу вылетает только ValueError или RuntimeError: трассировка Python на
    человека, который вставил ссылку, - это не сообщение об ошибке.

Запускается сам по себе: python tests_props.py
"""

import copy
import json
import random
import sys

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import core

ПРОГОН = settings(max_examples=250, deadline=None,
                  suppress_health_check=[HealthCheck.function_scoped_fixture])

# Из чего Hugging Face складывает имена: латиница, цифры, точка, дефис,
# подчёркивание, и первый знак - буква или цифра. Кириллических имён там не
# бывает, и программа их не принимает: такая «ссылка» - это опечатка.
АЛФАВИТ = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
ИМЯ = st.builds(lambda п, х: п + х,
                st.sampled_from(АЛФАВИТ),
                st.text(alphabet=АЛФАВИТ + "-_.", max_size=19))
# Имена, которые Windows принимает, но хранит не так, - отдельная история: под
# ними файл на диск не положить вовсе, и про них есть своё свойство ниже.
ЧАСТЬ_ПУТИ = ИМЯ.filter(lambda s: s not in (".", "..") and s.rstrip(" .") == s
                        and s.split(".")[0].upper() not in core.DEVICE_NAMES)
ПУТЬ = st.lists(ЧАСТЬ_ПУТИ, min_size=1, max_size=4).map("/".join)
# Имя автора без точек. В короткой записи «автор/репозиторий/файл» точки нет
# как разделителя, и «example.com/a/b» иначе не отличить от адреса чужого сайта:
# программа такую ссылку отвергает - и правильно делает. У автора на Hugging Face
# точек в имени не бывает, а у репозитория (model.v2) бывают.
ВЛАДЕЛЕЦ = ИМЯ.filter(lambda s: "." not in s)
РЕПО = st.tuples(ВЛАДЕЛЕЦ, ИМЯ).map(lambda p: f"{p[0]}/{p[1]}")
РАЗМЕР = st.integers(min_value=1, max_value=1 << 45)
SHA = st.one_of(st.none(), st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))

ВИДЫ = ("https://huggingface.co/{repo}/resolve/main/{path}",
        "https://huggingface.co/{repo}/blob/main/{path}",
        "https://huggingface.co/{repo}/raw/main/{path}",
        "http://hf.co/{repo}/resolve/main/{path}",
        "huggingface.co/{repo}/resolve/main/{path}",
        "https://huggingface.co/api/models/{repo}/resolve/main/{path}",
        "{repo}/{path}",
        "  https://huggingface.co/{repo}/resolve/main/{path}?download=true  ",
        "https://huggingface.co/{repo}/blob/main/{path}#файл")


def пустой_манифест():
    return {"comfyui_root": "C:/ComfyUI",
            "groups": {"ltx": {"title": "LTX", "title_ru": "LTX",
                               "files": [{"dest": "models/checkpoints/a.safetensors",
                                          "repo": "author/repo", "path": "a.safetensors",
                                          "size": 100}]}},
            "lmstudio": []}


# ------------------------------------------------------------- разбор ссылки

@ПРОГОН
@given(repo=РЕПО, path=ПУТЬ, вид=st.sampled_from(ВИДЫ))
def ссылка_читается_в_любом_виде(repo, path, вид):
    ref = core.parse_hf_ref(вид.format(repo=repo, path=path))
    assert ref.repo == repo, (вид, ref)
    assert ref.path == path, (вид, ref)


@ПРОГОН
@given(repo=РЕПО, path=ПУТЬ)
def процентные_коды_разворачиваются(repo, path):
    from urllib.parse import quote

    адрес = f"https://huggingface.co/{repo}/resolve/main/{quote(path)}"
    assert core.parse_hf_ref(адрес).path == path


@ПРОГОН
@given(repo=РЕПО, path=ПУТЬ,
       ветка=ИМЯ.filter(lambda s: s != "main"))
def чужая_ветка_всегда_отвергается(repo, path, ветка):
    try:
        core.parse_hf_ref(f"https://huggingface.co/{repo}/resolve/{ветка}/{path}")
    except ValueError as err:
        assert "main" in str(err), err
    else:
        raise AssertionError(f"ветка {ветка} принята, а качать программа умеет только main")


@ПРОГОН
@given(text=st.text(max_size=60))
def мусор_не_роняет_разбор(text):
    """Что угодно в поле ввода - это либо разобранная ссылка, либо ValueError.
    Никакого IndexError на пустом списке частей и никакого AttributeError."""
    try:
        ref = core.parse_hf_ref(text)
    except ValueError:
        return
    assert ref.repo.count("/") == 1 and ref.path


# ---------------------------------------------------- догадка о папке ComfyUI

@ПРОГОН
@given(path=ПУТЬ)
def догадка_о_папке_всегда_годится(path):
    dest = core.suggest_dest(path)
    части = core.dest_parts(dest)          # не бросает - иначе запись не легла бы
    assert части[0] == "models", dest
    assert len(части) == 3, dest
    assert части[-1] == path.rsplit("/", 1)[-1], (path, dest)


@ПРОГОН
@given(path=ПУТЬ, folder=st.sampled_from(core.COMFY_FOLDERS))
def папка_из_репозитория_сильнее_догадки_по_имени(path, folder):
    """Автор репозитория уже разложил файлы так, как их кладут в ComfyUI:
    split_files/text_encoders/... - и это знание сильнее догадки по имени."""
    dest = core.suggest_dest(f"split_files/{folder}/{path.rsplit('/', 1)[-1]}")
    assert dest.split("/")[1] == folder, dest


@ПРОГОН
@given(устройство=st.sampled_from(sorted(core.DEVICE_NAMES)),
       хвост=st.sampled_from(["", ".safetensors", ".gguf"]))
def файл_с_именем_устройства_объясняется_по_человечески(устройство, хвост):
    """Файл, названный в репозитории CON или COM1, Windows не сохранит: запись в
    него уходит в никуда. Жаловаться на «плохой dest в models.json» тут нельзя -
    манифест человек не правил, он вставил ссылку.
    """
    имя = f"{устройство}{хвост}"
    manifest = пустой_манифест()
    было = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    try:
        core.add_model(manifest, f"author/repo/{имя}",
                       listing=lambda _r: {имя: core.Remote(10, None)})
    except ValueError as err:
        assert "models.json" not in str(err), err
        assert "--dest" in str(err), err
    else:
        raise AssertionError(f"имя устройства {имя} прошло в манифест")
    assert json.dumps(manifest, ensure_ascii=False, sort_keys=True) == было


# ------------------------------------------------- добавление файла в манифест

@ПРОГОН
@given(repo=РЕПО, path=ПУТЬ, size=РАЗМЕР, sha=SHA, группа=st.one_of(st.none(), ИМЯ))
def удачное_добавление_оставляет_манифест_годным(repo, path, size, sha, группа):
    manifest = пустой_манифест()
    было = copy.deepcopy(manifest)
    listing = lambda _repo: {path: core.Remote(size, sha)}   # noqa: E731

    added = core.add_model(manifest, f"{repo}/{path}", group=группа, listing=listing)

    core.check_manifest(manifest)
    записи = [e for g in manifest["groups"].values() for e in g["files"]]
    старые = [e for g in было["groups"].values() for e in g["files"]]
    assert len(записи) == len(старые) + 1
    новая = manifest["groups"][added.group]["files"][-1]
    assert новая["size"] == size and новая["repo"] == repo and новая["path"] == path
    assert новая.get("sha256") == sha
    assert added.dest == новая["dest"]


@ПРОГОН
@given(repo=РЕПО, path=ПУТЬ, size=РАЗМЕР,
       беда=st.sampled_from(["нет файла", "закрыт", "нет связи", "плохой dest"]))
def неудачное_добавление_не_меняет_манифест(repo, path, size, беда):
    """Половина записи в манифесте хуже, чем её отсутствие: следующий запуск
    прочитает манифест целиком, и кривая запись остановит программу на загрузке.
    """
    manifest = пустой_манифест()
    было = json.dumps(manifest, ensure_ascii=False, sort_keys=True)

    def listing(_repo):
        if беда == "закрыт":
            raise RuntimeError("HTTP 403 - репозиторий закрыт или требует лицензии")
        if беда == "нет связи":
            raise RuntimeError("нет связи с Hugging Face")
        return {} if беда == "нет файла" else {path: core.Remote(size, None)}

    dest = "C:/мимо/ComfyUI.safetensors" if беда == "плохой dest" else None
    try:
        core.add_model(manifest, f"{repo}/{path}", dest=dest, listing=listing)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError(f"беда {беда!r} прошла незамеченной")
    стало = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    assert стало == было, f"манифест изменился после неудачи ({беда})"


@ПРОГОН
@given(repo=РЕПО, path=ПУТЬ, size=РАЗМЕР)
def один_и_тот_же_файл_дважды_не_добавляется(repo, path, size):
    manifest = пустой_манифест()
    listing = lambda _repo: {path: core.Remote(size, None)}   # noqa: E731
    core.add_model(manifest, f"{repo}/{path}", listing=listing)
    слепок = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    try:
        core.add_model(manifest, f"{repo}/{path}", listing=listing)
    except ValueError as err:
        assert "уже" in str(err), err
    else:
        raise AssertionError("тот же файл добавился дважды")
    assert json.dumps(manifest, ensure_ascii=False, sort_keys=True) == слепок


# ------------------------------------------------------ раздел LM Studio

КВАНТЫ = ("Q4_K_M", "Q4_K_S", "Q5_K_M", "Q6_K", "Q8_0", "IQ4_XS")


@ПРОГОН
@given(repo=РЕПО, основа=ИМЯ, кванты=st.lists(st.sampled_from(КВАНТЫ), min_size=1,
                                              max_size=6, unique=True),
       mmproj=st.booleans(), размеры=st.lists(РАЗМЕР, min_size=7, max_size=7))
def модель_lmstudio_собирается_из_описи(repo, основа, кванты, mmproj, размеры):
    """Настройки модели программа выясняет, а не спрашивает: файлы, их размеры,
    квант и спутник mmproj берутся из описи репозитория."""
    файлы = {f"{основа}-{q}.gguf": core.Remote(s, None) for q, s in zip(кванты, размеры)}
    if mmproj:
        файлы[f"mmproj-{основа}-F16.gguf"] = core.Remote(размеры[-1], None)
    файлы["README.md"] = core.Remote(100, None)      # не GGUF - в модель не попадает

    manifest = пустой_манифест()
    added = core.add_lmstudio(manifest, repo, listing=lambda _r: файлы,
                              card=lambda _r: {})

    core.check_manifest(manifest)
    assert added.quant in кванты, (added.quant, кванты)
    assert all(имя.endswith(".gguf") for имя in added.files)
    assert added.total == sum(файлы[и].size for и in added.files)
    assert (len(added.files) == 2) == mmproj
    # Q4_K_M - то, чем пользуются по умолчанию: если он есть, берётся он.
    if "Q4_K_M" in кванты:
        assert added.quant == "Q4_K_M"


# Своего суффикса варианта в имени модели быть не должно: срезается ровно один,
# и «gemma-chat-it» превратить в «gemma» было бы уже не переводом имени, а
# выдумыванием. Настоящие имена несут один суффикс - «-it» или «-Instruct».
БЕЗ_СУФФИКСА = ИМЯ.filter(
    lambda s: not any(s.lower().endswith(х) for х in core.LMS_SUFFIXES))


@ПРОГОН
@given(repo=РЕПО, основа=ИМЯ, vendor=ИМЯ, модель=БЕЗ_СУФФИКСА,
       суффикс=st.sampled_from(["", "-it", "-Instruct", "-chat"]))
def ключ_lms_всегда_в_нижнем_регистре_и_без_суффикса(repo, основа, vendor, модель, суффикс):
    файлы = {f"{основа}-Q4_K_M.gguf": core.Remote(100, None)}
    база = f"{vendor}/{модель}{суффикс}"
    manifest = пустой_манифест()
    added = core.add_lmstudio(manifest, repo, listing=lambda _r: файлы,
                              card=lambda _r: {"cardData": {"base_model": база}})
    assert added.key == f"{vendor}/{модель}".lower(), (added.key, база)
    assert not any(added.key.endswith(х) for х in core.LMS_SUFFIXES)
    assert "ключ" in added.guessed, "ключ выведен догадкой и обязан быть назван догадкой"


@ПРОГОН
@given(repo=РЕПО, беда=st.sampled_from(["без gguf", "нет связи", "дважды"]))
def неудача_в_lmstudio_не_меняет_манифест(repo, беда):
    manifest = пустой_манифест()
    файлы = {"модель-Q4_K_M.gguf": core.Remote(100, None)}

    def listing(_repo):
        if беда == "нет связи":
            raise RuntimeError("нет связи с Hugging Face")
        return {} if беда == "без gguf" else файлы

    if беда == "дважды":
        core.add_lmstudio(manifest, repo, listing=listing, card=lambda _r: {})
    было = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
    try:
        core.add_lmstudio(manifest, repo, listing=listing, card=lambda _r: {})
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError(f"беда {беда!r} прошла незамеченной")
    assert json.dumps(manifest, ensure_ascii=False, sort_keys=True) == было


# --------------------------------------------------- перебор углов, как в матрице

ССЫЛКИ = ("https://huggingface.co/author/repo/resolve/main/model.safetensors",
          "https://huggingface.co/author/repo/blob/main/sub/model.gguf?download=true",
          "author/repo/model.safetensors",
          "https://huggingface.co/author/repo",
          "https://huggingface.co/author/repo/tree/main/sub",
          "https://huggingface.co/datasets/author/repo/resolve/main/model.safetensors",
          "https://huggingface.co/author/repo/resolve/dev/model.safetensors",
          "", "   ", "просто текст", "автор/репо/файл.safetensors", "author/",
          "///", "C:/model.safetensors",
          "https://example.com/author/repo/resolve/main/model.safetensors")

ОПИСИ = ("есть файл", "пусто", "другой файл", "закрыт", "нет связи", "мусор")

DEST = (None, "models/loras/своя.safetensors", "../побег.safetensors",
        "C:/побег.safetensors", "models/CON.safetensors", "models/хвост .safetensors")


def добавление_переживает_любой_угол():
    """Тот же перебор, что и в tests_matrix: ссылка × опись × dest.

    В каждой клетке требуется одно и то же, чем бы дело ни кончилось:
    наружу вылетает только ValueError или RuntimeError, манифест либо вырос
    ровно на одну годную запись, либо не изменился вовсе.
    """
    клеток = удач = 0
    for ссылка in ССЫЛКИ:
        for опись in ОПИСИ:
            for dest in DEST:
                клеток += 1
                manifest = пустой_манифест()
                было = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
                записей = sum(len(g["files"]) for g in manifest["groups"].values())

                def listing(repo, опись=опись):
                    if опись == "закрыт":
                        raise RuntimeError("HTTP 403 - репозиторий закрыт")
                    if опись == "нет связи":
                        raise RuntimeError("нет связи с Hugging Face")
                    if опись == "мусор":
                        raise RuntimeError("ответ Hugging Face не разбирается")
                    if опись == "пусто":
                        return {}
                    if опись == "другой файл":
                        return {"quite/another.safetensors": core.Remote(5, None)}
                    return {"model.safetensors": core.Remote(5, "a" * 64),
                            "sub/model.gguf": core.Remote(7, None)}

                try:
                    added = core.add_model(manifest, ссылка, dest=dest, listing=listing)
                except (ValueError, RuntimeError):
                    стало = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
                    assert стало == было, f"{ссылка!r}/{опись}/{dest}: манифест изменился"
                    continue
                except Exception as err:                      # noqa: BLE001
                    raise AssertionError(
                        f"{ссылка!r}/{опись}/{dest}: наружу вылетел {type(err).__name__}: {err}"
                    ) from None

                core.check_manifest(manifest)
                удач += 1
                стало_записей = sum(len(g["files"]) for g in manifest["groups"].values())
                assert стало_записей == записей + 1, f"{ссылка!r}/{опись}/{dest}"
                if dest:
                    assert added.dest == dest
    # Перебор, в котором всё падает, ничего не проверяет: так уже было, когда
    # ссылки в этом списке были написаны кириллицей и ни одна не разбиралась.
    # Удача возможна на трёх ссылках из пятнадцати, одной описи из шести и двух
    # dest из шести - это девять клеток. Меньше значит, что перебор перестал
    # проверять удачный путь вовсе: ровно так и было, пока ссылки тут были
    # написаны кириллицей и ни одна из них не разбиралась.
    assert удач >= 9, f"удачных добавлений всего {удач} на {клеток} клеток"
    return клеток


def случайные_добавления_подряд():
    """Сто случайных добавлений в один манифест: он обязан оставаться годным
    после каждого, а число записей - совпадать с числом удач."""
    случай = random.Random(20260922)
    manifest = пустой_манифест()
    удач = 0
    записей = sum(len(g["files"]) for g in manifest["groups"].values())
    for шаг in range(100):
        файл = f"model{случай.randrange(6)}.safetensors"
        repo = f"author{случай.randrange(3)}/repo{случай.randrange(3)}"
        опись = {файл: core.Remote(случай.randrange(1, 10 ** 9), None)}
        группа = случай.choice([None, "mine", "ltx"])
        try:
            core.add_model(manifest, f"{repo}/{файл}", group=группа,
                           listing=lambda _r, о=опись: о)
        except (ValueError, RuntimeError):
            pass
        else:
            удач += 1
        core.check_manifest(manifest)
        стало = sum(len(g["files"]) for g in manifest["groups"].values())
        assert стало == записей + удач, f"шаг {шаг}: записей {стало}, удач {удач}"
    return 100


ГИПОТЕЗЫ = [ссылка_читается_в_любом_виде,
            процентные_коды_разворачиваются,
            чужая_ветка_всегда_отвергается,
            мусор_не_роняет_разбор,
            догадка_о_папке_всегда_годится,
            папка_из_репозитория_сильнее_догадки_по_имени,
            файл_с_именем_устройства_объясняется_по_человечески,
            удачное_добавление_оставляет_манифест_годным,
            неудачное_добавление_не_меняет_манифест,
            один_и_тот_же_файл_дважды_не_добавляется,
            модель_lmstudio_собирается_из_описи,
            ключ_lms_всегда_в_нижнем_регистре_и_без_суффикса,
            неудача_в_lmstudio_не_меняет_манифест]


def свойства_добавления_держатся():
    """Все гипотезы одним прогоном: в отчёт идёт число проверенных примеров."""
    for проверка in ГИПОТЕЗЫ:
        проверка()
    return len(ГИПОТЕЗЫ) * ПРОГОН.max_examples


CASES = [свойства_добавления_держатся,
         добавление_переживает_любой_угол,
         случайные_добавления_подряд]


if __name__ == "__main__":
    for поток in (sys.stdout, sys.stderr):
        if hasattr(поток, "reconfigure"):
            поток.reconfigure(encoding="utf-8", errors="replace")
    failed = 0
    for check in CASES:
        try:
            count = check()
        except AssertionError as err:
            failed += 1
            print(f"ПРОВАЛ  {check.__name__}\n        {err}")
        else:
            print(f"ок      {check.__name__}  ({count} примеров)")
    sys.exit(1 if failed else 0)
