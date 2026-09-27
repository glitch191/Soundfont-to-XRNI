# Changelog

## 1.0.0 — 2026-09-26

First public release.

- Converts SoundFont 2 (`.sf2`) presets and SFZ (`.sfz`) instruments to Renoise instruments (`.xrni`).
- Accepts files, folders and `.zip` / `.7z` archives (unpacked automatically); several at once.
- Sorts instruments into General MIDI family folders (name first, then program number).
- Copies audio files that no instrument uses as they are, into a `Samples` folder.
- Re-converting replaces earlier output and never touches files added by hand.
- Uses one worker process per logical CPU.
