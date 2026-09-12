# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['cockpit_main.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('cockpit/ui/theme.json', 'cockpit/ui'),
        ('cockpit/ui/theme.schema.json', 'cockpit/ui'),
        ('cockpit/ingestion/config/default_traveler_map.json', 'cockpit/ingestion/config'),
    ],
    hiddenimports=[
        'cockpit.ingestion.parsers.bom_parser',
        'cockpit.ingestion.parsers.eco_build_notes',
        'cockpit.ingestion.parsers.audit_bom',
        'sqlite3',
    ],
    hookspath=['build/hooks'],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'unittest',
        'pydoc',
        'xmlrpc',
        'pytest',
        'pytest_qt',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Filter out PyQt6 plugins that are not needed
allowed_plugins = ('platforms', 'styles', 'imageformats')
a.binaries = [x for x in a.binaries if not ('PyQt6' in x[1] and 'plugins' in x[1] and not any(p in x[1] for p in allowed_plugins))]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Cockpit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Cockpit',
)
