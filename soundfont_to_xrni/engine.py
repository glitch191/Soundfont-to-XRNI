"""SoundFont 2 (.sf2) and SFZ (.sfz) -> Renoise instruments (.xrni).

SF2: one .xrni per preset, in ``<library>/<sf2 name>/<family>/Bxxx-Pyyy <preset>.xrni``.
SFZ: one .xrni per .sfz file, in ``<library>/<folder or .zip name>/<family>/<sfz name>.xrni``.
Samples are FLAC; SF2 left/right sample pairs are merged into stereo samples; volume
envelopes become a Renoise AHDSR (``seconds = 60 * value^3``).
"""

from __future__ import annotations

import ctypes
import io
import os
import re
import shutil
import struct
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from multiprocessing import get_context
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile as zipfile_writer

import numpy as np
import soundfile as sf

from . import sfz

# --------------------------------------------------------------------------- destination

def documents_dir() -> str:
    """The current user's Documents folder, as Windows reports it (follows OneDrive or other
    redirections); ~/Documents elsewhere or if the lookup fails."""
    try:
        import uuid
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("data", ctypes.c_ubyte * 16)]

        folder = GUID()
        folder.data[:] = list(uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}").bytes_le)  # FOLDERID_Documents
        path = wintypes.LPWSTR()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folder), 0, None, ctypes.byref(path)) == 0:
            try:
                return path.value
            finally:
                ctypes.windll.ole32.CoTaskMemFree(path)
    except (AttributeError, OSError, ValueError):
        pass
    return os.path.join(os.path.expanduser("~"), "Documents")


# Renoise's default user library: <Documents>\Renoise\User Library\Instruments
DEFAULT_LIBRARY = os.path.join(documents_dir(), "Renoise", "User Library", "Instruments")
OUR_FILE = re.compile(r"^B\d{3}-P\d{3} .*\.xrni$")  # SF2 files this tool writes (safe to replace)
MANIFEST = ".soundfont-to-xrni"  # per library folder: every .xrni this tool wrote there
TMP_SUFFIX = ".xrni.tmp"

# --------------------------------------------------------------------------- classification

GM_FAMILIES = [
    "01 Piano", "02 Chromatic Percussion", "03 Organ", "04 Guitar",
    "05 Bass", "06 Strings", "07 Ensemble", "08 Brass", "09 Reed",
    "10 Pipe", "11 Synth Lead", "12 Synth Pad", "13 Synth Effects",
    "14 Ethnic", "15 Percussive", "16 Sound Effects",
]
DRUM_KITS = "17 Drum Kits"
OTHER = "18 Other"  # SFZ with no program number and a name matching no family
(PIANO, CHROM, ORGAN, GUITAR, BASS, STRINGS, ENSEMBLE, BRASS, REED, PIPE,
 LEAD, PAD, SYNTH_FX, ETHNIC, PERCUSSIVE, SFX) = GM_FAMILIES

# Name keywords -> families they are compatible with (first = preferred). The GM family
# given by the program number wins whenever it is one of the compatible families, so
# GM-conformant banks keep their GM layout and only misplaced presets move.
NAME_RULES = [
    (r"drum ?(kit|set)|\bkit\b", [DRUM_KITS]),
    (r"(?<!contra)(?<!contra )\bbass\b(?! ?drum)|\bbs\b\.?|\b303\b|\btb-?303", [BASS]),
    (r"piano|rhodes|wurl|clav|harpsi|e\.? ?grand|\bep\b|e\.? ?p(no|iano)", [PIANO]),
    (r"organ|accordi|harmonica|blues harp|bandone|harmonium|hammond|farf", [ORGAN]),
    (r"guitar|guit|gtr|\bgt\b|\bgt\.|strat|mandolin|ukulele|\bpick\b|rear pick|front pick", [GUITAR]),
    (r"celest|glock|music ?box|vibra|marimba|xylo|tubular|\bbells?\b|dulcimer|santur", [CHROM, ETHNIC, PERCUSSIVE]),
    (r"violin|viola|cello|contra ?bass|fiddle|pizz|(?<!blues )\bharp\b|timpani|tremolo str",
     [STRINGS, ETHNIC]),
    (r"string|choir|voice|vox|\baah|\booh|orchestra|ensemble|\bscat\b|humming|\bhit\b",
     [ENSEMBLE, STRINGS, LEAD, PAD, SYNTH_FX, SFX]),
    (r"english horn", [REED]),
    (r"trumpet|trombone|tuba|horn|brass|flugel|cornet", [BRASS]),
    (r"sax|oboe|clarinet|bassoon|\breed\b", [REED]),
    (r"flute|piccolo|recorder|shakuhachi|whistle|ocarina|bottle", [PIPE]),
    (r"\blead\b|square|\bsaw|sine|\bsync|calliope|chiff", [LEAD, PAD, SYNTH_FX, BASS]),
    (r"\bpad\b|sweep|\bwarm\b|halo|polysynth|space|new ?age", [PAD, SYNTH_FX]),
    (r"\bfx\b|rain|soundtrack|crystal|atmosphere|brightness|goblin|echo|sci-?fi", [SYNTH_FX, SFX]),
    (r"sitar|banjo|shamisen|koto|kalimba|bagpipe|shanai|shehnai|erhu|zither|balafon|kanoon|zhong|"
     r"pipa|\boud\b|didgeridoo|tabla|santoor|gamelan", [ETHNIC]),
    (r"tinkle|agogo|steel ?drum|woodblock|wood block|taiko|\btoms?\b|synth ?drum|reverse cymbal|"
     r"percussion|\bperc|snare|kick|bass ?drum|hi-?hat|cymbal|crash|\bride\b|tambo?u?r|conga|bongo|"
     r"\bclap|cowbell|shaker|cabasa|triangle|claves", [PERCUSSIVE]),
    (r"noise|\bnz\b|door|breath|seashore|bird|telephone|phone|helicopter|applause|gun|shot|scratch|"
     r"creak|\brail|wind|thunder|\bcar\b|train|laugh|scream|punch|heart|footstep|\bdog\b|horse|"
     r"explosion|siren|slide|scrape|click", [SFX, SYNTH_FX]),
]
SFX_BANK = 64  # GS/XG "SFX" bank: its program numbers do not follow the GM families
NAME_RULES = [(re.compile(p, re.I), fams) for p, fams in NAME_RULES]


