# PyInstaller spec: one-folder Windows build -> dist\Orchestrator\Orchestrator.exe
# Build with:  powershell -ExecutionPolicy Bypass -File scripts\build.ps1
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas, binaries, hiddenimports = [], [], []
for pkg in ("claude_agent_sdk", "qfluentwidgets", "qasync", "apscheduler", "keyring", "pygments", "markdown"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# mcp: skip its CLI (needs typer, exits on import) and the v1 fastmcp stub (raises on import)
hiddenimports += collect_submodules(
    "mcp", filter=lambda n: not n.startswith(("mcp.cli", "mcp.server.fastmcp")))
datas += collect_data_files("mcp")
hiddenimports += collect_submodules("orchestrator")
hiddenimports += ["keyring.backends.Windows", "sqlalchemy.dialects.sqlite", "win32ctypes.core"]

a = Analysis(
    ["run.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick3D", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Orchestrator",
    console=False,  # windowed; --mcp-server still talks over the stdio pipes the engine gives it
    icon="build/orchestrator.ico",
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Orchestrator")
