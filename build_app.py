"""Build the menu-launched application without global Python changes."""
from pathlib import Path
import subprocess
import sys

HERE = Path(__file__).resolve().parent
result = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed",
    "--onedir", "--name", "OBS伴侣桥接", "--paths", str(HERE),
    "--paths", str(HERE / "app"), "--distpath", str(HERE / "dist"),
    "--workpath", str(HERE / "build"), "--specpath", str(HERE),
    "--hidden-import", "adapter.windows_transport", "--hidden-import", "adapter.reviewed_pipe_client",
    "--hidden-import", "win32pipe", "--hidden-import", "win32file",
    "--hidden-import", "win32event", "--hidden-import", "win32security",
    "--version-file", str(HERE / "version_info.txt"),
    str(HERE / "app" / "desktop_app.py")], cwd=HERE)
raise SystemExit(result.returncode)