def classify(name: str, bank: int | None, program: int | None) -> str:
    """Name first, then GM family from the program number (``None`` = unknown, SFZ)."""
    if bank == 128:
        return DRUM_KITS
    if bank == SFX_BANK:
        gm = SFX
    elif program is not None:
        gm = GM_FAMILIES[min(program, 127) // 8]
    else:
        gm = None
    name = name.replace("_", " ")
    compatible: list[str] = []
    for rx, fams in NAME_RULES:
        if rx.search(name):
            compatible += [f for f in fams if f not in compatible]
    if gm and (not compatible or gm in compatible):
        return gm
    return compatible[0] if compatible else OTHER


# --------------------------------------------------------------------------- SF2 reader

START_OFS, END_OFS, STARTLOOP_OFS, ENDLOOP_OFS, START_COARSE = 0, 1, 2, 3, 4
END_COARSE, PAN = 12, 17
DELAY_VOL, ATTACK_VOL, HOLD_VOL, DECAY_VOL, SUSTAIN_VOL, RELEASE_VOL = 33, 34, 35, 36, 37, 38
INSTRUMENT, KEY_RANGE, VEL_RANGE, STARTLOOP_COARSE = 41, 43, 44, 45
ATTENUATION, ENDLOOP_COARSE, COARSE_TUNE, FINE_TUNE = 48, 50, 51, 52
SAMPLE_ID, SAMPLE_MODES, SCALE_TUNING, EXCLUSIVE_CLASS, ROOT_KEY = 53, 54, 56, 57, 58

DEFAULTS = {DELAY_VOL: -12000, ATTACK_VOL: -12000, HOLD_VOL: -12000,
            DECAY_VOL: -12000, RELEASE_VOL: -12000, SCALE_TUNING: 100, ROOT_KEY: -1}
# Generators a preset zone must not add to the instrument zone (SF2 spec 8.5)
NOT_ADDITIVE_AT_PRESET = {START_OFS, END_OFS, STARTLOOP_OFS, ENDLOOP_OFS, START_COARSE, END_COARSE,
                          STARTLOOP_COARSE, ENDLOOP_COARSE, SAMPLE_MODES, EXCLUSIVE_CLASS, ROOT_KEY,
                          46, 47, SAMPLE_ID, INSTRUMENT, KEY_RANGE, VEL_RANGE}
UNSIGNED_GENS = {SAMPLE_ID, INSTRUMENT}

ATTENUATION_FACTOR = 0.4  # EMU / FluidSynth convention for initialAttenuation


def _name(raw: bytes) -> str:
    return raw.split(b"\0")[0].decode("latin1").strip()


class SF2:
    def __init__(self, path: str) -> None:
        self.path = path
        smpl = sm24 = None
        chunks: dict[bytes, bytes] = {}
        with open(path, "rb") as f:
            riff, _, form = struct.unpack("<4sI4s", f.read(12))
            if riff != b"RIFF" or form != b"sfbk":
                raise ValueError("not a SoundFont 2 file")
            while True:
                h = f.read(12)
                if len(h) < 12:
                    break
                _, size, kind = struct.unpack("<4sI4s", h)
                list_end = f.tell() + size - 4
                while f.tell() + 8 <= list_end:
                    cid, sz = struct.unpack("<4sI", f.read(8))
                    if cid == b"smpl":
                        smpl = (f.tell(), sz // 2)
                        f.seek(sz, 1)
                    elif cid == b"sm24":
                        sm24 = f.tell()
                        f.seek(sz, 1)
                    elif kind == b"pdta":
                        chunks[cid] = f.read(sz)
                    else:
                        f.seek(sz, 1)
                    if sz % 2:
                        f.seek(1, 1)
                f.seek(list_end + (size % 2))
        missing = [c.decode() for c in (b"phdr", b"pbag", b"pgen", b"inst", b"ibag", b"igen", b"shdr")
                   if c not in chunks]
        if smpl is None or missing:
            raise ValueError("incomplete or damaged SF2 file")
        self.smpl_len = smpl[1]
        self.smpl = np.memmap(path, dtype="<i2", mode="r", offset=smpl[0], shape=(self.smpl_len,))
        self.sm24 = (np.memmap(path, dtype="u1", mode="r", offset=sm24, shape=(self.smpl_len,))
                     if sm24 is not None else None)

        def recs(cid: bytes, fmt: str) -> list[tuple]:
            size, data = struct.calcsize(fmt), chunks[cid]
            return [struct.unpack_from(fmt, data, i) for i in range(0, len(data) - size + 1, size)]

        self.phdr = recs(b"phdr", "<20sHHHIII")
        self.pbag = recs(b"pbag", "<HH")
        self.pgen = recs(b"pgen", "<HH")
        self.inst = recs(b"inst", "<20sH")
        self.ibag = recs(b"ibag", "<HH")
        self.igen = recs(b"igen", "<HH")
        self.shdr = recs(b"shdr", "<20sIIIIIBbHH")

    def presets(self) -> list[tuple[int, str, int, int]]:
        """(index, name, bank, program) for every preset (terminal EOP record excluded)."""
        return [(i, _name(p[0]) or f"Preset {i}", p[2], p[1]) for i, p in enumerate(self.phdr[:-1])]

    def _zones(self, bag: list, gen: list, first: int, last: int) -> list[dict]:
        zones = []
        for b in range(first, min(last, len(bag) - 1)):
            zone = {}
            for oper, amount in gen[bag[b][0]:bag[b + 1][0]]:
                if oper in (KEY_RANGE, VEL_RANGE):
                    zone[oper] = (amount & 0xFF, amount >> 8)
                elif oper in UNSIGNED_GENS:
                    zone[oper] = amount
                else:
                    zone[oper] = amount - 0x10000 if amount >= 0x8000 else amount
            zones.append(zone)
        return zones

    def zones(self, index: int) -> list[dict]:
        """Flatten a preset into zones (preset + instrument generators combined)."""
        pzones = self._zones(self.pbag, self.pgen, self.phdr[index][3], self.phdr[index + 1][3])
        pglobal = pzones.pop(0) if pzones and INSTRUMENT not in pzones[0] else {}
        out = []
        for pz in pzones:
            if INSTRUMENT not in pz or pz[INSTRUMENT] >= len(self.inst) - 1:
                continue
            p = {**pglobal, **pz}
            i = p[INSTRUMENT]
            izones = self._zones(self.ibag, self.igen, self.inst[i][1], self.inst[i + 1][1])
            iglobal = izones.pop(0) if izones and SAMPLE_ID not in izones[0] else {}
            for iz in izones:
                if SAMPLE_ID not in iz or iz[SAMPLE_ID] >= len(self.shdr) - 1:
                    continue
                if self.shdr[iz[SAMPLE_ID]][9] & 0x8000:  # ROM sample: no data in the file
                    continue
                z = {**DEFAULTS, **iglobal, **iz}
                klo, khi = _intersect(z.get(KEY_RANGE, (0, 127)), p.get(KEY_RANGE, (0, 127)))
                vlo, vhi = _intersect(z.get(VEL_RANGE, (0, 127)), p.get(VEL_RANGE, (0, 127)))
                if klo > khi or vlo > vhi:
                    continue
                for g, v in p.items():
                    if g not in NOT_ADDITIVE_AT_PRESET:
                        z[g] = z.get(g, 0) + v
                z[KEY_RANGE], z[VEL_RANGE] = (klo, khi), (vlo, vhi)
                out.append(z)
        return out

    def pcm(self, start: int, end: int) -> tuple[np.ndarray, int]:
        start, end = max(0, start), min(self.smpl_len, end)
        if end <= start:
            return np.zeros(0, dtype=np.int32), 16
        data = self.smpl[start:end].astype(np.int32)
        if self.sm24 is not None:
            return (data << 8) | self.sm24[start:end].astype(np.int32), 24
        return data, 16


def _intersect(a: tuple[int, int], b: tuple[int, int]) -> tuple[int, int]:
    return max(a[0], b[0]), min(a[1], b[1])


# --------------------------------------------------------------------------- SF2 zones -> Renoise samples

def _tc_to_sec(tc: int) -> float:
    return 0.0 if tc <= -12000 else 2.0 ** (tc / 1200.0)


def _ahdsr(sec: float) -> float:
    return min(1.0, max(0.0, sec / 60.0)) ** (1.0 / 3.0)


def _note(n: int) -> int:
    return max(0, min(119, n))


class Region:
    pass


def build_regions(sf2: SF2, zones: list[dict]) -> list[Region]:
    used: set[int] = set()
    regions = []
    for idx, z in enumerate(zones):
        if idx in used:
            continue
        s = sf2.shdr[z[SAMPLE_ID]]
        partner = None
        if s[9] & 0x7FFF in (2, 4):  # right / left: look for the twin zone
            for j, z2 in enumerate(zones):
                if (j != idx and j not in used and z2[SAMPLE_ID] == s[8]
                        and z2[KEY_RANGE] == z[KEY_RANGE] and z2[VEL_RANGE] == z[VEL_RANGE]):
                    partner = j
                    break
        used.add(idx)
        main, other = z, None
        if partner is not None:
            used.add(partner)
            main, other = (zones[partner], z) if s[9] & 0x7FFF == 2 else (z, zones[partner])
        r = _region(sf2, main, other)
        if r is not None:
            regions.append(r)
    return regions


def _bounds(sf2: SF2, z: dict) -> tuple[int, int, int, int]:
    s = sf2.shdr[z[SAMPLE_ID]]
    start = s[1] + z.get(START_OFS, 0) + 32768 * z.get(START_COARSE, 0)
    end = s[2] + z.get(END_OFS, 0) + 32768 * z.get(END_COARSE, 0)
    ls = s[3] + z.get(STARTLOOP_OFS, 0) + 32768 * z.get(STARTLOOP_COARSE, 0)
    le = s[4] + z.get(ENDLOOP_OFS, 0) + 32768 * z.get(ENDLOOP_COARSE, 0)
    return start, end, ls - start, le - start


def _region(sf2: SF2, z: dict, zr: dict | None) -> Region | None:
    s = sf2.shdr[z[SAMPLE_ID]]
    r = Region()
    r.name = _name(s[0]) or "Sample"
    r.rate = s[5] if 1000 <= s[5] <= 384000 else 44100
    start, end, r.loop_start, r.loop_end = _bounds(sf2, z)
    left, r.bits = sf2.pcm(start, end)
    if not len(left):
        return None
    r.key = (start, end, _bounds(sf2, zr)[:2] if zr else None)
    if zr is not None:
        rs, re_, _, _ = _bounds(sf2, zr)
        right, _ = sf2.pcm(rs, re_)
        n = max(len(left), len(right))
        r.data = np.stack([np.pad(left, (0, n - len(left))), np.pad(right, (0, n - len(right)))], axis=1)
        pan = (z.get(PAN, 0) + zr.get(PAN, 0)) / 2.0
        r.name = re.sub(r"[\s_-]*\(?[LR]\)?$", "", r.name) or r.name
    else:
        r.data = left
        pan = z.get(PAN, 0)
    r.frames = len(r.data)
    r.pan = min(1.0, max(0.0, 0.5 + pan / 1000.0))
    r.volume = min(4.0, 10.0 ** (-max(0, z.get(ATTENUATION, 0)) * ATTENUATION_FACTOR / 200.0))

    root = z[ROOT_KEY] if 0 <= z[ROOT_KEY] <= 127 else s[6]
    if root > 127:
        root = 60
    cents = z.get(COARSE_TUNE, 0) * 100 + z.get(FINE_TUNE, 0) + s[7]
    r.transpose = max(-120, min(120, int(round(cents / 100.0))))
    r.finetune = int(max(-127, min(127, round((cents - r.transpose * 100) * 1.28))))
    r.key_to_pitch = z[SCALE_TUNING] != 0
    r.base_note = _note(root - 12)  # Renoise C-4 = 48 = MIDI 60
    klo, khi = z[KEY_RANGE]
    r.note_start, r.note_end = _note(klo - 12), _note(khi - 12)
    r.vel_start, r.vel_end = min(127, z[VEL_RANGE][0]), min(127, z[VEL_RANGE][1])

    mode = z.get(SAMPLE_MODES, 0) & 3
    r.loop_mode = "Forward" if mode in (1, 3) and 0 <= r.loop_start < r.loop_end <= r.frames else "Off"
    r.loop_release = mode == 3
    if r.loop_mode == "Off":
        r.loop_start, r.loop_end = 0, r.frames
    r.exclusive = z.get(EXCLUSIVE_CLASS, 0)

    sustain_cb = min(1440, max(0, z.get(SUSTAIN_VOL, 0)))
    decay = _tc_to_sec(z[DECAY_VOL]) * min(sustain_cb, 1000) / 1000.0
    r.env = (
        round(_ahdsr(_tc_to_sec(z[ATTACK_VOL])), 4),
        round(_ahdsr(_tc_to_sec(z[HOLD_VOL]) + _tc_to_sec(z[DELAY_VOL])), 4),
        round(_ahdsr(decay), 4),
        round(10.0 ** (-sustain_cb / 200.0) if sustain_cb < 1440 else 0.0, 4),
        round(_ahdsr(_tc_to_sec(z[RELEASE_VOL])), 4),
    )
    return r


# --------------------------------------------------------------------------- XRNI writer

def _param(tag: str, value, indent: int) -> str:
    pad = " " * indent
    return (f"{pad}<{tag}>\n{pad}  <Value>{value}</Value>\n"
            f"{pad}  <Visualization>Device only</Visualization>\n{pad}</{tag}>\n")


def _modulation_set(env: tuple, index: int) -> str:
    a, h, d, s, r = env
    x = "      <ModulationSet>\n        <Devices>\n"
    x += '          <SampleMixerModulationDevice type="SampleMixerModulationDevice">\n'
    for tag, v in (("IsActive", "1.0"), ("Volume", "1.0"), ("Panning", "0.0"), ("Pitch", "0.0")):
        x += _param(tag, v, 12)
    x += "            <PitchModulationRange>12</PitchModulationRange>\n"
    for tag, v in (("Cutoff", "127"), ("Resonance", "64"), ("Drive", "0.0")):
        x += _param(tag, v, 12)
    x += "          </SampleMixerModulationDevice>\n"
    x += '          <SampleAhdsrModulationDevice type="SampleAhdsrModulationDevice">\n'
    x += "            <IsMaximized>true</IsMaximized>\n"
    x += _param("IsActive", "1.0", 12)
    x += ("            <Target>Volume</Target>\n            <Operator>*</Operator>\n"
          "            <Bipolar>false</Bipolar>\n            <TempoSynced>false</TempoSynced>\n")
    for tag, v in (("Attack", a), ("Hold", h), ("Decay", d), ("Sustain", s), ("Release", r),
                   ("AttackScaling", 0.0), ("DecayScaling", 0.0), ("ReleaseScaling", 0.0)):
        x += _param(tag, v, 12)
    x += "          </SampleAhdsrModulationDevice>\n        </Devices>\n"
    x += f"        <Name>Env {index + 1:02d}</Name>\n        <FilterType>0</FilterType>\n"
    x += "        <FilterBankVersion>3</FilterBankVersion>\n      </ModulationSet>\n"
    return x


def _sample(r: Region, mod_index: int, mute_group: int, is_drum: bool) -> str:
    b = lambda v: "true" if v else "false"
    return f"""      <Sample>
        <Name>{escape(r.name)}</Name>
        <Volume>{r.volume:.6f}</Volume>
        <Panning>{r.pan:.4f}</Panning>
        <Transpose>{r.transpose}</Transpose>
        <Finetune>{r.finetune}</Finetune>
        <BeatSyncIsActive>false</BeatSyncIsActive>
        <OneShotTrigger>{b(getattr(r, "one_shot", False))}</OneShotTrigger>
        <NewNoteAction>{"None" if is_drum else "NoteOff"}</NewNoteAction>
        <InterpolationMode>Cubic</InterpolationMode>
        <AutoSeek>false</AutoSeek>
        <AutoFade>true</AutoFade>
        <LoopMode>{r.loop_mode}</LoopMode>
        <LoopRelease>{b(r.loop_release)}</LoopRelease>
        <LoopStart>{r.loop_start}</LoopStart>
        <LoopEnd>{r.loop_end}</LoopEnd>
        <IsAlias>false</IsAlias>
        <MuteGroupIndex>{mute_group}</MuteGroupIndex>
        <ModulationSetIndex>{mod_index}</ModulationSetIndex>
        <DeviceChainIndex>-1</DeviceChainIndex>
        <Mapping>
          <Layer>{getattr(r, "layer", "Note-On Layer")}</Layer>
          <BaseNote>{r.base_note}</BaseNote>
          <NoteStart>{r.note_start}</NoteStart>
          <NoteEnd>{r.note_end}</NoteEnd>
          <MapKeyToPitch>{b(r.key_to_pitch)}</MapKeyToPitch>
          <VelocityStart>{r.vel_start}</VelocityStart>
          <VelocityEnd>{r.vel_end}</VelocityEnd>
          <MapVelocityToVolume>{b(getattr(r, "vel_to_vol", True))}</MapVelocityToVolume>
        </Mapping>
        <DisplayStart>0</DisplayStart>
        <DisplayLength>{r.frames}</DisplayLength>
      </Sample>
"""


_flac_cache: dict = {}
_flac_cache_bytes = 0
FLAC_CACHE_LIMIT = 256 * 2**20  # per worker process


def _flac(src: str, r: Region) -> bytes:
    global _flac_cache_bytes
    key = (src, r.key)
    if key in _flac_cache:
        return _flac_cache[key]
    buf = io.BytesIO()
    if r.bits == 24:
        sf.write(buf, (r.data << 8).astype(np.int32), r.rate, format="FLAC", subtype="PCM_24")
    else:
        sf.write(buf, r.data.astype(np.int16), r.rate, format="FLAC", subtype="PCM_16")
    raw = buf.getvalue()
    if _flac_cache_bytes + len(raw) > FLAC_CACHE_LIMIT:
        _flac_cache.clear()
        _flac_cache_bytes = 0
    _flac_cache[key] = raw
    _flac_cache_bytes += len(raw)
    return raw


def _flac_rate(r: Region) -> None:
    """FLAC stores rates above 65535 Hz only in steps of 10 Hz: round (e.g. 66896 -> 66900) and
    compensate the pitch with the fine tune (a fraction of a cent)."""
    if r.rate > 65535 and r.rate % 10:
        new = int(round(r.rate / 10.0)) * 10
        cents = 1200.0 * np.log2(r.rate / new)
        r.finetune = int(max(-127, min(127, r.finetune + round(cents * 1.28))))
        r.rate = min(new, 655350)


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:100] or "Untitled"


def write_xrni(path: str, src: str, title: str, regions: list[Region], is_drum: bool,
               comment: str, overlap: str = "Play All") -> int:
    envs: list[tuple] = []
    classes: list[int] = []
    parts = []
    for r in regions:
        _flac_rate(r)
        if r.env not in envs:
            envs.append(r.env)
        mute = -1
        if r.exclusive:
            if r.exclusive not in classes and len(classes) < 15:
                classes.append(r.exclusive)
            mute = classes.index(r.exclusive) if r.exclusive in classes else -1
        parts.append(_sample(r, envs.index(r.env), mute, is_drum))
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<RenoiseInstrument doc_version="31">\n'
           f"  <Name>{escape(title)}</Name>\n  <GlobalProperties>\n"
           "    <Volume>1.0</Volume>\n    <Transpose>0</Transpose>\n"
           f"    <Comments>\n      <Comment>{escape(comment)}</Comment>\n    </Comments>\n"
           "    <ShowCommentsAfterLoading>false</ShowCommentsAfterLoading>\n"
           "  </GlobalProperties>\n  <SampleGenerator>\n    <Samples>\n"
           + "".join(parts) +
           "    </Samples>\n    <ModulationSets>\n"
           + "".join(_modulation_set(e, k) for k, e in enumerate(envs)) +
           f"    </ModulationSets>\n    <KeyzoneOverlappingMode>{overlap}</KeyzoneOverlappingMode>\n"
           "  </SampleGenerator>\n  <ActiveGeneratorTab>Samples</ActiveGeneratorTab>\n"
           "</RenoiseInstrument>\n")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path[:-5] + TMP_SUFFIX
    with zipfile_writer(tmp, "w") as z:
        z.writestr("Instrument.xml", xml, compress_type=ZIP_DEFLATED)
        for i, r in enumerate(regions):
            z.writestr(f"SampleData/Sample{i:02d} ({safe_name(r.name)}).flac", _flac(src, r),
                       compress_type=ZIP_STORED)
    os.replace(tmp, path)
    return os.path.getsize(path)


