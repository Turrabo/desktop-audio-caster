# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one windowed .exe bundling the tray app, its assets, and
the native audio/cast stack. Build via scripts/build.ps1."""
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Native / plugin-heavy packages whose data files, DLLs, or dynamically
# imported submodules PyInstaller won't find by static analysis alone.
for pkg in ("pychromecast", "zeroconf", "pyaudiowpatch", "pycaw",
            "comtypes", "pystray", "aiohttp"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# Bundled fonts + app icon, resolved at runtime via sys._MEIPASS (see
# streamer/ui/fonts.py).
datas += [("assets", "assets")]
hiddenimports += ["pystray._win32"]

a = Analysis(
    ["launch_tray.pyw"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter.test", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DesktopAudioStreamer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                 # windowed: no console flash on launch
    icon="assets/app.ico",
    version="version_info.txt",
)
