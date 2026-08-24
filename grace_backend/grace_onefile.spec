# grace_onefile.spec — single-file, windowed (no console) build of Grace.
# Build from grace_backend/:  pyinstaller grace_onefile.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ('../index.html', '.'),
    ('../styles.css', '.'),
    ('../setup.html', '.'),
]
binaries = []
hiddenimports = ['main']
hiddenimports += collect_submodules('routers')
hiddenimports += collect_submodules('services')

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

# Single file (a.binaries + a.datas in EXE, no COLLECT), windowed (no console).
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='Grace',
    console=False,
    icon=None,
)
