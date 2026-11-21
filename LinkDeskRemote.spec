# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['./app.py'],
    pathex=[],
    binaries=[],
    datas=[('./signal/worker.mjs', 'signal'), ('./signal/wrangler.toml', 'signal'), ('./render_signal/server.mjs', 'render_signal'), ('./render_signal/worker.mjs', 'render_signal')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['cv2', 'matplotlib', 'scipy', 'IPython', 'pandas', 'pytest', 'PyQt6', 'PySide6'],
    noarchive=False,
    optimize=0,
)
from pathlib import Path

wheel_dlls = {
    (Path(target).name.casefold(), Path(source).resolve())
    for target, source, _ in a.binaries + a.datas
    if Path(target).parent.name in {"av.libs", "numpy.libs"}
    and Path(target).suffix.lower() == ".dll"
}
a.binaries = [
    (target, source, kind) for target, source, kind in a.binaries
    if not (Path(target).parent == Path(".")
            and (Path(target).name.casefold(), Path(source).resolve()) in wheel_dlls)
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='LinkDeskRemote',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)