# --------------------------------------------------------------------------- jobs

@dataclass
class Job:
    kind: str       # "sf2", "sfz" or "copy" (unused audio file, copied as it is)
    src: str        # source .sf2 / .sfz
    index: int      # preset index in the .sf2 (0 for .sfz)
    name: str
    bank: int | None
    program: int | None
    folder: str     # family folder
    dst: str        # output .xrni (or copied audio file)
    root: str       # library sub-folder (one per .sf2 / per SFZ folder or archive)
    weight: int     # number of zones (for time estimates)


ARCHIVES = (".zip", ".7z")
_temp_dirs: list[str] = []


def output_root(name: str, library: str) -> str:
    return os.path.join(library, safe_name(name))


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path.rstrip("\\/")))[0]


def extract_archive(path: str) -> str:
    """Unpack a .zip / .7z into a temporary folder (removed by ``cleanup_temp``)."""
    out = tempfile.mkdtemp(prefix="soundfont-to-xrni_")
    _temp_dirs.append(out)
    if path.lower().endswith(".7z"):
        import py7zr
        with py7zr.SevenZipFile(path, "r") as z:
            z.extractall(out)
    else:
        with zipfile.ZipFile(path) as z:
            z.extractall(out)  # member names are sanitised (no "..", no absolute paths)
    return out


