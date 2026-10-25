# Changelog

What changed for the user in each release. The full text of each release, in English and
Russian, is on the [releases page](https://github.com/ALEXalesha/InstallerModels/releases).

## 2.2.0 - 2026-09-24 - The window opens where and how large it was closed, maximized too

- The geometry is saved when the window closes and restored at the next start. A damaged value, or a window whose title bar would not be on any screen, gives the default size.
- `settings.json` is now written key by key through a temporary file: before, choosing a folder with Browse rewrote the whole file with that one key, and would have wiped the window's place.

## 2.1.0 - 2026-09-22

**Your own models are now added by pasting a Hugging Face link** - in the window, on both tabs, or with `--add` in the console.

Until now that meant editing `models.json` by hand, including finding the exact size **in bytes**: a four-byte mistake stops the download with "manifest is out of date", which sent you to fix the very number you had just typed. The program already knew how to ask the server for sizes and hashes - `--sync-manifest` does exactly that. The only thing it could not do was take a link.

```bash
python install.py --add "https://huggingface.co/owner/repo/blob/main/model.safetensors"
python install.py --add "https://huggingface.co/lmstudio-community/Some-Model-GGUF" --lmstudio
```

- **Any shape of link**: `/resolve/`, `/blob/`, `/raw/`, `?download=true`, percent-encoded, no `https://`, or the short `owner/repo/path`. Only `main`, because that is the only branch the downloader can fetch.
- **The folder inside ComfyUI is guessed** from the repository layout and labelled as a guess; `--dest` and `--group` override it.
- **For LM Studio the settings are worked out, not asked**: files and their exact sizes from the listing, the quant from the file name, the `mmproj` companion by name, and the `lms load` key from `base_model` in the repository card.
- **A failure changes nothing**: the manifest stays byte-for-byte as it was.

**Tests: 65 → 86.** The new ones include a third suite, `tests_props.py`, built on hypothesis: it describes what the input can be rather than listing examples. Its first run found three real bugs - the quant was read from the first match in a name instead of the last, a file named `CON.safetensors` was blamed on `models.json`, and `https://example.com/a/b/...` parsed as the repository `example.com/a`.

## 2.0.0 - 2026-09-22

The window was rewritten from Tkinter to Qt (PySide6).

**Why:** in Tk every widget is a separate Windows window, and the program visibly lagged while being resized. Qt draws the window as one thing. It also follows the Windows light/dark theme by itself, and the buttons in dialogs are labelled in Russian.

**Also in 2.0**

- The model key used by `lms load` and the LM Studio API is shown next to each model - it was in `models.json` and in the docs, but nowhere in the program.
- "Copy" survives closing the window: Qt hands the clipboard over to the system on exit. On Tk, "copy and close" meant there was nothing to paste - exactly the sequence the button is for.
