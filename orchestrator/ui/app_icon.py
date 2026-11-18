"""The app icon (rendered from assets/icon.svg by scripts/make_icon.py), Windows taskbar identity and shortcuts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ID = "Orchestrator.App"  # AppUserModelID: groups our windows on the taskbar under our icon, not python's
ICON_DIR = Path(__file__).resolve().parents[1] / "assets" / "icons"
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def icon_file(size: int) -> Path:
    return ICON_DIR / f"icon-{size}.png"


def ico_file() -> Path:
    return ICON_DIR / "orchestrator.ico"


def app_icon():
    """QIcon with every rendered size (Qt picks the sharpest one for each use)."""
    from PySide6.QtGui import QIcon

    icon = QIcon()
    for s in SIZES:
        p = icon_file(s)
        if p.is_file():
            icon.addFile(str(p))
    if icon.isNull():
        return _drawn_fallback()
    return icon


def _drawn_fallback():
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#101a2e"))
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setBrush(QColor("#4cc2ff"))
    p.drawEllipse(22, 34, 20, 20)
    p.setBrush(QColor("white"))
    for x in (12, 28, 44):
        p.drawEllipse(x, 10, 10, 10)
    p.end()
    return QIcon(pm)


def set_app_user_model_id() -> bool:
    """Call before any window exists: the taskbar then shows our icon (not pythonw's) and pins this app."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID) == 0
    except (AttributeError, OSError):
        return False


# ---------------------------------------------------------------- shortcuts

def launch_target() -> tuple[str, str, str]:
    """(program, arguments, working folder) that starts this app without a console."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        return str(exe), "", str(exe.parent)
    root = Path(__file__).resolve().parents[2]
    pyw = Path(sys.executable).with_name("pythonw.exe")
    if not pyw.is_file():
        pyw = Path(sys.executable)
    return str(pyw), f'"{root / "run.py"}"', str(root)


def shortcut_paths() -> dict[str, Path]:
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    desktop = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"
    try:  # the real Desktop folder (may be redirected, e.g. to OneDrive)
        from win32com.shell import shell, shellcon

        desktop = Path(shell.SHGetFolderPath(0, shellcon.CSIDL_DESKTOPDIRECTORY, None, 0))
    except Exception:  # noqa: BLE001 - fall back to %USERPROFILE%\Desktop
        pass
    return {"desktop": desktop / "Orchestrator.lnk",
            "start_menu": appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Orchestrator.lnk"}


def create_shortcut(path: Path) -> Path:
    """Write a .lnk that starts the app with our icon and AppUserModelID (so pinning keeps working)."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    target, args, cwd = launch_target()
    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)
    link.SetPath(target)
    link.SetArguments(args)
    link.SetWorkingDirectory(cwd)
    link.SetDescription("Orchestrator")
    icon = target if getattr(sys, "frozen", False) else str(ico_file())
    link.SetIconLocation(icon, 0)
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    store.Commit()
    path.parent.mkdir(parents=True, exist_ok=True)
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)
    return path


def read_shortcut(path: Path) -> dict[str, str]:
    """Target, arguments, icon and AppUserModelID of a .lnk (for tests and checks)."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)
    link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path))
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    return {"target": link.GetPath(shell.SLGP_RAWPATH)[0], "args": link.GetArguments(),
            "icon": link.GetIconLocation()[0], "app_id": str(store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue())}
