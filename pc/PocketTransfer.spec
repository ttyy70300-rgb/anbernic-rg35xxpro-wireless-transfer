# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\Guo\\WorkBuddy\\2026-10-04-22-19-16\\RG35XX-Transfer\\pc\\main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['protocol', 'client'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['numpy', 'PIL', 'pandas', 'matplotlib', 'scipy', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'wx', 'IPython', 'jupyter', 'notebook', 'pytest', 'setuptools', 'test', 'unittest', 'pydoc', 'doctest', 'sqlite3', 'email', 'http', 'xmlrpc', 'pdb', 'curses'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PocketTransfer',
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
    icon=['C:\\Users\\Guo\\WorkBuddy\\2026-10-04-22-19-16\\RG35XX-Transfer\\pc\\icon.ico'],
)
