"""Build the Windows executable and the release archive.

    py -m pip install -r requirements-build.txt
    py build.py

Output: dist/Soundfont-to-XRNI.exe and dist/Soundfont-to-XRNI-<version>-windows-x64.zip
(the .exe + README + LICENSE, ready to attach to a GitHub release).
"""

import os
import sys
import tempfile
import zipfile

import PyInstaller.__main__

from soundfont_to_xrni import APP_NAME, __version__

HERE = os.path.dirname(os.path.abspath(__file__))

# Packages with native extensions / data files PyInstaller must bundle completely.
COLLECT = ["tkinterdnd2", "_soundfile_data", "py7zr", "pyppmd", "pybcj", "inflate64", "multivolumefile"]


def main() -> None:
    os.chdir(HERE)
    icon = os.path.join(HERE, "icon.ico")
    with tempfile.TemporaryDirectory(prefix="s2x_build_") as work:  # keeps build/ and .spec out of the repo
        args = [os.path.join(HERE, "app.py"), "--noconfirm", "--onefile", "--windowed", "--name", APP_NAME,
                "--icon", icon, "--add-data", f"{icon}{os.pathsep}.",
                "--distpath", os.path.join(HERE, "dist"), "--workpath", work, "--specpath", work,
                "--exclude-module", "PIL", "--exclude-module", "pytest"]
        for package in COLLECT:
            args += ["--collect-all", package]
        PyInstaller.__main__.run(args)

    exe = os.path.join("dist", APP_NAME + ".exe")
    archive = os.path.join("dist", f"{APP_NAME}-{__version__}-windows-x64.zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(exe, os.path.basename(exe))
        z.write("README.md")
        z.write("LICENSE")
    print(f"\nBuilt {exe}\n      {archive}")


if __name__ == "__main__":
    sys.exit(main())
