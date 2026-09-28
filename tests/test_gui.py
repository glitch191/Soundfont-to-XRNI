"""Window behaviour: dropping only scans; nothing is written until Convert is clicked.

Drives the real window (hidden) without a mouse; skipped when no display is available.
"""

import glob
import os
import shutil
import tempfile
import time
import unittest

from soundfont_to_xrni import engine

from test_conversion import write_sf2

try:
    from tkinterdnd2 import TkinterDnD
    from soundfont_to_xrni import gui

    _root = TkinterDnD.Tk()
    _root.destroy()
    HAVE_DISPLAY = True
except Exception:  # no display / Tk
    HAVE_DISPLAY = False


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class ScanThenConvert(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="s2x_gui_")
        self.lib = os.path.join(self.tmp, "library")
        self.src = os.path.join(self.tmp, "My Bank.sf2")
        write_sf2(self.src)
        self.root = TkinterDnD.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root, [])
        self.app.library = self.lib

    def tearDown(self) -> None:
        self.app._quit()
        engine.cleanup_temp()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pump(self, until, timeout: float = 60.0) -> None:
        end = time.monotonic() + timeout
        while not until():
            self.assertLess(time.monotonic(), end, "timed out")
            self.root.update()
            time.sleep(0.02)

    def outputs(self) -> list[str]:
        return glob.glob(os.path.join(self.lib, "**", "*.xrni"), recursive=True)

    def states(self) -> list[str]:
        return [self.app.tree.set(i, "state") for i in self.app.tree.get_children()]

    def test_drop_scans_and_waits_for_convert(self):
        self.app.add([self.src])
        self.pump(lambda: self.app.pending)
        self.assertEqual(len(self.app.pending), 2)
        self.assertEqual(self.states(), ["New", "New"])
        self.assertIn("Scanned: 2 instrument(s)", self.app.status.cget("text"))
        self.assertFalse(self.app.convert_btn.instate(["disabled"]))
        time.sleep(0.5)
        self.root.update()
        self.assertEqual(self.outputs(), [])  # nothing written before confirmation

        self.app.convert_btn.invoke()
        self.pump(lambda: self.app.done == 2 and not self.app.runner.running)
        self.pump(lambda: self.app.cancel_btn.instate(["disabled"]))
        self.assertEqual(len(self.outputs()), 2)
        self.assertEqual(self.states(), ["Converted", "Converted"])
        self.assertTrue(self.app.convert_btn.instate(["disabled"]))

        # A second scan of the same bank announces the replacement
        self.app.add([self.src])
        self.pump(lambda: self.app.pending)
        self.assertEqual(self.states(), ["Replace", "Replace"])
        self.assertIn("2 existing file(s) will be replaced", self.app.status.cget("text"))

    def test_scan_shows_progress(self):
        archive = shutil.make_archive(os.path.join(self.tmp, "Zipped Bank"), "zip", self.tmp, "My Bank.sf2")
        texts = []

        def done() -> bool:
            texts.append(self.app.status.cget("text"))
            return bool(self.app.pending)

        self.app.add([archive])
        self.assertTrue(self.app.status.cget("text").startswith("Scanning"))  # feedback right away
        self.pump(done)
        self.assertTrue(any(t.startswith("Scanning") for t in texts))
        self.assertIn("Scanned: 2 instrument(s)", self.app.status.cget("text"))
        self.assertIsNone(self.app.scan)

    def test_clear_forgets_the_scan(self):
        self.app.add([self.src])
        self.pump(lambda: self.app.pending)
        self.app.clear_btn.invoke()
        self.assertEqual(self.app.pending, {})
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertTrue(self.app.convert_btn.instate(["disabled"]))
        self.app._convert()  # nothing to do
        self.root.update()
        self.assertEqual(self.outputs(), [])


if __name__ == "__main__":
    unittest.main()
