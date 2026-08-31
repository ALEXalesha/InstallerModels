# Модели LM Studio

Скрипт их не качает. Причина простая: LM Studio держит свой индекс моделей, и файлы, положенные в папку мимо приложения, оно может не увидеть, пока не переиндексирует. Скачать через сам LM Studio надёжнее и занимает две минуты.

Ниже точные названия. Их можно вбить в поиск внутри LM Studio или отдать команде `lms`.

## gemma-4 E2B Instruct

Вбить в поиск: `lmstudio-community/gemma-4-E2B-it-GGUF`

Выбрать квант: **Q4_K_M**

Ключ модели в LM Studio: `google/gemma-4-e2b`

Файлы, которые появятся:

| Файл | Размер |
|---|---|
| `gemma-4-E2B-it-Q4_K_M.gguf` | 3.2 ГиБ |
| `mmproj-gemma-4-E2B-it-BF16.gguf` | 941 МиБ |

## Qwen3-VL 4B Instruct

Вбить в поиск: `lmstudio-community/Qwen3-VL-4B-Instruct-GGUF`

Выбрать квант: **Q4_K_M**

Ключ модели в LM Studio: `qwen/qwen3-vl-4b`

Файлы, которые появятся:

| Файл | Размер |
|---|---|
| `Qwen3-VL-4B-Instruct-Q4_K_M.gguf` | 2.3 ГиБ |
| `mmproj-Qwen3-VL-4B-Instruct-F16.gguf` | 797 МиБ |

## Про mmproj

Обе модели умеют смотреть картинки. За это отвечает отдельный файл `mmproj` - проектор, который переводит картинку в токены для языковой модели. LM Studio качает его вместе с основным файлом сам, отдельно искать не надо. Но если модель вдруг перестанет принимать изображения, стоит проверить, что `mmproj` лежит рядом с основным `.gguf`.

## Через консоль

Если установлен LM Studio CLI:

```bash
lms get lmstudio-community/gemma-4-E2B-it-GGUF
```

```bash
lms get lmstudio-community/Qwen3-VL-4B-Instruct-GGUF
```

Посмотреть, что уже скачано:

```bash
lms ls
```

## Куда LM Studio их кладёт

```
C:\Users\User\.lmstudio\models\lmstudio-community\gemma-4-E2B-it-GGUF\
C:\Users\User\.lmstudio\models\lmstudio-community\Qwen3-VL-4B-Instruct-GGUF\
```

Рядом есть папка `C:\Users\User\.lmstudio\hub\models\` - там лежат только манифесты и настройки моделей, весов в ней нет. Её удалять не надо, она весит копейки.

## Быстрая справка из скрипта

Те же названия, кванты и ключи моделей показывает вкладка «LM Studio» в окне программы, с кнопкой «Копировать». Или командой:

```bash
python install.py --lmstudio
```