def cleanup_temp() -> None:
    while _temp_dirs:
        shutil.rmtree(_temp_dirs.pop(), ignore_errors=True)


AUDIO = (".wav", ".aif", ".aiff", ".flac", ".ogg", ".mp3")  # formats Renoise loads as samples
SAMPLES_FOLDER = "Samples"  # audio files no instrument uses are copied here as they are


def _scan(path: str, lib: str | None, base: str | None, depth: int, found: list, errors: list) -> int:
    """Collect (kind, file, library name, scan base) under ``path``; returns ignored-file count.
    ``base`` is the dropped folder / unpacked archive, used to keep the relative layout of
    copied audio files."""
    ignored = 0
    low = path.lower()
    if os.path.isdir(path):
        lib, base = lib or _stem(path), base or path
        for folder, _, names in os.walk(path):
            for n in sorted(names):
                if n.lower().endswith((".sf2", ".sfz") + ARCHIVES + AUDIO):
                    ignored += _scan(os.path.join(folder, n), lib, base, depth, found, errors)
    elif low.endswith(ARCHIVES) and os.path.isfile(path):
        if depth >= 3:
            return 0
        try:
            out = extract_archive(path)
            ignored += _scan(out, _stem(path), out, depth + 1, found, errors)
        except Exception as e:  # corrupt / encrypted / unsupported archive
            errors.append((path, f"unreadable archive ({e})"))
    elif low.endswith(".sf2") and os.path.isfile(path):
        found.append(("sf2", path, None, None))
    elif low.endswith(".sfz") and os.path.isfile(path):
        found.append(("sfz", path, lib or _stem(os.path.dirname(path)), None))
    elif low.endswith(AUDIO) and os.path.isfile(path):
        found.append(("copy", path, lib or _stem(os.path.dirname(path)), base or os.path.dirname(path)))
    else:
        ignored += 1
    return ignored


