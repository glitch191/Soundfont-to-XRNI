"""End-to-end tests on small SF2 / SFZ files generated on the fly (no copyrighted material).

    py -m unittest discover -s tests -v
"""

import io
import os
import shutil
import struct
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf

from soundfont_to_xrni import engine, sfz

RATE = 44100


def tone(frames: int, freq: float = 440.0) -> np.ndarray:
    t = np.arange(frames) / RATE
    return (np.sin(2 * np.pi * freq * t) * 12000).astype(np.int16)


# --------------------------------------------------------------------------- minimal SF2 writer

def _chunk(cid: bytes, data: bytes) -> bytes:
    return cid + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")


def _list(kind: bytes, *chunks: bytes) -> bytes:
    return _chunk(b"LIST", kind + b"".join(chunks))


def write_sf2(path: str) -> None:
    """One instrument: a mono looped zone (keys 0-59) and a stereo L/R pair (keys 60-127),
    used by two presets: "Piano Test" (bank 0) and "Test Kit" (bank 128)."""
    mono, left, right = tone(1000), tone(800, 330), tone(800, 331)
    pad = np.zeros(46, dtype=np.int16)
    data, starts = [], []
    for s in (mono, left, right):
        starts.append(sum(len(d) for d in data))
        data += [s, pad]
    smpl = np.concatenate(data).astype("<i2").tobytes()

    def shdr(name, start, n, loop, link, kind):
        return struct.pack("<20sIIIIIBbHH", name.encode(), start, start + n, start + loop[0],
                           start + loop[1], RATE, 60, 0, link, kind)

    shdrs = (shdr("mono", starts[0], 1000, (100, 900), 0, 1)
             + shdr("pair L", starts[1], 800, (0, 0), 2, 4)
             + shdr("pair R", starts[2], 800, (0, 0), 1, 2)
             + struct.pack("<20sIIIIIBbHH", b"EOS", 0, 0, 0, 0, 0, 0, 0, 0, 0))

    def gen(oper, amount):
        return struct.pack("<HH", oper, amount & 0xFFFF)

    kr = lambda lo, hi: lo | (hi << 8)
    igen = (gen(43, kr(0, 59)) + gen(54, 1) + gen(53, 0)
            + gen(43, kr(60, 127)) + gen(17, -500) + gen(53, 1)
            + gen(43, kr(60, 127)) + gen(17, 500) + gen(53, 2) + gen(0, 0))
    ibag = b"".join(struct.pack("<HH", g, 0) for g in (0, 3, 6, 9))
    inst = struct.pack("<20sH", b"Inst", 0) + struct.pack("<20sH", b"EOI", 3)
    pgen = gen(41, 0) + gen(41, 0) + gen(0, 0)
    pbag = b"".join(struct.pack("<HH", g, 0) for g in (0, 1, 2))
    phdr = (struct.pack("<20sHHHIII", b"Piano Test", 0, 0, 0, 0, 0, 0)
            + struct.pack("<20sHHHIII", b"Test Kit", 0, 128, 1, 0, 0, 0)
            + struct.pack("<20sHHHIII", b"EOP", 0, 0, 2, 0, 0, 0))
    mod = b"\0" * 10
    body = (b"sfbk"
            + _list(b"INFO", _chunk(b"ifil", struct.pack("<HH", 2, 1)))
            + _list(b"sdta", _chunk(b"smpl", smpl))
            + _list(b"pdta", _chunk(b"phdr", phdr), _chunk(b"pbag", pbag), _chunk(b"pmod", mod),
                    _chunk(b"pgen", pgen), _chunk(b"inst", inst), _chunk(b"ibag", ibag),
                    _chunk(b"imod", mod), _chunk(b"igen", igen), _chunk(b"shdr", shdrs)))
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


