# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for voice2text.

Bundles the binary-heavy ML stack (ctranslate2, av/PyAV, onnxruntime,
tokenizers) plus faster-whisper's data assets (the bundled Silero VAD model).

The Whisper weights themselves (MODEL_SIZE) are NOT bundled — they are
downloaded on first run via huggingface_hub into the user's HF cache.
"""

import glob
import os
import sys

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# tkinter on this interpreter links against Tcl/Tk 9.0 shared libraries that
# live in a non-standard (snap + uv) layout, so PyInstaller's automatic
# dependency scan can't resolve them. Add them explicitly. The script-library
# data dirs (_tcl_data / _tk_data) are handled by the standard _tkinter hook.
_lib = os.path.join(sys.base_prefix, "lib")
for _so in glob.glob(os.path.join(_lib, "libtcl9*.so")) + glob.glob(
    os.path.join(_lib, "libtk9*.so")
):
    binaries.append((_so, "."))
if not any(name.endswith("libtcl9.0.so") for name, _ in binaries):
    raise SystemExit(f"Tcl/Tk 9 shared libs not found under {_lib}")

# Packages that ship binaries and/or data files PyInstaller can't infer.
for pkg in ("faster_whisper", "ctranslate2", "av", "onnxruntime", "tokenizers"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h


block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="voice2text",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