def _unique(dst: str, seen: set[str]) -> str:
    stem, ext = os.path.splitext(dst)
    n, out = 2, dst
    while out.lower() in seen:
        out, n = f"{stem} ({n}){ext}", n + 1
    seen.add(out.lower())
    return out


def _sample_copy_path(path: str, base: str, root: str) -> str:
    """<root>/Samples/<path relative to the dropped folder or archive>, without a redundant
    leading "samples" folder (samples/piano.wav -> Samples/piano.wav)."""
    parts = os.path.relpath(path, base).split(os.sep)
    if len(parts) > 1 and parts[0].lower() in ("samples", "sample", "wav", "audio"):
        parts = parts[1:]
    return os.path.join(root, SAMPLES_FOLDER, *(safe_name(p) for p in parts))


def collect_jobs(paths: list[str], library: str) -> tuple[list[Job], list[tuple[str, str]], int]:
    """Jobs for every .sf2 preset / .sfz file in ``paths`` (folders and .zip / .7z archives
    are searched too), plus a copy job for each audio file no .sfz uses.
    Returns (jobs, [(file, error)], ignored_count)."""
    found: list = []
    errors: list = []
    ignored = sum(_scan(os.path.abspath(p), None, None, 0, found, errors) for p in paths)
    used: set[str] = set()  # audio files the .sfz instruments embed
    for kind, path, _, _ in found:
        if kind == "sfz":
            try:
                used |= {os.path.normcase(os.path.abspath(r["_path"])) for r in sfz.parse(path) if "_path" in r}
            except (OSError, UnicodeError):
                pass
    jobs: list[Job] = []
    seen: set[str] = set()
    done: set[str] = set()
    for kind, path, lib, base in found:
        if path.lower() in done:
            continue
        done.add(path.lower())
        if kind == "copy":
            if os.path.normcase(os.path.abspath(path)) not in used:
                root = output_root(lib, library)
                dst = _unique(_sample_copy_path(path, base, root), seen)
                jobs.append(Job("copy", path, 0, os.path.basename(path), None, None, SAMPLES_FOLDER, dst, root, 1))
            continue
        try:
            if kind == "sf2":
                sf2 = SF2(path)
                root = output_root(_stem(path), library)
                for index, name, bank, program in sf2.presets():
                    folder = classify(name, bank, program)
                    dst = _unique(os.path.join(root, folder, safe_name(f"B{bank:03d}-P{program:03d} {name}")
                                               + ".xrni"), seen)
                    weight = max(1, sf2.phdr[index + 1][3] - sf2.phdr[index][3])
                    jobs.append(Job("sf2", path, index, name, bank, program, folder, dst, root, weight))
                del sf2
            else:
                name = _stem(path)
                m = re.match(r"^(\d{1,3})(?=[\s_.\-])", name)
                program = int(m.group(1)) if m and int(m.group(1)) <= 127 else None
                folder = DRUM_KITS if sfz.looks_like_drum_kit(path) else classify(name, None, program)
                root = output_root(lib, library)
                dst = _unique(os.path.join(root, folder, safe_name(name) + ".xrni"), seen)
                jobs.append(Job("sfz", path, 0, name, None, program, folder, dst, root,
                                sfz.region_count(path)))
        except (OSError, ValueError, struct.error, UnicodeError) as e:
            errors.append((path, str(e)))
    return jobs, errors, ignored


