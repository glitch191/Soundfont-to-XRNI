# Changelog

## 1.1.0 — 2026-09-28

- **Scan first, then confirm**: dropping files (or opening them with the `.exe`) now only scans
  them. The list shows every instrument, its destination folder and whether it is **New** or will
  **Replace** an existing file, with a summary; nothing is written until you click **Convert**
  (or press Enter).
- **Scan progress**: while a dropped file is scanned, the window shows what is happening —
  unpacking progress for `.zip` / `.7z` archives (percentage and MB), then the reading of presets —
  with an elapsed-time counter. Large archives no longer look stuck.
- **Faster unpacking**: `.zip` members are unpacked in parallel on every CPU thread (4x faster on a
  400-file library), and `.7z` archives use 7-Zip's own decoder when 7-Zip is installed (852 MB
  SoundFont: 10 s instead of 17 s; solid 400-file library: 3 s instead of 15 s). Without 7-Zip,
  the built-in decoder is used as before.
- New **Clear** button to discard a scan.
- Files dropped while a conversion runs are scanned and wait for their own confirmation.
- Changing the destination re-scans the pending files.
- Closing the window during a scan stops it at once and removes the unpacked files; unpacked
  archives left behind by a session that was killed are cleaned up at the next start.
- The command line (`--cli`) still converts right away.

## 1.0.0 — 2026-09-26

First public release.

- Converts SoundFont 2 (`.sf2`) presets and SFZ (`.sfz`) instruments to Renoise instruments (`.xrni`).
- Accepts files, folders and `.zip` / `.7z` archives (unpacked automatically); several at once.
- Sorts instruments into General MIDI family folders (name first, then program number).
- Copies audio files that no instrument uses as they are, into a `Samples` folder.
- Re-converting replaces earlier output and never touches files added by hand.
- Uses one worker process per logical CPU.