def read_xrni(path: str) -> tuple[ET.Element, zipfile.ZipFile]:
    z = zipfile.ZipFile(path)
    return ET.fromstring(z.read("Instrument.xml")), z


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="s2x_test_")
        self.lib = os.path.join(self.tmp, "library")

    def tearDown(self) -> None:
        engine.cleanup_temp()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def xrni(self, *parts: str) -> str:
        return os.path.join(self.lib, *parts)


# --------------------------------------------------------------------------- tests

class Classification(unittest.TestCase):
    def test_gm_program_wins_when_compatible(self):
        self.assertEqual(engine.classify("Piano 1", 0, 0), "01 Piano")
        self.assertEqual(engine.classify("Synth Bass 1", 0, 38), "05 Bass")

    def test_name_fixes_misnumbered_presets(self):
        self.assertEqual(engine.classify("Choir 1", 126, 29), "07 Ensemble")
        self.assertEqual(engine.classify("Steel Drums CM", 126, 69), "15 Percussive")
        self.assertEqual(engine.classify("Bass Drum Menu 1", 40, 116), "15 Percussive")
        self.assertEqual(engine.classify("XG Door Creak", 64, 65), "16 Sound Effects")

    def test_drum_bank_and_sfz_without_program(self):
        self.assertEqual(engine.classify("Anything", 128, 5), "17 Drum Kits")
        self.assertEqual(engine.classify("033_fbass", None, 33), "05 Bass")
        self.assertEqual(engine.classify("mystery", None, None), "18 Other")

    def test_note_names(self):
        for name, number in (("c4", 60), ("C#4", 61), ("db3", 49), ("c-1", 0), ("72", 72)):
            self.assertEqual(sfz.note_number(name, -1), number, name)


class SF2Conversion(Base):
    def test_presets_become_instruments(self):
        src = os.path.join(self.tmp, "My Bank.sf2")
        write_sf2(src)
        results = engine.run_blocking([src], self.lib)
        self.assertEqual([r["status"] for r in results], ["ok", "ok"])

        root, z = read_xrni(self.xrni("My Bank", "01 Piano", "B000-P000 Piano Test.xrni"))
        samples = root.findall(".//Samples/Sample")
        self.assertEqual(len(samples), 2)  # mono zone + stereo pair merged into one sample
        channels = sorted(sf.info(io.BytesIO(z.read(n))).channels for n in z.namelist()[1:])
        self.assertEqual(channels, [1, 2])
        mono = next(s for s in samples if s.findtext("Name") == "mono")
        self.assertEqual(mono.findtext("LoopMode"), "Forward")
        self.assertEqual((mono.findtext("LoopStart"), mono.findtext("LoopEnd")), ("100", "900"))
        self.assertEqual(mono.findtext("Mapping/BaseNote"), "48")  # MIDI 60 = Renoise C-4
        self.assertEqual(mono.findtext("Mapping/NoteEnd"), "47")
        stereo = next(s for s in samples if s is not mono)
        self.assertEqual(stereo.findtext("Panning"), "0.5000")

        self.assertTrue(os.path.isfile(self.xrni("My Bank", "17 Drum Kits", "B128-P000 Test Kit.xrni")))

    def test_reconversion_replaces_and_keeps_user_files(self):
        src = os.path.join(self.tmp, "My Bank.sf2")
        write_sf2(src)
        engine.run_blocking([src], self.lib)
        stale = self.xrni("My Bank", "03 Organ", "B000-P016 Old.xrni")
        mine = self.xrni("My Bank", "01 Piano", "My own instrument.xrni")
        os.makedirs(os.path.dirname(stale))
        for path in (stale, mine):
            open(path, "wb").close()
        engine.run_blocking([src], self.lib)
        self.assertFalse(os.path.exists(stale))
        self.assertFalse(os.path.exists(os.path.dirname(stale)))  # emptied folder removed
        self.assertTrue(os.path.exists(mine))