# Files this tool wrote in a library folder: SF2 outputs are recognised by their name, all
# outputs are also listed in a hidden manifest (SFZ outputs keep their original names).

def _read_manifest(root: str) -> set[str]:
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as f:
            return {os.path.normpath(os.path.join(root, line.strip())) for line in f if line.strip()}
    except OSError:
        return set()


def _write_manifest(root: str, files: set[str]) -> None:
    path = os.path.join(root, MANIFEST)
    files = sorted(os.path.relpath(f, root) for f in files if os.path.isfile(f))
    try:
        if not files:
            if os.path.exists(path):
                os.remove(path)
            return
        if os.path.exists(path):
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x80)  # normal, so it can be rewritten
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(files) + "\n")
        ctypes.windll.kernel32.SetFileAttributesW(path, 0x2)  # hidden
    except (OSError, AttributeError):
        pass


def existing_outputs(root: str) -> set[str]:
    """.xrni files a previous run of this tool wrote under ``root``."""
    found = {p for p in _read_manifest(root) if os.path.isfile(p)}
    if os.path.isdir(root):
        for base, _, names in os.walk(root):
            found |= {os.path.join(base, n) for n in names if OUR_FILE.match(n)}
    return found


def remove_stale(root: str, keep: set[str], before: set[str]) -> int:
    """Delete .xrni written by an earlier run that this run did not rewrite, then empty folders.
    Files the user added are never touched."""
    removed = 0
    keep_l = {k.lower() for k in keep}
    for path in before:
        if path.lower() not in keep_l:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass
    for base, dirs, files in os.walk(root, topdown=False):
        if base != root and not dirs and not files:
            try:
                os.rmdir(base)
            except OSError:
                pass
    return removed


