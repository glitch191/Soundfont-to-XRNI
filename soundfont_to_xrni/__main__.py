"""Entry point: GUI by default (paths given on the command line - e.g. files dropped on the .exe
icon - are converted right away); ``--cli PATH... [--library DIR]`` runs headless."""

from __future__ import annotations

import multiprocessing
import os
import sys

USAGE = """Usage:
  Soundfont-to-XRNI [PATH...]                      open the window (PATHs are converted right away)
  Soundfont-to-XRNI --cli PATH... [--library DIR]  convert without a window

PATH: .sf2 / .sfz files, folders containing them, or .zip / .7z archives.
DIR:  Renoise library "Instruments" folder
      (default: Documents\\Renoise\\User Library\\Instruments)."""


def _cli(argv: list[str]) -> int:
    from . import engine

    library = engine.DEFAULT_LIBRARY
    paths = []
    it = iter(argv)
    for a in it:
        if a == "--library":
            library = next(it, library)
        elif not a.startswith("--"):
            paths.append(a)
    out = sys.stdout or open(os.devnull, "w", encoding="utf-8")  # the .exe has no console
    if not paths:
        print(USAGE, file=out)
        return 2
    results = engine.run_blocking(paths, library,
                                  lambda r: print(f"[{r['status']}] {r['dst']} ({r['samples']} samples, "
                                                  f"{r['size']} bytes) {r['message']}", file=out, flush=True))
    return 1 if any(r["status"] == "error" for r in results) else 0


def main() -> None:
    multiprocessing.freeze_support()
    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help", "--version"):
        from . import APP_NAME, __version__
        out = sys.stdout or open(os.devnull, "w", encoding="utf-8")
        print(f"{APP_NAME} {__version__}" if argv[0] == "--version" else USAGE, file=out)
        return
    if "--cli" in argv:
        sys.exit(_cli([a for a in argv if a != "--cli"]))
    from .gui import main as gui_main
    gui_main(argv)


if __name__ == "__main__":
    main()
