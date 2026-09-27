"""SFZ reader: one .sfz file -> Renoise sample regions (same ``Region`` objects as the SF2 path).

Supported: <control> default_path, #define, #include, <global>/<master>/<group>/<region>
inheritance; sample, key/lokey/hikey (numbers or note names), lovel/hivel, pitch_keycenter,
pitch_keytrack, transpose, tune, volume, amplitude, pan, amp_veltrack, offset, end, loop_mode,
loop_start/loop_end (or loopstart/loopend, else the WAV "smpl" loop), ampeg_* envelope,
group/off_by (mute groups), trigger=release (Note-Off layer), seq_length / lorand (round robin).
Ignored: filters, LFOs, MIDI CC modulation and other opcodes with no Renoise sample equivalent.
"""

from __future__ import annotations

import os
import re
import struct

import numpy as np
import soundfile as sf

HEADERS = ("control", "global", "master", "group", "region", "curve", "effect", "midi", "sample")
NOTE_NAMES = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_TOKEN = re.compile(r"<(\w+)>|(\w+)=")


def note_number(value: str, default: int) -> int:
    """'60', 'c4', 'C#4', 'db3', 'c-1' -> MIDI number (SFZ: c4 = 60)."""
    v = value.strip().lower()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    m = re.fullmatch(r"([a-g])([#b♯♭]?)(-?\d+)", v)
    if not m:
        return default
    n = NOTE_NAMES[m.group(1)] + {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(m.group(2), 0)
    return (int(m.group(3)) + 1) * 12 + n


def _num(opcodes: dict, key: str, default: float) -> float:
    try:
        return float(opcodes[key])
    except (KeyError, ValueError):
        return default


def _read_text(path: str, defines: dict, depth: int = 0) -> str:
    """File text with comments removed, #define substituted and #include expanded."""
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"(^|\s)//.*", "", line)
        m = re.match(r'\s*#define\s+(\$\w+)\s+(.*\S)', line)
        if m:
            defines[m.group(1)] = m.group(2)
            continue
        m = re.match(r'\s*#include\s+"([^"]+)"', line)
        if m:
            inc = os.path.join(os.path.dirname(path), m.group(1).replace("\\", os.sep))
            if depth < 8 and os.path.isfile(inc):
                lines.append(_read_text(inc, defines, depth + 1))
            continue
        for name in sorted(defines, key=len, reverse=True):
            line = line.replace(name, defines[name])
        lines.append(line)
    return "\n".join(lines)


def parse(path: str) -> list[dict]:
    """Regions of an .sfz file, each a dict of opcodes (inheritance already applied)."""
    text = _read_text(path, {})
    tokens = list(_TOKEN.finditer(text))
    control: dict = {}
    levels = {"global": {}, "master": {}, "group": {}}
    regions: list[dict] = []
    current: dict | None = None
    for i, m in enumerate(tokens):
        end = tokens[i + 1].start() if i + 1 < len(tokens) else len(text)
        if m.group(1):  # <header>
            head = m.group(1).lower()
            if head == "control":
                current = control
            elif head == "global":
                levels = {"global": {}, "master": {}, "group": {}}
                current = levels["global"]
            elif head == "master":
                levels["master"], levels["group"] = {}, {}
                current = levels["master"]
            elif head == "group":
                levels["group"] = {}
                current = levels["group"]
            elif head == "region":
                current = {**levels["global"], **levels["master"], **levels["group"]}
                regions.append(current)
            else:
                current = None
            continue
        key = m.group(2).lower()
        # The value runs to the next opcode/header; only "sample" values may contain spaces.
        raw = text[m.end():end]
        if key == "sample":
            value = raw.strip()
        else:
            words = raw.split()
            value = words[0] if words else ""
        if current is not None:
            current[key] = value
    base = os.path.dirname(os.path.abspath(path))
    default_path = control.get("default_path", "").replace("\\", "/")
    for r in regions:
        if "sample" in r and not r["sample"].startswith("*"):
            rel = (default_path + r["sample"].replace("\\", "/")) if not os.path.isabs(r["sample"]) else r["sample"]
            r["_path"] = os.path.normpath(os.path.join(base, rel))
    return regions


def wav_loop(path: str) -> tuple[int, int] | None:
    """First loop of a WAV "smpl" chunk as (start, end inclusive), if any."""
    if not path.lower().endswith(".wav"):
        return None
    try:
        with open(path, "rb") as f:
            riff, _, wave = struct.unpack("<4sI4s", f.read(12))
            if riff != b"RIFF" or wave != b"WAVE":
                return None
            while True:
                h = f.read(8)
                if len(h) < 8:
                    return None
                cid, size = struct.unpack("<4sI", h)
                if cid == b"smpl" and size >= 60:
                    data = f.read(size)
                    if struct.unpack_from("<I", data, 28)[0] >= 1:
                        start, end = struct.unpack_from("<II", data, 36 + 8)
                        return start, end
                    return None
                f.seek(size + (size & 1), 1)
    except (OSError, struct.error):
        return None


_audio: dict[str, tuple[np.ndarray, int, int]] = {}


def load_audio(path: str) -> tuple[np.ndarray, int, int]:
    """(int32 frames[, 2], rate, bits 16|24) - cached per worker; float files become 24-bit."""
    if path in _audio:
        return _audio[path]
    info = sf.info(path)
    bits = 16 if info.subtype in ("PCM_16", "PCM_S8", "PCM_U8", "ULAW", "ALAW") else 24
    data = sf.read(path, dtype="int32", always_2d=True)[0]
    data = data[:, :2] if data.shape[1] >= 2 else data[:, 0]
    data = data >> (16 if bits == 16 else 8)
    if len(_audio) > 64:
        _audio.clear()
    _audio[path] = (data, info.samplerate, bits)
    return _audio[path]