def kept_outputs(results: list[dict]) -> set[str]:
    """Outputs to keep after a complete run: everything except presets that turned out empty
    (a preset that failed keeps its previous .xrni, if any)."""
    return {r["dst"] for r in results if r["status"] != "empty"}


def finalize(root: str, before: set[str], results: list[dict], complete: bool) -> int:
    """After a run: on completion, replace (remove stale outputs); always update the manifest."""
    removed = remove_stale(root, kept_outputs(results), before) if complete else 0
    written = {r["dst"] for r in results if r["status"] == "ok"}
    _write_manifest(root, written | (before if not complete else before & kept_outputs(results)))
    return removed


# --------------------------------------------------------------------------- worker side

_open: dict[str, SF2] = {}


def _init_worker() -> None:
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(),
                                                0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
    except (AttributeError, OSError):
        pass


def process_job(job: Job) -> dict:
    res = {"dst": job.dst, "src": job.src, "samples": 0, "size": 0, "status": "ok", "message": "Converted"}
    try:
        if job.kind == "copy":
            os.makedirs(os.path.dirname(job.dst), exist_ok=True)
            tmp = tmp_path(job.dst)
            shutil.copy2(job.src, tmp)
            os.replace(tmp, job.dst)
            res.update(size=os.path.getsize(job.dst), message="Copied as is (unused)")
            return res
        overlap = "Play All"
        if job.kind == "sf2":
            sf2 = _open.get(job.src)
            if sf2 is None:
                sf2 = _open[job.src] = SF2(job.src)
            regions = build_regions(sf2, sf2.zones(job.index))
            comment = f"Converted from {os.path.basename(job.src)} - bank {job.bank}, program {job.program}"
        else:
            regions, overlap = sfz.build_regions(job.src, Region)
            comment = f"Converted from {os.path.basename(job.src)}"
        if not regions:
            res.update(status="empty", message="No samples (skipped)")
            return res
        res["size"] = write_xrni(job.dst, job.src, job.name, regions, job.folder == DRUM_KITS, comment, overlap)
        res["samples"] = len(regions)
    except Exception as e:  # one broken preset must not stop the batch
        res.update(status="error", message=f"Error: {e}")
        try:
            os.remove(tmp_path(job.dst))
        except OSError:
            pass
    return res