class SFZConversion(Base):
    def make_library(self) -> str:
        """An SFZ library folder: two instruments, a shared sample folder and one unused WAV."""
        src = os.path.join(self.tmp, "Retro Set")
        os.makedirs(os.path.join(src, "samples"))
        for name, freq in (("lead.wav", 440), ("kick.wav", 60), ("hat.wav", 5000), ("unused.wav", 220)):
            sf.write(os.path.join(src, "samples", name), tone(2000, freq), RATE, subtype="PCM_16")
        with open(os.path.join(src, "081_lead.sfz"), "w") as f:
            f.write("// comment\n<group> ampeg_release=0.5 loop_mode=loop_continuous\n"
                    "<region> sample=samples\\lead.wav lokey=c3 hikey=b5 pitch_keycenter=a4\n"
                    "loop_start=100 loop_end=1899 transpose=-12 tune=50 volume=-6 pan=-100\n"
                    "<region> sample=samples\\lead.wav key=c6 trigger=release\n")
        with open(os.path.join(src, "drums.sfz"), "w") as f:
            f.write("<group> pitch_keytrack=0 loop_mode=one_shot\n"
                    + "".join(f"<region> sample=samples/kick.wav key={36 + i}\n" for i in range(4))
                    + "<region> sample=samples/hat.wav key=42 group=1 off_by=2\n"
                    + "<region> sample=samples/hat.wav key=46 group=2 off_by=1\n")
        return src

    def test_sfz_folder(self):
        src = self.make_library()
        results = engine.run_blocking([src], self.lib)
        self.assertTrue(all(r["status"] == "ok" for r in results), results)

        root, _ = read_xrni(self.xrni("Retro Set", "11 Synth Lead", "081_lead.xrni"))
        s = root.findall(".//Samples/Sample")
        self.assertEqual(len(s), 2)
        m = s[0].find("Mapping")
        self.assertEqual((m.findtext("NoteStart"), m.findtext("NoteEnd"), m.findtext("BaseNote")), ("36", "71", "57"))
        self.assertEqual((s[0].findtext("LoopStart"), s[0].findtext("LoopEnd")), ("100", "1900"))
        self.assertEqual((s[0].findtext("Transpose"), s[0].findtext("Finetune")), ("-12", "64"))
        self.assertEqual(s[0].findtext("Panning"), "0.0000")
        self.assertAlmostEqual(float(s[0].findtext("Volume")), 10 ** (-6 / 20), places=4)
        self.assertEqual(s[1].findtext("Mapping/Layer"), "Note-Off Layer")

        root, _ = read_xrni(self.xrni("Retro Set", "17 Drum Kits", "drums.xrni"))
        s = root.findall(".//Samples/Sample")
        self.assertEqual(s[0].findtext("OneShotTrigger"), "true")
        self.assertEqual(s[0].findtext("Mapping/MapKeyToPitch"), "false")
        self.assertEqual({x.findtext("MuteGroupIndex") for x in s[4:]}, {"0", "1"})  # hats choke each other

        copies = [r for r in results if r["dst"].endswith(".wav")]
        self.assertEqual([os.path.relpath(r["dst"], self.lib) for r in copies],
                         [os.path.join("Retro Set", "Samples", "unused.wav")])

    def test_zip_and_7z_archives(self):
        import py7zr
        src = self.make_library()
        zpath = shutil.make_archive(os.path.join(self.tmp, "Zipped Set"), "zip", src)
        spath = os.path.join(self.tmp, "Seven Set.7z")
        with py7zr.SevenZipFile(spath, "w") as z:
            z.writeall(src, "Seven Set")
        results = engine.run_blocking([zpath, spath], self.lib)
        self.assertTrue(all(r["status"] == "ok" for r in results), results)
        for name in ("Zipped Set", "Seven Set"):
            self.assertTrue(os.path.isfile(self.xrni(name, "11 Synth Lead", "081_lead.xrni")), name)
        self.assertEqual(engine._temp_dirs, [])  # unpacked archives are cleaned up


if __name__ == "__main__":
    unittest.main()
