# PyInstaller 配方：把 matrix-connect 打成自带 Python 的独立程序（onedir），用户电脑上不需要 uv、git、Python。
#   uv run --with pyinstaller pyinstaller packaging/matrix-connect.spec --noconfirm
# 产物在 dist/matrix-connect/：可执行文件 matrix-connect（Windows 上另有无控制台版 matrix-connectw.exe，后台任务用）。
# 打包与安装的设计见 docs/device-runtimes.md 第 2.7 节；CI 见 .github/workflows/connect-release.yml。
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent

# 外部驱动（docs/device-driver-authoring.md 第 5 节）：MATRIX_CONNECT_EXTRA_DRIVERS=<发行包名>,… 把装在打包环境里的
# 驱动包一起打进去。连接器按 entry point「matrix.connect_drivers」发现驱动，所以要带上包的元数据（copy_metadata），
# 并收齐 entry point 指向的那个顶层包的全部模块。
import os
from importlib.metadata import distribution
from PyInstaller.utils.hooks import collect_submodules, copy_metadata

extra_datas, extra_imports = [], []
for dist_name in filter(None, (x.strip() for x in os.environ.get("MATRIX_CONNECT_EXTRA_DRIVERS", "").split(","))):
    dist = distribution(dist_name)
    extra_datas += copy_metadata(dist_name)
    for ep in dist.entry_points:
        if ep.group == "matrix.connect_drivers":
            extra_imports += collect_submodules(ep.value.split(":")[0].split(".")[0])

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    # dsh 的覆盖层按文件路径引用这几个插件（runtime.APPROVAL_PLUGIN 等），要作为文件放进包里
    datas=[(str(ROOT / "matrix" / "dsh_plugins" / "*.mjs"), "matrix/dsh_plugins"), *extra_datas],
    # 二进制被当成 Python 拉起这几个 MCP 服务（main.BUNDLED_SCRIPTS）；websockets 是函数里才导入的
    hiddenimports=["matrix.mcp_server", "matrix.mcp_connectors", "matrix.web_fetch", "websockets.sync.client",
                   *extra_imports],
    # 连接器用不到服务端的依赖：别让它们被顺带打进来
    excludes=["fastapi", "starlette", "uvicorn", "pydantic", "cryptography", "tkinter", "matrix.server", "matrix.engine"],
    noarchive=False,
)
pyz = PYZ(a.pure)

def exe(name, console):
    return EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, console=console,
               strip=False, upx=False, codesign_identity=None, entitlements_file=None)

exes = [exe("matrix-connect", True)]
if sys.platform == "win32":
    exes.append(exe("matrix-connectw", False))  # 后台任务用：没有控制台窗口（service.WINDOWLESS_EXE）

coll = COLLECT(*exes, a.binaries, a.datas, strip=False, upx=False, name="matrix-connect")
