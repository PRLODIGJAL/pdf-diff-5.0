#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""setup_env.py —— 环境自检与启动器（Windows / macOS 共用这一份）。

启动脚本会先跑它：.venv 已就绪就秒过，否则自动装依赖，然后拉起区别界面。

**打印格式统一走 bootui.py**（2026-10-23 抽出来，各项目逐字节同一份）：
标记只有 `[ OK ] [ !! ] [ -- ] [ .. ]` 四种，分节只用 `-- 名字 ----` 一行，
宽度固定 62 列。**别在这里直接 print** —— 一直接 print，几个项目的启动界面
就各长各的，那正是使用者抱怨的「文字部分千奇百怪」。

⚠️ 2026-10-19 安装策略修正（原指令C「完全离线」过于严格，用户拍板放宽）：
  · 本工具在「比对阶段」是**完全离线**的——两份 PDF 的内容绝不联网、绝不上传；
  · 但「首次装运行依赖」这一步**允许联网 pip 安装**，和其他已交付项目一致；
  · 优先用随包 `offline_wheels/` 离线安装（若打包时放了 wheel），
    没有离线包或离线装失败时，**回退到联网 pip 安装**，不再卡死报错。
  数据安全边界没变：只有「装依赖」联网，比对数据永不外传。

用法
----
    python setup_env.py            自检 → 按需安装 → 启动（正常双击走这条）
    python setup_env.py --check    只自检并打印结论，不安装、不启动
    python setup_env.py --setup    只把环境装好，不启动界面
    python setup_env.py --rebuild  不管现状，强制重建环境（可配 --setup 一起用）

