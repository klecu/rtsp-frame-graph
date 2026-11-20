"""Build a portable Windows x64 release with PyInstaller."""

from pathlib import Path
from shutil import copyfileobj, make_archive
import subprocess
import sys
from sysconfig import get_platform
from tempfile import TemporaryDirectory


APP_NAME = "RTSP Frame Graph"
ARCHIVE_NAME = "RTSP-Frame-Graph-Windows-x64"
ROOT = Path(__file__).resolve().parent


def main():
    if get_platform() != "win-amd64":
        raise SystemExit("Build the Windows bundle with x64 Python on Windows.")

    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="rtsp-frame-graph-build-") as directory:
        stage = Path(directory)
        bundles = stage / "bundles"
        command = [
            sys.executable, "-m", "PyInstaller", "--onedir", "--windowed",
            "--noupx", "--name", APP_NAME, "--distpath", str(bundles),
            "--workpath", str(stage / "work"), "--specpath", str(stage),
        ]
        for distribution in ("av", "numpy", "PySide6", "PySide6-Essentials",
                             "shiboken6", "pyqtgraph", "colorama"):
            command.extend(("--copy-metadata", distribution))
        subprocess.run([*command, str(ROOT / "rtsp_frame_graph.py")], cwd=ROOT, check=True)

        (bundles / APP_NAME / "Read me.txt").write_text(
            "Extract the entire ZIP before running RTSP Frame Graph.exe.\n"
            "Enter the camera's RTSP address and credentials in the application.\n"
            "The application saves each camera's last working URL and username locally.\n",
            encoding="utf-8",
        )
        archive = make_archive(str(stage / ARCHIVE_NAME), "zip", bundles, APP_NAME)
        target = output / f"{ARCHIVE_NAME}.zip"
        # Copy across volumes if Windows keeps its temporary directory elsewhere.
        temporary_archive = output / f"{ARCHIVE_NAME}.zip.tmp"
        try:
            with Path(archive).open("rb") as source, temporary_archive.open("wb") as destination:
                copyfileobj(source, destination)
            temporary_archive.replace(target)
        finally:
            temporary_archive.unlink(missing_ok=True)
        print(target)


if __name__ == "__main__":
    main()
