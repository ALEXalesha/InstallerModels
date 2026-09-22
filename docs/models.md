# Полный список моделей ComfyUI

Таблица собрана из `models.json`. Размеры сняты с Hugging Face и совпадают с тем, что лежало на диске, байт в байт.

Кроме размера у каждой записи в `models.json` есть `sha256` - контрольная сумма, которую отдаёт сам Hugging Face. Она считается на лету при скачивании и сверяется до того, как файл встанет на место. В таблицу суммы не вынесены: шестьдесят четыре знака на строку читать невозможно, а сверяет их программа.

Таблицу не надо переписывать руками, когда автор пересоберёт модель:

```bash
python install.py --sync-manifest --write
```

обновит размеры и суммы в `models.json` за один заход, а `--verify` пересчитает суммы у того, что уже лежит на диске. Подробности - в [README](../README.md#если-поменялся-состав-моделей).

## ltx - LTX 2.3 - video (image/audio to video)

Воркфлоу: `video_ltx2_3_ia2v.json`

Файлов: 5, всего 40.0 GiB

| Куда кладётся | Репозиторий | Файл в репозитории | Размер |
|---|---|---|---|
| `models/checkpoints/ltx-2.3-22b-dev-fp8.safetensors` | [Lightricks/LTX-2.3-fp8](https://huggingface.co/Lightricks/LTX-2.3-fp8) | `ltx-2.3-22b-dev-fp8.safetensors` | 27.1 GiB |
| `models/latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors` | [Lightricks/LTX-2.3](https://huggingface.co/Lightricks/LTX-2.3) | `ltx-2.3-spatial-upscaler-x2-1.1.safetensors` | 949.6 MiB |
| `models/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors` | [Comfy-Org/ltx-2.3](https://huggingface.co/Comfy-Org/ltx-2.3) | `split_files/loras/ltx_2.3_22b_distilled_1.1_lora_dynamic_fro09_avg_rank_111_bf16.safetensors` | 2.6 GiB |
| `models/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors` | [Comfy-Org/ltx-2](https://huggingface.co/Comfy-Org/ltx-2) | `split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors` | 8.8 GiB |
| `models/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors` | [Comfy-Org/ltx-2](https://huggingface.co/Comfy-Org/ltx-2) | `split_files/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors` | 599.1 MiB |

## qwen - Qwen Image Edit 2509 - image editing

Воркфлоу: `image_qwen_image_edit_2509.json`

Файлов: 4, всего 23.7 GiB

| Куда кладётся | Репозиторий | Файл в репозитории | Размер |
|---|---|---|---|
| `models/unet/Qwen-Image-Edit-2509-Q5_K_M.gguf` | [QuantStack/Qwen-Image-Edit-2509-GGUF](https://huggingface.co/QuantStack/Qwen-Image-Edit-2509-GGUF) | `Qwen-Image-Edit-2509-Q5_K_M.gguf` | 13.9 GiB |
| `models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI) | `split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors` | 8.7 GiB |
| `models/vae/qwen_image_vae.safetensors` | [Comfy-Org/Qwen-Image_ComfyUI](https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI) | `split_files/vae/qwen_image_vae.safetensors` | 242.0 MiB |
| `models/loras/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors` | [lightx2v/Qwen-Image-Lightning](https://huggingface.co/lightx2v/Qwen-Image-Lightning) | `Qwen-Image-Edit-2509/Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors` | 810.2 MiB |

## sdxl - SDXL Juggernaut XI - image generation

Воркфлоу: `Alex gen images.json`

Файлов: 2, всего 6.9 GiB

| Куда кладётся | Репозиторий | Файл в репозитории | Размер |
|---|---|---|---|
| `models/checkpoints/juggernautXL_juggXIByRundiffusion.safetensors` | [RunDiffusion/Juggernaut-XI-v11](https://huggingface.co/RunDiffusion/Juggernaut-XI-v11) | `Juggernaut-XI-byRunDiffusion.safetensors` | 6.6 GiB |
| `models/vae/sdxl_vae.safetensors` | [stabilityai/sdxl-vae](https://huggingface.co/stabilityai/sdxl-vae) | `sdxl_vae.safetensors` | 319.1 MiB |

## gemma - Gemma 4 E4B - text generation inside ComfyUI

Воркфлоу: `llm_gemma4_text_gen.json`

Файлов: 1, всего 8.4 GiB

| Куда кладётся | Репозиторий | Файл в репозитории | Размер |
|---|---|---|---|
| `models/text_encoders/gemma4_e4b_it_fp8_scaled.safetensors` | [Comfy-Org/gemma-4](https://huggingface.co/Comfy-Org/gemma-4) | `text_encoders/gemma4_e4b_it_fp8_scaled.safetensors` | 8.4 GiB |

## audio - ACE-Step v1 3.5B - music generation

Воркфлоу: `audio_ace_step_1_t2a_song.json`

Файлов: 1, всего 7.2 GiB

| Куда кладётся | Репозиторий | Файл в репозитории | Размер |
|---|---|---|---|
| `models/checkpoints/ace_step_v1_3.5b.safetensors` | [Comfy-Org/ACE-Step_ComfyUI_repackaged](https://huggingface.co/Comfy-Org/ACE-Step_ComfyUI_repackaged) | `all_in_one/ace_step_v1_3.5b.safetensors` | 7.2 GiB |

## hunyuan3d - Hunyuan3D 2.1 - image to 3D mesh

Воркфлоу: `3d_hunyuan3d-v2.1.json`

Файлов: 1, всего 6.9 GiB

| Куда кладётся | Репозиторий | Файл в репозитории | Размер |
|---|---|---|---|
| `models/checkpoints/hunyuan_3d_v2.1.safetensors` | [Comfy-Org/hunyuan3D_2.1_repackaged](https://huggingface.co/Comfy-Org/hunyuan3D_2.1_repackaged) | `hunyuan_3d_v2.1.safetensors` | 6.9 GiB |

## clipvision - CLIP ViT-H-14 vision encoder (no saved workflow uses it)

Воркфлоу: нет, файл лежит про запас

Файлов: 1, всего 2.4 GiB

| Куда кладётся | Репозиторий | Файл в репозитории | Размер |
|---|---|---|---|
| `models/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | [Comfy-Org/CLIP-ViT-H-14-laion2B-s32B-b79K_repackaged](https://huggingface.co/Comfy-Org/CLIP-ViT-H-14-laion2B-s32B-b79K_repackaged) | `split_files/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors` | 2.4 GiB |

## Итого

15 файлов  95.4 GiB (102 472 923 377 байт).

## Как добавить свою модель (с версии 2.1)

Править `models.json` руками больше не нужно. В окне, на вкладке ComfyUI, под списком групп есть поле «Своя модель: ссылка на файл в Hugging Face». В консоли то же самое:

```bash
python install.py --add "https://huggingface.co/автор/репозиторий/blob/main/файл.safetensors"
```

Программа спрашивает у Hugging Face опись репозитория - ту же, по которой работает `--sync-manifest`, - и берёт оттуда точный размер в байтах и sha256. Именно ради этого команда и появилась: руками размер добывался по одному `curl` на файл, а ошибка на четыре байта останавливает скачивание сообщением «manifest is out of date», то есть человека отправляли чинить число, которое он же и переписал.

Папку внутри ComfyUI программа угадывает по тому, как файлы разложены в репозитории: `split_files/text_encoders/...` кладётся в `models/text_encoders`, `.gguf` - в `models/unet` (в ComfyUI это квантованный unet для GGUF-нод, а не чекпойнт), файл со словом `lora` в имени - в `models/loras`. Догадка называется догадкой в выводе, и её можно не угадывать:

```bash
python install.py --add "ссылка" --dest models/loras/моя-lora.safetensors --group свои
```

Без `--group` файл ложится в группу `custom` («Добавленные вручную»), она создаётся при первом добавлении. Ссылку понимают в любом виде, в каком её копируют: `/resolve/`, `/blob/`, `/raw/`, с хвостом `?download=true`, с процентными кодами и короткую запись `автор/репозиторий/путь`. Ветка только `main` - другую программа качать не умеет и потому честно отказывается.

`--dry-run` показывает, что добавилось бы, и ничего не записывает.

## Чего в этом списке нет

В воркфлоу `video_ltx2_3_ia2v.json` упоминаются ещё два файла:

- `gemma-3-12b-it-qat-q4_0-unquantized_readout_proj/model/model.safetensors`
- `ltx-av-step-1751000_vocoder_24K.safetensors`

На диске в папке `models/` их не было, и в кеше Hugging Face тоже. Скорее всего нода качает их сама при первом запуске. В манифест они не попали, потому что я не смог проверить, откуда именно они берутся. Если LTX ругнётся на их отсутствие, надо смотреть, какая нода их просит.

Отдельно от папки `models/` лежит кеш Hugging Face: `C:/Users/User/.cache/huggingface`, около 7.8 ГиБ (F5-TTS, vocos-mel-24khz, sdxl-turbo). Его качают ноды сами по себе, поэтому в манифест он не входит.