【软件更新后要不要重装】不用。装好的东西在本文件夹的 .venv/ 里，
更新只是换掉几个 .py —— 别连 .venv 一起删就行。
"""
import os
import shutil
import subprocess
import sys
from typing import Optional, Tuple

import bootui as B

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
APP = os.path.join(HERE, "diff_app5.py")

TOOL_NAME = "PDF 对比工具"
TOOL_VERSION = "5.0"

# 5.0 依赖：numpy + pymupdf + 拖放库 + RapidOCR（统一 OCR 引擎，取代 Mac Vision
# 与 Windows Tesseract 的不一致）。这些依赖应在打包阶段随包分发（PyInstaller
# onedir 或随包 venv），运行期不再联网安装。
PKGS = ["numpy", "pymupdf", "tkinterdnd2", "rapidocr", "onnxruntime"]

IS_WIN = os.name == "nt"

# ONNX Runtime 1.31 的预编译 Windows 包硬性要求 VC++ 运行库 >= 14.40
# （官方 release note 原话：prebuilt Windows packages now require VC++ Runtime
#   version >= 14.40 instead of 14.38；低于此版本的典型现象是 import 时
#   "DLL load failed" 或初始化阶段直接崩）。
# 14.40 对应 VS 2022 17.10+；VS 2019 只到 14.29，差得远。
VC_MIN = (14, 40)
VC_REDIST_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"

# 依赖存在性探测：用 importlib.util.find_spec 只查「装没装」，**不真正 import**。
# 为什么不能直接 import onnxruntime 判断：载入 .pyd 会触发 DLL 解析，若 VC 运行库
# 太旧，import 必然失败 → 脚本会误判为「依赖没装」→ 每次双击都重装一遍。
# ⚠️ 判据仍是 find_spec 是否为 None，只是把结论**打印出来**（缺哪个包说哪个），
#    不再靠退出码 —— 缺包和「Python 自己跑不起来」混在一个退出码里，报不出人话。
_PROBE = (
    "import importlib.util as u;"
    "miss=[n for n in %r if u.find_spec(n) is None];"
    "print('MISS='+','.join(miss))"
)

# 离线 wheel 目录：打包时把各平台的依赖 wheel 放这里，随包分发。
OFFLINE_WHEELS = os.path.join(HERE, "offline_wheels")


def vc_runtime() -> Optional[Tuple[int, int]]:
    """读本机 x64 的 VC++ 运行库版本，返回 (major, minor)；读不到返回 None。

    读的是 VC++ 2015-2022 Redistributable 官方注册表键的 x64 子键。加
    KEY_WOW64_64KEY 是为了让 32 位 Python 也能读到原生视图的键（默认会被
    重定向到 WOW6432Node，那里没有这条记录）。
    """
    if not IS_WIN:
        return None
    try:
        import winreg  # 仅 Windows 提供

        key = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            ver, _ = winreg.QueryValueEx(k, "Version")  # 形如 "v14.40.33807.00"
        parts = str(ver).lstrip("vV").split(".")
        return (int(parts[0]), int(parts[1]))
    except (ImportError, OSError, ValueError, IndexError):
        return None


def vc_row() -> bool:
    """VC++ 运行库体检，打一行结论。返回 True = 满足 ONNX Runtime 要求。

    ⭐ 这一步必须在「装依赖」**之前**报出来：运行库旧的时候，依赖装得再全
       也加载不了 OCR，用户会白等几分钟再撞同一堵墙。
    """
    if not IS_WIN:
        B.item(B.SKIP, "运行库", "不是 Windows，这一项不适用")
        return True
    v = vc_runtime()
    if v is None:
        B.item(B.SKIP, "运行库", "读不到版本，跳过检查（不影响后面的安装）")
        return True
    if v >= VC_MIN:
        B.item(B.OK, "运行库", "VC++ %d.%d（OCR 引擎要求 %d.%d 以上）"
               % (v[0], v[1], VC_MIN[0], VC_MIN[1]))
        return True
    B.item(B.FAIL, "运行库", "VC++ %d.%d，低于要求的 %d.%d —— OCR 引擎会加载失败"
           % (v[0], v[1], VC_MIN[0], VC_MIN[1]))
    B.note("影响：转曲 / 图片区的文字识别静默失效，表现为「该标出来的差异没标」，"
           "程序本身不报错。")
    B.note("修复：装微软官方运行库，装完必须重启电脑（文件被占用时不会替换）：")
    B.raw("    " + VC_REDIST_URL)
    return False


def venv_python() -> str:
    p = os.path.join(VENV, "Scripts" if IS_WIN else "bin",
                     "python.exe" if IS_WIN else "python")
    return p if os.path.exists(p) else ""


def run(cmd, **kw) -> int:
    """跑子进程，输出直接接当前终端（pip 装大包时要能看到进度）。"""
    try:
        return subprocess.run(cmd, **kw).returncode
    except OSError as e:
        B.note("调用失败：%s" % e)
        return 1


def missing_pkgs(py: str) -> list:
    """这台机器的 .venv 里还缺哪些包。只查「装没装」，不真正 import。"""
    try:
        p = subprocess.run([py, "-c", _PROBE % (PKGS,)],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        return list(PKGS) + ["（探测失败：%s）" % e]
    if p.returncode != 0:
        # 解释器自己都跑不起来（多半是别的机器的 venv）：当成「全都缺」，
        # 让后面的安装流程去处理，而不是在这里猜。
        return list(PKGS)
    for ln in (p.stdout or "").splitlines():
        if ln.startswith("MISS="):
            return [x for x in ln[5:].strip().split(",") if x]
    return list(PKGS)


def version_code() -> str:
    """读各依赖版本用的小脚本（包名从 PKGS 派生，不写死）。"""
    return ("import importlib.metadata as md\n"
            "for n in %r:\n"
            "    try:\n"
            "        print(f'{n} {md.version(n)}')\n"
            "    except Exception:\n"
            "        print(n)" % (PKGS,))


def dep_versions(py: str) -> str:
    """「依赖」那一行的值：`numpy 2.1.3 · pymupdf 1.24.10 · …`。

    读不出版本时只列包名 —— 宁可少一样信息，也不要让这一行报错。
    """
    try:
        p = subprocess.run([py, "-c", version_code()],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError:
        return "、".join(PKGS)
    if p.returncode != 0:
        return "、".join(PKGS)
    items = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    return " · ".join(items) or "、".join(PKGS)


def make_venv() -> str:
    """建虚拟环境，返回解释器路径（失败返回空串）。"""
    B.step("创建虚拟环境 .venv")
    if run([sys.executable, "-m", "venv", VENV]) != 0:
        B.item(B.FAIL, "虚拟环境", "创建失败 —— 请确认已装 Python 3.9 以上")
        return ""
    py = venv_python()
    if not py:
        B.item(B.FAIL, "虚拟环境", "建好了但找不到解释器")
        return ""
    return py


def pip_install(py: str, need: list) -> bool:
    """装依赖：先试离线包，再回退联网。返回装好没有。"""
    wheels = []
    if os.path.isdir(OFFLINE_WHEELS):
        wheels = [os.path.join(OFFLINE_WHEELS, f)
                  for f in os.listdir(OFFLINE_WHEELS)
                  if f.endswith(".whl")]
    if wheels:
        B.step("从本地离线包安装依赖（%d 个 wheel，不联网）" % len(wheels))
        pip = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check",
               "--no-index", "--find-links", OFFLINE_WHEELS]
        if run(pip + need) == 0:
            B.item(B.OK, "依赖", "离线包装好了（没有联网）")
            return True
        B.note("离线包装不上，改用联网安装…")
    # 联网安装兜底：只用于「装依赖」，比对数据绝不联网。
    B.step("从 PyPI 联网安装依赖（只有安装阶段联网，比对数据不上传）")
    B.note("首次约 1～3 分钟，请勿关窗口")
    pip = [py, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--upgrade"]
    if run(pip + need) != 0:
        B.item(B.FAIL, "依赖", "联网安装也失败 —— 检查网络后重试")
        return False
    B.item(B.OK, "依赖", "装好了")
    return True


def probe_onnx(py: str) -> bool:
    """真加载一次 onnxruntime。

    「装上了」≠「能加载」—— DLL 缺失只有真正 import 才暴露，而它一旦失败，
    主程序会静默降级成「OCR 全部失效但界面正常」，用户只会觉得「识别不准」。
    这里把它提前变成一句明确的报错。
    """
    if not IS_WIN:
        return True
    B.step("试加载 OCR 引擎 onnxruntime")
    try:
        p = subprocess.run([py, "-c", "import onnxruntime as o; print(o.__version__)"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
    except OSError as e:
        B.item(B.FAIL, "依赖", "onnxruntime 加载不了 —— %s" % e)
        return False
    if p.returncode == 0:
        B.item(B.OK, "依赖", "OCR 引擎 onnxruntime %s 加载正常" % (p.stdout or "").strip())
        return True
    B.item(B.FAIL, "依赖", "onnxruntime 装上了但加载不了 —— 基本都是 VC++ 运行库太低")
    tail = (p.stderr or "").strip().splitlines()[-3:]
    if tail:
        B.note("原始报错：")
        for line in tail:
            B.note(line)
    B.note("修复：装下面这个运行库，装完必须重启电脑：")
    B.raw("    " + VC_REDIST_URL)
    return False


def ready_rows(py: str) -> None:
    """环境就绪时的固定两行 —— 每条路都长这样，方便扫。"""
    B.item(B.OK, "虚拟环境", os.path.relpath(VENV, HERE))
    B.item(B.OK, "依赖", dep_versions(py))


def ensure_env(rebuild: bool) -> str:
    """确保有一个能用的环境，返回解释器路径（失败返回空串）。

    ⚠️ 行序是**故意**的：虚拟环境 → 运行库 → 依赖。
       「运行库」必须在「装依赖」之前报出来：它不行的话，依赖装得再全
       也加载不了 OCR，用户会白等几分钟再撞同一堵墙。
    """
    if rebuild and os.path.isdir(VENV):
        B.step("删除旧虚拟环境 .venv")
        shutil.rmtree(VENV, ignore_errors=True)

    py = venv_python()
    if not py:
        B.item(B.SKIP, "虚拟环境", "还没装过，这一次先把环境备好")
    else:
        B.item(B.OK, "虚拟环境", os.path.relpath(VENV, HERE))

    vc_row()

    if not py:
        B.section("", "准备环境")
        py = make_venv()
        if not py:
            return ""

    need = missing_pkgs(py)
    if need:
        real = [n for n in need if not n.startswith("（")]
        B.item(B.SKIP, "依赖", "缺 %s，开始安装" % "、".join(real or need))
        if not pip_install(py, real or list(PKGS)):
            return ""
    else:
        B.item(B.OK, "依赖", dep_versions(py))

    if not probe_onnx(py):
        return ""
    return py


def launch(py: str) -> int:
    """用虚拟环境的解释器拉起界面。"""
    if not os.path.exists(APP):
        B.end(B.FAIL, "找不到 diff_app5.py",
              steps=["确认这个文件夹是完整的（diff_app5.py 和 core/ 都在同一层）",
                     "重新拷一份完整的交付包"])
        return 1
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"       # 免得 Windows 控制台用 cp936 打印报错
    env["PYTHONUTF8"] = "1"
    B.end(B.OK, "环境就绪，正在打开界面…（关掉界面窗口即可退出）")
    try:
        return subprocess.run([py, "-B", APP], cwd=HERE, env=env).returncode
    except KeyboardInterrupt:
        return 0


def do_check() -> int:
    """`--check`：只看不动，把结论一次说完。"""
    py = venv_python()
    exist = os.path.isdir(VENV)
    B.section("", "运行环境")
    B.item(B.OK if exist else B.SKIP, "虚拟环境",
           "%s（%s）" % (os.path.relpath(VENV, HERE), "存在" if exist else "不存在"))
    if not py:
        B.item(B.FAIL, "依赖", "还没装过")
        B.end(B.FAIL, "环境还没准备好",
              steps=["双击「运行前的环境准备.bat」，它会当场把环境备好"])
        return 1
    miss = missing_pkgs(py)
    if miss:
        B.item(B.FAIL, "依赖", "缺 %s" % "、".join(miss))
        B.end(B.FAIL, "环境还没准备好",
              steps=["双击「运行前的环境准备.bat」，它会当场把环境备好"])
        return 1
    B.item(B.OK, "依赖", dep_versions(py))
    vc_row()
    probe_onnx(py)
    B.end(B.OK, "可以正常启动")
    return 0


def main() -> int:
    args = sys.argv[1:]

    B.head(TOOL_NAME, "v" + TOOL_VERSION, "运行前自检",
           [("系统", B.os_line()),
            ("Python", "%d.%d.%d" % sys.version_info[:3]),
            ("位置", HERE)])

    if "--check" in args:
        return do_check()

    B.section("", "运行环境")
    py = ensure_env(rebuild="--rebuild" in args)
    if not py:
        B.end(B.FAIL, "没能准备好环境",
              steps=["把这一屏截图发给我，我按情况处理",
                     "想重来一次就双击「运行前的环境准备.bat」"])
        return 1

    if "--setup" in args:
        B.end(B.OK, "环境已就绪，以后双击启动器就能直接用")
        return 0

    return launch(py)


if __name__ == "__main__":
    raise SystemExit(main())
