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

# Bundled fonts, app icon, and opus.dll, resolved at runtime via sys._MEIPASS
# (see streamer/assets.py). opus.dll is a static-CRT build depending only on
# KERNEL32, so shipping it as data (not a scanned binary) is safe.
datas += [("assets", "assets")]
# The mirror stack is imported lazily in appctl (so a bad import only routes to
# HTTP), so name it explicitly to guarantee it is frozen in.
hiddenimports += ["pystray._win32", "streamer.mirror", "streamer._opus",
                  "streamer._aesctr"]

a = Analysis(
    ["launch_tray.pyw"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # av + cryptography are spike-only (experiments/mirroring); the shipped app
    # encodes via ctypes->opus.dll and encrypts via Windows CNG, so neither is
    # needed at runtime. Excluding them keeps the exe lean.
    excludes=["tkinter.test", "test", "unittest", "av", "cryptography"],
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
