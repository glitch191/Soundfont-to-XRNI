# Soundfont-to-XRNI

Windows desktop app that converts SoundFont 2 (`.sf2`) presets and SFZ (`.sfz`) instruments into
Renoise instruments (`.xrni`), written straight into the user's Renoise User Library.
Public repo: https://github.com/glitch191/Soundfont-to-XRNI (MIT licence).

## Working with the owner

- The owner writes in French: answer in French. Everything inside the repository (code, comments,
  UI text, docs, file names) must be in English: no French anywhere in the tree.
- No em dashes in user-facing prose (README, release notes, forum posts): the owner removed them
  from the README and asked for this explicitly ("sounds like AI"). Use colons or commas.
- The owner is new to GitHub and publishes with GitHub Desktop (commit, push) and the github.com
  website (releases). Git is available in Git Bash; it was not on the PowerShell PATH earlier.
- Ask before destructive or outward-facing actions (deleting files, publishing).

## Layout

```
soundfont_to_xrni/
  __init__.py   APP_NAME, __version__ (single source of the version number)
  __main__.py   entry point: window by default, --cli for headless use, --help, --version
  engine.py     SF2 reader, classification, archive unpacking, XRNI writer, job runner
  sfz.py        SFZ parser (opcodes -> the same Region objects as SF2)
  gui.py        tkinter + tkinterdnd2 window (scan first, then Convert)
tests/          unittest suite; generates its own tiny SF2 / SFZ / WAV files
app.py          PyInstaller entry point
build.py        builds dist/Soundfont-to-XRNI.exe and the release .zip
make_icon.py    regenerates icon.ico and docs/icon.png (needs Pillow)
.github/workflows/build.yml   CI: tests on every push; on a v* tag, builds and attaches the .zip
```

## Commands

```
py -m pip install -r requirements.txt          # numpy, soundfile, tkinterdnd2, py7zr
py -m soundfont_to_xrni                        # run the window from source
py -m soundfont_to_xrni --cli PATH... [--library DIR]
py -m unittest discover -s tests -v            # 15 tests (2 drive the real window, hidden)
py -m pip install -r requirements-build.txt    # + pyinstaller, pillow
py build.py                                    # dist/Soundfont-to-XRNI.exe + dist/*-windows-x64.zip
```

Environment variables: `S2X_WORKERS=N` forces the worker count (benchmarks), `S2X_7ZIP=off`
ignores an installed 7-Zip (tests the built-in decoder).
Local Python is 3.14; CI uses 3.13. `build.py` keeps PyInstaller's build/ and .spec in a temp folder.

## Architecture

1. Scan (`engine.collect_jobs`, run in a thread by the GUI): walks dropped files, folders and
   archives, unpacks archives to temp folders, returns one `Job` per SF2 preset, per `.sfz`, and
   per unused audio file to copy. Reports progress through `progress(text, done, total)`;
   raising `ScanCancelled` from it stops the scan (used when the window closes).
2. The GUI lists the jobs (New / Replace) and writes nothing until the user clicks Convert.
3. `Runner`: spawn-context process pool, one process per logical CPU, BELOW_NORMAL priority.
   `process_job` builds regions and calls `write_xrni`; each worker caches open SF2 files and
   encoded FLAC data.
4. `finalize`: on a complete run, deletes outputs of an earlier run that were not rewritten, then
   updates the hidden manifest.

Output: `<library>/<sf2 name | sfz folder or archive name>/<family>/<name>.xrni`, plus
`Samples/` for copied unused audio. Default library: `<Documents>\Renoise\User Library\Instruments`,
with Documents resolved through `SHGetKnownFolderPath` (follows OneDrive redirection); never hard-code
a user path.

### Replace safety

Files this tool wrote are recognised by the SF2 name pattern `Bxxx-Pyyy <preset>.xrni` and by a
hidden manifest `.soundfont-to-xrni` in each library folder (SFZ outputs keep their own names).
Anything else in those folders belongs to the user and is never touched.

### Classification

17 GM family folders (`01 Piano` ... `16 Sound Effects`, `17 Drum Kits`) plus `18 Other` (only for an
SFZ with no program number and an unmatched name). Name keywords (`NAME_RULES`) give a list of
compatible families; the GM family from the program number wins when it is in that list, so
GM-conformant banks keep their GM layout and only misnumbered presets move. Bank 128 = drums,
bank 64 = GS/XG sound-effects bank. SFZ: a leading number in the file name is the program; an SFZ
of mostly fixed-pitch single-key regions is a drum kit. The owner chose: name first, then GM, no
special folder for vocals.

## XRNI facts (verified against Renoise 3.5.4 bundled instruments unless noted)

- `.xrni` = zip: `Instrument.xml` (`RenoiseInstrument doc_version="31"`) + `SampleData/SampleNN (name).flac`.
- `LoopStart` is 0-based; `LoopEnd` is exclusive (equals the frame count for a full loop).
- Renoise note = MIDI note - 12 (C-4 = 48 = MIDI 60); notes clamp to 0..119.
- AHDSR times: `seconds = 60 * value^3` (Renoise forum, not measured here). Sustain is written as a
  linear 0..1 amplitude: assumption, not verified.
- `Finetune` range -127..127, written as 1.28 per cent: assumption, not verified in Renoise.
- `NewNoteAction` values seen: `NoteOff`, `Cut`, `None` (drums use `None`).
- SF2 `initialAttenuation` uses the EMU / FluidSynth 0.4 factor, so everything is about 8 dB
  quieter than full scale; never compared by ear against another player.
- FLAC cannot store rates above 65535 Hz unless they are multiples of 10: `_flac_rate` rounds
  (66896 -> 66900 Hz) and compensates with the fine tune.

## Archive unpacking (measured on a Ryzen 7 9800X3D)

- `.zip`: members unpacked in parallel threads (zlib releases the GIL): 1.6 s -> 0.4 s for 400 WAVs.
- `.7z`, one big block or solid: 7-Zip's `7z.exe` if installed (found via registry / Program Files):
  852 MB SoundFont 17 s -> 10 s, solid 400-file library 15 s -> 3 s. LZMA2 in a single block
  cannot be split across cores by any tool.
- `.7z`, one block per file: py7zr on one thread (1.5 s, 7-Zip needs 6.4 s). Threads give no gain
  because py7zr holds the GIL; worker processes were judged not worth their start-up cost.
- py7zr is driven through a custom `WriterFactory` so progress and cancellation happen in the
  extracting thread.

## Conventions

- Version: `soundfont_to_xrni/__init__.py`, the CHANGELOG heading and the git tag (`vX.Y.Z`) must match.
- Never commit SoundFonts, SFZ libraries or audio (copyright); `.gitignore` excludes them.
- Keep personal data out of the repo: no user names, e-mail addresses or absolute user paths.
  `LICENSE` holds the owner's name on purpose.
- README screenshots: capture only the app window with `PrintWindow` (an earlier full-screen
  capture leaked another window). Use synthetic demo material.
- Match the existing code style: type hints, short docstrings, comments only where the reason
  is not obvious.