def tmp_path(dst: str) -> str:
    """Temporary name an output is written under before it is moved into place."""
    return dst[:-5] + TMP_SUFFIX if dst.lower().endswith(".xrni") else dst + ".tmp"


WORKERS_OVERRIDE = int(os.environ.get("S2X_WORKERS", "0"))  # benchmarks only


def plan_workers(jobs: list[Job]) -> int:
    """One process per logical CPU (FLAC encoding is CPU-bound; SF2 reads are memory-mapped)."""
    return max(1, min(WORKERS_OVERRIDE or os.cpu_count() or 1, len(jobs)))


class Runner:
    """Process pool; results arrive in ``events`` as ("result", dict), then ("done", None)
    or ("cancelled", None). Jobs can be added while running."""

    def __init__(self, events) -> None:
        self.events = events
        self._pool = None
        self._pending = 0
        self._lock = threading.Lock()
        self._dsts: set[str] = set()
        self.processes = 0

    @property
    def running(self) -> bool:
        return self._pool is not None

    def submit(self, jobs: list[Job]) -> None:
        if not jobs:
            return
        with self._lock:
            if self._pool is None:
                self.processes = plan_workers(jobs)
                self._pool = get_context("spawn").Pool(self.processes, _init_worker)
            self._pending += len(jobs)
            pool = self._pool
        # Big presets first so the last minutes are not spent on one huge drum kit.
        for job in sorted(jobs, key=lambda j: -j.weight):
            self._dsts.add(job.dst)
            pool.apply_async(process_job, (job,), callback=self._on_result,
                             error_callback=lambda exc, j=job: self._on_result(
                                 {"dst": j.dst, "src": j.src, "samples": 0, "size": 0, "status": "error",
                                  "message": f"Worker process stopped: {exc}"}))

    def _on_result(self, res: dict) -> None:
        self.events.put(("result", res))
        with self._lock:
            self._pending -= 1
            finished = self._pending == 0 and self._pool is not None
            pool = self._pool if finished else None
            if finished:
                self._pool = None
        if pool is not None:
            threading.Thread(target=self._close, args=(pool,), daemon=True).start()

    def _close(self, pool) -> None:
        pool.close()
        pool.join()
        self.events.put(("done", None))

    def cancel(self) -> None:
        """Stop now: kill the workers and remove their half-written temporary files."""
        with self._lock:
            pool, self._pool, self._pending = self._pool, None, 0
        if pool is None:
            return
        pool.terminate()
        pool.join()
        for dst in self._dsts:
            try:
                os.remove(tmp_path(dst))
            except OSError:
                pass
        self.events.put(("cancelled", None))


def run_blocking(paths: list[str], library: str, on_result=None) -> list[dict]:
    """Headless conversion (CLI / tests), with the same replace + cleanup as the window."""
    import queue
    try:
        jobs, errors, _ = collect_jobs(paths, library)
        for path, err in errors:
            print(f"[error] {path}: {err}")
        roots = {j.root for j in jobs}
        before = {r: existing_outputs(r) for r in roots}
        events: queue.Queue = queue.Queue()
        Runner(events).submit(jobs)
        results = []
        while len(results) < len(jobs):
            kind, payload = events.get()
            if kind == "result":
                results.append(payload)
                if on_result:
                    on_result(payload)
        root_of = {j.dst: j.root for j in jobs}
        for root in roots:
            finalize(root, before[root], [r for r in results if root_of[r["dst"]] == root], True)
        return results
    finally:
        cleanup_temp()
