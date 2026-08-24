# grace.spec — PyInstaller build for the Grace desktop app.
# Build from the grace_backend/ folder:  pyinstaller grace.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ('../index.html', '.'),
    ('../styles.css', '.'),
    ('../setup.html', '.'),
]
binaries = []
hiddenimports = ['main']

# The app's own packages (make sure every router/service is pulled in).
hiddenimports += collect_submodules('routers')
hiddenimports += collect_submodules('services')

# Third-party libs that use dynamic imports and/or ship data files.
for pkg in ['uvicorn', 'webview', 'google.genai', 'psycopg', 'sqlalchemy',
            'edge_tts', 'googleapiclient', 'google_auth_oauthlib', 'google.auth',
            'google.oauth2', 'docx', 'pypdf', 'fpdf', 'anyio', 'dotenv',
            'PIL', 'send2trash', 'pycaw', 'comtypes', 'psutil']:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print('collect_all skipped', pkg, e)

hiddenimports += collect_submodules('uvicorn')

a = Analysis(
    ['desktop.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PyInstaller', 'pytest'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Grace',
    console=True,          # console during validation; flip to False for the final windowed build
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name='Grace')
