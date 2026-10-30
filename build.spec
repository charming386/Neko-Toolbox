# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：单文件、无控制台、带猫娘图标。

打包要点：
  * onefile —— 资源（assets/、app.ico）解压到 sys._MEIPASS，只读使用
  * data/ 不打包 —— 由 store.py 定位到 exe 所在目录，重装/重启都不丢
  * excludes —— 排除 PySide6 没用到的重量级模块，减小体积
"""
import os

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

proj = os.path.abspath(SPECPATH)

a = Analysis(
    ["app.py"],
    pathex=[proj],
    binaries=[],
    datas=[
        ("assets", "assets"),
        ("app.ico", "."),
    ],
    hiddenimports=[
        "win32com.client",
        "win32com.shell",
        "pythoncom",
        "pywinauto",
        "comtypes",
        "win32api",
        "win32con",
        "win32gui",
        "win32clipboard",
        # markdown 的扩展是运行时按名字动态 import 的，静态分析找不到，必须显式收进来
        *collect_submodules("markdown"),
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc", "doctest", "test",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtQuick", "PySide6.QtQuick3D",
        "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets", "PySide6.QtDesigner", "PySide6.QtBluetooth",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf",
        "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets", "PySide6.QtSpatialAudio", "PySide6.QtSensors",
        "matplotlib", "numpy", "scipy", "pandas", "PIL.ImageQt",
    ],
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
    a.zipfiles,
    a.datas,
    [],
    name="猫娘工具箱",
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
    icon=os.path.join(proj, "app.ico"),
    # 显式指定清单：Per-Monitor V2 DPI 感知。
    # 不加这个，Windows 会把窗口当成「系统 DPI 感知」，拖到缩放比不同的另一块屏上
    # 会被整体位图拉伸 —— 于是小屏上就「显示不全」了。
    manifest=os.path.join(proj, "app.manifest"),
)
