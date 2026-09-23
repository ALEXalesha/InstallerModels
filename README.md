<div align="center">

# InstallerModels

**Puts ComfyUI models back on the disk after you deleted them to free space. Downloads straight from Hugging Face, resumes after a broken connection, checks the size and the sha256 - 15 files, 95.4 GiB, grouped by the workflow that needs them. Your own models are added by pasting a link: the size, the hash and the settings are asked of Hugging Face, not of you.**

[Download for Windows](https://github.com/ALEXalesha/InstallerModels/releases/latest) &nbsp;·&nbsp; [Русская версия этого файла](README.ru.md)

[![CI](https://github.com/ALEXalesha/InstallerModels/actions/workflows/ci.yml/badge.svg)](https://github.com/ALEXalesha/InstallerModels/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/ALEXalesha/InstallerModels?color=16a34a)](https://github.com/ALEXalesha/InstallerModels/releases/latest)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

<img src="docs/screen-comfyui.png" width="820" alt="InstallerModels window">

</div>

> **The interface is in Russian**, and so is the full manual - [README.ru.md](README.ru.md), which this file summarises. In the screenshot, "Папка ComfyUI" is the ComfyUI folder, "Скачать выбранное" downloads what is ticked, and "не установлено" means "not installed".

## What it is

A 95 GiB model set is exactly what you delete first when the disk fills up, and exactly what is annoying to get back: fifteen files from seven Hugging Face repositories, each into its own folder inside ComfyUI. This program is that list plus a downloader that survives a bad connection. There is a window and a console version, and both run on the same `core.py`, so they cannot drift apart.

Models for LM Studio are **not** downloaded: files put into a folder behind LM Studio's back may not be picked up. The second tab just shows the exact names to paste into its search.

| | |
| --- | --- |
| Groups | one group is one workflow's files: LTX 2.3 (40 GiB), Qwen Image Edit 2509 (23.7), Gemma 4 E4B (8.4), ACE-Step v1 (7.2), SDXL Juggernaut XI (6.9), Hunyuan3D 2.1 (6.9), CLIP ViT-H-14 (2.4) |
| Resume | the partial file lives next to the real one as `.part` and is never deleted, so the next run continues from the same byte |
| Integrity | the size is checked against the response header before a single byte lands, and again after the download; the sha256 is computed while the bytes flow and verified before the file takes its final name |
| No dependencies | the console version is bare standard library, Python 3.8+; the window adds PySide6 |
| Your own models | paste a Hugging Face link, in the window or with `--add`: the exact size and sha256 come from the repository listing |
| Windows builds | installer and portable, both carrying Python inside |

<img src="docs/screen-lmstudio.png" width="820" alt="The LM Studio tab">

## Console

```bash
python install.py --list            # groups, sizes, what is already installed
python install.py ltx               # install one group
python install.py --all             # install everything (95.4 GiB)
python install.py --check ltx       # per-file state: whole, partial, corrupt, missing
python install.py --verify          # recompute sha256 of what is on the disk
python install.py --remove ltx --yes
python install.py --sync-manifest --write   # refresh sizes and hashes from Hugging Face
python install.py --add "https://huggingface.co/owner/repo/blob/main/file.safetensors"
python install.py --add "https://huggingface.co/lmstudio-community/Some-GGUF" --lmstudio
python install.py --all --root "D:/ComfyUI"
```

Exit codes: `0` fine, `1` something did not download or was not found, `130` interrupted with `Ctrl+C`.

## The part worth reading: what a download has to survive

Every rule below was written after the matching failure actually happened. The [Russian README](README.ru.md) tells each story in full; this is the list.

- **Five failures in a row, not five in total.** As soon as an attempt writes one new byte, the counter resets. A hundred drops over thirty gigabytes is normal; before 1.0.3 the fifth drop killed the download even though the file was growing the whole time.
- **Progress is compared against the largest size ever seen**, not the previous one, or a server that restarts the file from zero would look like progress forever. Restarts from zero get their own counter, also five, because a server that cannot do `Range` and drops the connection used to spin in a loop with no way out.
- **Not every drop arrives as a network error.** A server that closes the socket silently ends the read early; that counts as a drop, with the byte offset written to the log.
- **404, 401 and 403 are not retried.** A renamed file should say so immediately, and a gated repository should say "accept the licence and set `HF_TOKEN`", not "check your connection". The token is read from the same variables as the official `huggingface_hub`.
- **A disk error is not a connection error.** `open()` raises the same `OSError` as a socket, and a read-only folder used to go through twenty seconds of retries before complaining about the network. The file is opened before the network part now.
- **The disk is inspected before the network is touched at all**: a folder named like the target file, a file where the `models` folder should be, a volume that will not take a new folder.
- **A busy file does not eat the download.** Windows refuses to rename over a file that a running ComfyUI holds open; thirty downloaded gigabytes used to end as `[WinError 5]`. Now the message names the culprit and the data stays in `.part`.
- **`Accept-Encoding: identity` is sent on purpose.** If a proxy compresses the response, `Content-Length` becomes the size of the archive, and a `Range` resume would seek into the wrong offsets.
- **A full disk is reported as a full disk**, not retried as a network failure. Free space is checked up front for the whole queue, minus what already lies in `.part`.
- **`dest` is validated when the manifest is read.** `Path("C:/ComfyUI") / "C:/qwe.bin"` is just `C:/qwe.bin` in Python - one typo and a 30 GiB file goes somewhere else entirely. Absolute paths, `..`, and the Windows device names (`CON`, `NUL`, `COM1`…) plus trailing spaces and dots are rejected outright: those are the names Windows accepts and then stores differently, so the file downloads again on every run, with no error and no file.

## Adding your own model (2.1)

The list ships the models one person's workflows need; anyone else needs different ones. That used to mean editing `models.json` by hand, including finding the exact size **in bytes** - and a four-byte mistake stops the download with "manifest is out of date", sending you to fix the very number you had just typed.

Now it takes a link. There is a field under the group list in the window, and `--add` in the console:

```bash
python install.py --add "https://huggingface.co/owner/repo/blob/main/model.safetensors"
```

The program asks Hugging Face for the repository listing - the same one `--sync-manifest` uses - and takes the exact size and sha256 from it. The new entry is then checked exactly like the fifteen that shipped with it.

- **Any shape of link works**: `/resolve/`, `/blob/`, `/raw/`, with `?download=true`, percent-encoded, without `https://`, or the short `owner/repo/path`. Only the `main` branch is accepted, because that is the only one the downloader can fetch - promising otherwise would be a lie.
- **The folder inside ComfyUI is a guess, and is labelled as one**: `split_files/text_encoders/...` goes to `models/text_encoders`, a `.gguf` to `models/unet` (in ComfyUI that is a quantised unet for the GGUF nodes, not a checkpoint), a name containing `lora` to `models/loras`. Override it with `--dest models/loras/mine.safetensors --group mine`.
- **A failure changes nothing.** Repository missing, file not in it, connection lost, name Windows cannot store - the manifest stays byte-for-byte as it was. Half an entry is worse than none: the next run reads the manifest whole and stops on the broken line.

**LM Studio models work the same way, except the program works the settings out itself.** All you know is the repository address, so it reads the files and their exact sizes from the listing, takes the quant from the chosen file name (preferring `Q4_K_M`), picks up the `mmproj` companion by name - without it the model stops accepting images - and derives the `lms load` key from `base_model` in the repository card: `google/gemma-4-E2B-it` gives `google/gemma-4-e2b`. Whatever was guessed rather than read is listed as a guess in the report.

## Tests

```bash
.venv\Scripts\python.exe tests.py
```

86 checks, about 16 seconds, no network: Hugging Face is played by a local server that can be told to drop the connection, report the wrong size or go quiet.

The three files are built on different principles, which is the point:

- **`tests.py` is a list of failures that already happened.** Each check was written after a bug was found and is named so that the report says what broke. By construction, such a check catches its bug only the second time.
- **`tests_matrix.py` knows no specific bug at all.** It walks 480 combinations of how the server can behave and what can be on the disk, and in every cell demands the same seven rules - "only a readable error comes out, never a Python traceback", "a wrong body never appears under the final name". Rules like that also catch what nobody foresaw. The same file walks 1884 queues for the window's progress bars and 264 ways to corrupt the manifest.
- **`tests_props.py` does not even know the space.** It describes what an input can look like - a link, a repository listing, the state of the manifest - and hypothesis looks for the one that breaks a rule, then shrinks it to the shortest example. The first run found three real bugs: the quantisation was taken from the first match in the file name, not the last (`Qwen3-Q8-preview-Q4_K_M.gguf` would have been recorded as `Q8`); a file named like a Windows device was explained as a complaint about `models.json`, which the user had not touched; and `https://example.com/a/b/...` was parsed as the repository `example.com/a`.

Behind seven of the 86 checks there are 6530 generated cases. The window is covered too: widget construction, the event pump, the three ways a download can end, 300 random actions in a row with the invariants checked after each one, the window-title lookup done exactly as the installer's `FindWindow` does it, and a resize speed budget.

Details of the invariants, the axes of the sweep and the mutation run: [docs/tests.md](docs/tests.md).

## Building the Windows binaries

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1
```

Creates the project's own `.venv`, installs the dependencies, runs every check, and writes `dist/InstallerModels-Setup-<version>.exe` and `dist/InstallerModels-portable-<version>.zip`. NSIS makes the installer. See [docs/build.md](docs/build.md).

The title, the program name and the version are written down exactly once, in `core.py`; `build.py` generates `version.nsh` from them for NSIS. A check of the form "A equals B" is a symptom - the same fact is recorded twice. Keeping copies in sync never gets cheaper than not having copies.

## Editing the manifest by hand

Hand-editing still works, and the program still guesses nothing - `models.json` is the whole truth. It is just no longer the only way in; see "Adding your own model" above.

```json
{
  "dest": "models/loras/my-lora.safetensors",
  "repo": "author/repository",
  "path": "path/inside/the/repo.safetensors",
  "size": 123456789,
  "sha256": "..."
}
```

`size` and `sha256` do not have to be typed by hand - `python install.py --sync-manifest --write` asks Hugging Face for them, one request per repository rather than per file. Full file list: [docs/models.md](docs/models.md).

## Known limits

One file at a time, one connection. A parallel downloader would be faster but would need dependencies, and the point is that it runs on bare Python.

`--check` compares sizes only: hashing 95 GiB on every check is slow, and the hash is verified anyway at the one moment the file appears. `--verify` is the separate, slow command that re-reads everything - about twenty minutes for the full set. A corrupt file will not re-download by itself, because its size is right; `--verify` says so plainly, and deleting it is left to you.

The branch is always `main`. If the author of a repository renames a file, the download fails with 404 and `path` in `models.json` has to be fixed.

## Screenshots are generated

`tools/make_screenshots.py` opens the real window, ticks two groups, plays a few progress frames through the same event queue a live download uses, and captures the widget with `QWidget.grab()`, one frame per tab. No network is touched: the frames come from `core.QueueProgress` and the sizes in the manifest.

A screen grab would be wrong twice over. A window that just opened can sit behind others, and then the shot catches someone else's content - and the previous hand-made shots showed `D:\ComfyUI` with "131.2 GiB free" on a machine that has no D: drive, that is, a state the program could not have produced.

## Stack

Python · PySide6 (Qt 6) · PyInstaller · NSIS · Hugging Face HTTP API

## Licence

MIT, see [LICENSE](LICENSE).
