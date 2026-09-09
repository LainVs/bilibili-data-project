# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：B站数据采集分析 GUI。

用法:
    pyinstaller build.spec --noconfirm
产物: dist/BiliDataAnalyzer/BiliDataAnalyzer.exe
"""
import os
from pathlib import Path

ROOT = Path(SPECPATH)

a = Analysis(
    [str(ROOT / "gui_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "config"), "config"),
    ],
    hiddenimports=[
        # 各采集任务模块（GUI 按需延迟导入）
        "crawler.tasks.full",
        "crawler.tasks.increment",
        "crawler.tasks.up_crawl",
        "crawler.tasks.fav_crawl",
        "crawler.tasks.relation_crawl",
        "crawler.tasks.post_crawl",
        "crawler.tasks.broad_crawl",
        "crawler.tasks.scheduler",
        "crawler.tasks.resolve_uid",
        "crawler.api.popular",
        "tools.db_transfer",
        "gui.db_config_dialog",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="BiliDataAnalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="BiliDataAnalyzer",
)