def _sec(v: float) -> float:
    return min(1.0, max(0.0, v / 60.0)) ** (1.0 / 3.0)  # Renoise AHDSR: seconds = 60 * value^3


def build_regions(path: str, Region) -> tuple[list, str]:
    """(Renoise regions, keyzone overlap mode) for one .sfz file."""
    out = []
    groups_off = set()
    parsed = parse(path)
    for op in parsed:
        if "off_by" in op:
            groups_off.add(op["off_by"])
    mute_ids: dict[str, int] = {}
    cycle = any(_num(op, "seq_length", 1) > 1 for op in parsed)
    rand = any("lorand" in op or "hirand" in op for op in parsed)
    for op in parsed:
        sample = op.get("_path")
        if not sample or not os.path.isfile(sample):
            continue
        data, rate, bits = load_audio(sample)
        total = len(data)
        start = int(_num(op, "offset", 0))
        end = int(_num(op, "end", total - 1))
        if end < 0:
            continue  # end=-1 means "silent region"
        start, end = max(0, min(start, total - 1)), max(0, min(end, total - 1))
        if end < start:
            continue
        r = Region()
        r.name = os.path.splitext(os.path.basename(sample))[0]
        r.rate, r.bits = rate, bits
        r.data = data[start:end + 1]
        r.frames = len(r.data)
        r.key = (sample, start, end)

        file_loop = wav_loop(sample)
        mode = op.get("loop_mode", op.get("loopmode", "loop_continuous" if file_loop else "no_loop")).lower()
        ls = _num(op, "loop_start", _num(op, "loopstart", file_loop[0] if file_loop else 0))
        le = _num(op, "loop_end", _num(op, "loopend", file_loop[1] if file_loop else total - 1))
        r.loop_start, r.loop_end = int(ls) - start, int(le) + 1 - start  # SFZ loop_end is inclusive
        looped = mode in ("loop_continuous", "loop_sustain")
        r.loop_mode = "Forward" if looped and 0 <= r.loop_start < r.loop_end <= r.frames else "Off"
        r.loop_release = mode == "loop_sustain"
        if r.loop_mode == "Off":
            r.loop_start, r.loop_end = 0, r.frames
        r.one_shot = mode == "one_shot"

        if "key" in op:
            lo = hi = center = note_number(op["key"], 60)
        else:
            lo, hi = note_number(op.get("lokey", "0"), 0), note_number(op.get("hikey", "127"), 127)
            center = note_number(op.get("pitch_keycenter", "60"), 60)
        if "pitch_keycenter" in op:
            center = note_number(op["pitch_keycenter"], center)
        clamp = lambda n: max(0, min(119, n - 12))  # Renoise C-4 = 48 = MIDI 60
        r.note_start, r.note_end, r.base_note = clamp(max(0, lo)), clamp(min(127, hi)), clamp(center)
        r.vel_start = int(max(0, min(127, _num(op, "lovel", 1) if _num(op, "lovel", 1) > 1 else 0)))
        r.vel_end = int(max(0, min(127, _num(op, "hivel", 127))))
        r.key_to_pitch = _num(op, "pitch_keytrack", 100) != 0
        cents = _num(op, "transpose", 0) * 100 + _num(op, "tune", 0)
        r.transpose = max(-120, min(120, int(round(cents / 100.0))))
        r.finetune = int(max(-127, min(127, round((cents - r.transpose * 100) * 1.28))))
        gain = 10 ** (_num(op, "volume", 0) / 20.0) * _num(op, "amplitude", 100) / 100.0
        r.volume = min(4.0, max(0.0, gain))
        r.pan = min(1.0, max(0.0, 0.5 + _num(op, "pan", 0) / 200.0))
        r.vel_to_vol = _num(op, "amp_veltrack", 100) != 0
        r.layer = "Note-Off Layer" if op.get("trigger", "attack").lower() == "release" else "Note-On Layer"

        grp = op.get("group")
        if grp is not None and grp in groups_off:
            mute_ids.setdefault(grp, len(mute_ids) + 1)
            r.exclusive = mute_ids[grp]
        else:
            r.exclusive = 0

        sustain = min(100.0, max(0.0, _num(op, "ampeg_sustain", 100))) / 100.0
        r.env = (
            round(_sec(_num(op, "ampeg_attack", 0)), 4),
            round(_sec(_num(op, "ampeg_hold", 0) + _num(op, "ampeg_delay", 0)), 4),
            round(_sec(_num(op, "ampeg_decay", 0)), 4),
            round(sustain, 4),
            round(_sec(_num(op, "ampeg_release", 0.001)), 4),
        )
        out.append(r)
    overlap = "Cycle" if cycle else "Random" if rand else "Play All"
    return out, overlap


def looks_like_drum_kit(path: str) -> bool:
    """Many single-key regions without pitch tracking = a drum kit mapped across the keyboard."""
    regions = parse(path)
    if len(regions) < 6:
        return False
    fixed = sum(1 for op in regions if _num(op, "pitch_keytrack", 100) == 0
                and ("key" in op or op.get("lokey") == op.get("hikey")))
    return fixed >= 0.6 * len(regions)


def region_count(path: str) -> int:
    try:
        return max(1, len(parse(path)))
    except OSError:
        return 1
