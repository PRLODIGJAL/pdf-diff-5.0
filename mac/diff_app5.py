# -*- coding: utf-8 -*-
"""
diff_app5.py —— PDF 差异对比工具 5.0（单窗口直显版）
==================================================================
5.0 起点（2026-10-20）：4.0 定版后整体归档到 `vibecoding成品/项目2PDF差异对比/`，
本文件由 `diff_app3.py` 改名而来（文件名后缀终于对上了版本号），
改名当时**代码逻辑一行未改**，行为与 4.0 最终态完全一致；5.0 的新能力从这份基线往上加。
最终方案（用户 2026-10-13 晚拍板，2026-10-15 升级 4.0）：
  识别**用新版引擎**（core/pdfdiff_locate.locate：字符级 diff + 转曲区像素
  兜底 + 5 倍比例移动判定），展示为单窗口直显界面：上面选文件点对比，
  下面左右并排标注，一个窗口完事。不弹第二个预览窗，没有文字清单层。

  曾试过纯像素引擎（pdfdiff3.py），对文字 PDF 太糙（整段糊红绿），弃用；
  pdfdiff3.py 保留但不再被界面调用。

渲染管线（自包含，不依赖 diff_preview）：
  fitz.open(路径) → ann.paint_into(内存画标注) → get_pixmap → PNG → Tk 位图。
  「屏幕看到的」与 paint_into 同源，屏幕=导出。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "PDF 差异对比工具 5.0"
FONT = "PingFang SC"
BG = "#f5f6f8"
CARD = "#ffffff"
BORDER = "#d9dee5"
TEXT = "#2a2f36"
TEXT2 = "#7a828c"
PRIMARY = "#2f6fed"
PRIMARY_D = "#2257c4"

# ---- 唯一拖放区（2026-10-20 刘剑二次要求：三行合成**一块**）----
# 原来「合并框 + 旧版/新版两行」三层入口，刘剑的实际操作永远是「找文件 → 拖进来」，
# 两行手工指定属于多余的精确度。现在只剩这一块：**一次拖两个**或**一份一份拖**
# 都走同一套自动排位（早的＝旧版），右边一个「清空」重置。
DZ_BG = "#f7f9fc"          # 常态底色
DZ_BG_ON = "#eaf1ff"       # 拖入悬停
DZ_BORDER_ON = PRIMARY
# ⚠️ 提示只能**一行**，且必须在 900px 最小窗口下**不折行/不裁字**
#    （PingFang SC 10 实测 450px，可用 490px；再多 4 个汉字就会被切掉）。
# ⚠️ 900px 最小窗口下提示只给 ~440px（PingFang SC 10 号实测），**每改一次都要
#    量宽**：含「早的＝旧版」的变体全在 458px+ 会被裁字（2026-10-21 实测）。
#    「早的＝旧版」不写这里 —— 槽位左侧的「旧版 / 新版」标签与拖放后的状态栏
#    文案都会说，提示行只保留三要素：拖到这里 / 一份一份也行 / 按文件名排位。
DROP_HINT = "PDF/图片/Word/Excel 拖到这里　·　一份一份拖也行，按文件名排位"
SLOT_EMPTY = "（空　·　拖入或点「浏览…」）"

# ---- 「停止对比」（2026-10-20 刘剑要求）----
# 大面积偏移/多页文件跑一次几分钟，选错文件得能当场止损，不用退出软件。
# 走的是**协作式中止**（core/pdfdiff_locate 的 Cancelled）：在 OCR/区域/页 这些
# 耗时单元的边界上查标志，命中就退栈 —— 判定代码一行没改，未点停止时结果不变。
STOP_IDLE = "■ 停止对比"
STOP_GOING = "停止中…"
STOP_FG = "#c0392b"        # 停 = 红（与「红=删减」不冲突：按钮是白底细边框）


def _nat_tokens(s: str):
    """字符串 → 自然排序键（连续数字按数值）。空串给空元组，避免比较报错。"""
    out = []
    for tok in re.split(r"(\d+)", s or ""):
        if not tok:
            continue
        out.append((1, int(tok)) if tok.isdigit() else (0, tok.lower()))
    return tuple(out)


def _nat_key(path: str):
    """文件名自然排序键：**先比主名、再比扩展名**，数字按数值比。

    为什么不能直接 `sorted()`：字符串序里 `rev10` 会排在 `rev9` 前面
    （"1" < "9"），`Picture2.pdf` 也会跑到 `Picture.pdf` 前面。做法是把
    连续数字切成段、数字转 int 逐段比较。

    ⚠️ 主名与扩展名**必须分开比**：`Picture.pdf` 的单词段是 `picture.pdf`，
    整串比会输给 `Picture2.pdf` 的 `picture`（前缀更短者小），
    于是「Picture.pdf」被当成新版 —— 桌面 `对比案例/label/` 正是这种命名。
    """
    base = os.path.basename(path or "")
    stem, ext = os.path.splitext(base)
    return (_nat_tokens(stem), _nat_tokens(ext))


def _pair_from_paths(paths):
    """两个以上路径 → (旧, 新)。排序在前的当旧版，多余的丢弃（调用方负责提示）。"""
    ps = sorted([p for p in paths if p], key=_nat_key)
    if not ps:
        return "", ""
    if len(ps) == 1:
        return ps[0], ""
    return ps[0], ps[1]


def _fmt_error(e: BaseException) -> str:
    """异常 →「一句人话 + 完整栈落盘路径」。

    ⚠️ 2026-10-22 的教训：Windows 上只显示
       `expected str, bytes or os.PathLike object, not NoneType`
       —— 这种消息里没有一个项目专有名词，光看它无法定位是哪一行，
       只能靠反复猜。以后所有后台异常都连栈写进临时目录，
       用户把路径那一行发过来就能直接跳到文件和行号。
    栈用 `e.__traceback__` 显式取，不依赖 sys.exc_info 的上下文。
    """
    import traceback
    msg = str(e).strip() or e.__class__.__name__
    log = os.path.join(tempfile.gettempdir(), "PDF差异5.0_错误日志.txt")
    try:
        tb = "".join(traceback.format_exception(
            type(e), e, e.__traceback__))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("\n===== %s =====\n%s\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), tb))
        return msg + "\n\n（完整技术日志：%s —— 报障时把这一行发过来即可）" % log
    except Exception:
        return msg


def _mk_check(parent, text, var, command):
    """统一的勾选框：**画的是对勾，不是叉**。

    （2026-10-21 刘剑反馈）`ttk.Checkbutton` 在本机 clam 主题下，选中态画出来的是
    一个「✗」，和「关闭 / 不通过」的语义撞车，很容易让人以为没勾上。
    改用经典 `tk.Checkbutton`：macOS 由系统画原生蓝底白勾，
    三处勾选（删减/新增/位移、底图灰度、开跑前提示）外观就一致了。
    """
    return tk.Checkbutton(parent, text=text, variable=var, command=command,
                          bg=BG, fg=TEXT, activebackground=BG,
                          activeforeground=TEXT, selectcolor=CARD,
                          font=(FONT, 10), bd=0, highlightthickness=0,
                          anchor="w")


try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False

try:
    import pymupdf as fitz
except ImportError:
    import fitz  # type: ignore

# 上次选择的文件记忆（沿用 2.0 的机制：同目录「上次选择.json」）
_HERE = os.path.dirname(os.path.abspath(__file__))
# 识别引擎已归入 core/ 子文件夹，把 core 加进模块搜索路径（老引擎入口）
_sys = __import__("sys")
_CORE = os.path.join(_HERE, "core")
if os.path.isdir(_CORE) and _CORE not in _sys.path:
    _sys.path.insert(0, _CORE)

import pdf_annotate as ann
import pdfdiff_locate as loc            # 只为了「停止对比」的取消标志（见 _stop）
import pdfdiff_preflight as pre         # 开跑前体检：纯图片/扫描件先问一句（见 _gate_image_only）
CONFIG_PATH = os.path.join(_HERE, "上次选择.json")

DPI_STEPS = (40, 55, 80, 110, 140, 180, 240)
DPI_MIN, DPI_MAX = 40, 240
CACHE_MAX = 6            # 渲染缓存张数（PhotoImage 很占内存，别无限攒）


def _gray_luma(rgb):
    """按 ITU-R BT.601 亮度公式把 RGB（最后一维 3 或 4 通道）压成灰度。"""
    import numpy as np
    return ((rgb[..., 0].astype(np.int32) * 299
             + rgb[..., 1].astype(np.int32) * 587
             + rgb[..., 2].astype(np.int32) * 114) // 1000)


def _pixmap_gray_under_annots(page, dpi: int, plain_page=None):
    """底图去色、标注**保持原色**地渲染一页。

    为什么不能直接用 `get_pixmap(colorspace=csGRAY)`：那会把红/绿/浅蓝标注
    一起灰掉，三色区分就没了。

    为什么需要 `plain_page`：标注不是 PDF annotation，而是 `paint_into()` 用
    `draw_rect` 直接写进**页面内容流**的（见 core/pdf_annotate.py `_paint`），
    所以 `annots=False` 根本去不掉它，两次渲染的结果一模一样。要拿到「只有
    底图」的那一版，只能拿同一个文件**没画标注**的另一份文档来渲染，再逐像素
    比对：两版不一样的地方就是标注，保留彩色；一样的地方按亮度去色。

    好处：标注的颜色、半透明填充、编号、抗锯齿全都是 PyMuPDF 亲手画的那一份，
    和导出 PDF 里看到的完全一致，不可能走样。

    只作用在**渲染**这一层：`locate_marks()` 的判定一个字节都不经过这里。
    """
    import numpy as np

    pm_c = page.get_pixmap(dpi=dpi, annots=True)          # 底图 + 标注
    if plain_page is None:
        # 拿不到干净底图就整页去色：标注也会变灰，但至少能看、且绝不崩
        return page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, annots=True)
    pm_p = plain_page.get_pixmap(dpi=dpi, annots=True)    # 只有底图
    h, w = pm_c.height, pm_c.width
    if (pm_p.height, pm_p.width) != (h, w) or pm_p.n != pm_c.n:
        return pm_c                                       # 尺寸对不上就别冒险
    n = pm_c.n

    def _arr(pm):
        # 用 stride 切片：PyMuPDF 的每行可能有尾部填充，不能直接按 w*n 展开
        buf = np.frombuffer(pm.samples, dtype=np.uint8)
        return buf.reshape(pm.height, pm.stride)[:, :w * n].reshape(h, w, n)

    C = _arr(pm_c)
    P = _arr(pm_p)
    lum = _gray_luma(P).astype(np.uint8)
    out = np.repeat(lum[..., None], n, axis=2)
    if n == 4:
        out[..., 3] = P[..., 3]                           # alpha 原样带过来
    mask = np.abs(C[..., :3].astype(np.int16)
                  - P[..., :3].astype(np.int16)).max(axis=2) > 2
    out[mask] = C[mask]
    return fitz.Pixmap(fitz.csRGB, w, h, out.tobytes(), n == 4)


class App(TkinterDnD.Tk if _HAS_DND else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1240x860+80+60")
        self.configure(bg=BG)
        self.minsize(900, 560)

        self.old_var = tk.StringVar()
        self.new_var = tk.StringVar()
        # 文件框显示用变量：只显示文件名（真实路径存在 old_var/new_var）
        self.old_disp = tk.StringVar()
        self.new_disp = tk.StringVar()
        # 拖放区里两行槽位的显示文本（空着时给一句灰字引导，别留一片白）
        self.old_slot = tk.StringVar()
        self.new_slot = tk.StringVar()
        self.status_var = tk.StringVar(
            value="把 PDF / 图片 / Word / Excel 拖进上方那块（拖两个自动排位，早的＝旧版）"
                  "→ 点「开始对比」，结果直接显示在下方")
        self.busy = False
        self.q = queue.Queue()
        self.marks = None
        self._stopping = False          # 已点「停止对比」、等后台真正退栈
        self._t0 = 0.0                  # 本轮开始时间（停止后要报已耗时）
        # 「开跑前提示」开关（2026-10-21 刘剑要求）：默认打开。
        # 只在**纯图片类 / 扫描件 / 直接导入的 PNG-JPG** 上弹窗问一句，
        # 转曲、DWG 那种图文穿插都不问（见 core/pdfdiff_preflight 的判据）。
        self.warn_on = tk.BooleanVar(value=True)
        # 本轮「为什么可能很慢」的标注（纯图片类确认后写入，跑完/复位时清空）
        self._slow_note = ""
        # 分类开关：删减/新增/字体默认开，位移默认关（沿用 ann.KIND_DEFAULT_ON）
        self.kind_on = {k: tk.BooleanVar(value=(k in ann.KIND_DEFAULT_ON))
                        for k in ann.KIND_ORDER}
        self.page = 0                    # 当前页（0 基）
        self.dpi = 110                   # 当前渲染档
        self._auto_fit = True            # 打开/换文档后自动适应宽度
        self._reset_view = True          # 翻页后回顶部/左上角（缩放/勾选时不回）
        self._imgs: dict = {}            # side → PhotoImage（留引用防空白）
        self._cache: dict = {}           # (side, page, dpi) → PhotoImage
        self.docs: dict = {}             # side → 画好标注的内存文档
        self.docs_plain: dict = {}       # side → **没画标注**的同文件副本（底图灰度用）
        self._load_cfg()
        self._build_ui()
        self.after(120, self._poll)
        self.bind("<Configure>", self._on_resize)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- 记忆 ----------------
    def _load_cfg(self):
        self.export_dir = ""          # 导出目录单独记，不和文件选择混在一起
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.old_var.set(cfg.get("old", ""))
            self.new_var.set(cfg.get("new", ""))
            self.export_dir = cfg.get("export_dir", "")
            # 缺这个键时按「打开」处理：老配置文件里没有它，不能因此把提示关掉
            self.warn_on.set(bool(cfg.get("warn_image", True)))
        except Exception:
            pass
        self._sync_disp()

    def _save_cfg(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"old": self.old_var.get(), "new": self.new_var.get(),
                           "export_dir": getattr(self, "export_dir", ""),
                           "warn_image": bool(self.warn_on.get())},
                          f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _sync_disp(self):
        """文件框只显示**文件名**（完整路径太长看不清哪个是哪个版）。

        路径仍完整存在 old_var/new_var 里，显示与存储分离。
        拖放区两行槽位用的是同一份文件名；空着时给一句灰字（SLOT_EMPTY），
        否则整块拖放区一片空白，用户看不出「到底选没选上」。
        """
        for var, disp, slot in ((self.old_var, self.old_disp, self.old_slot),
                                (self.new_var, self.new_disp, self.new_slot)):
            p = var.get().strip()
            name = os.path.basename(p) if p else ""
            disp.set(name)
            slot.set(name or SLOT_EMPTY)

    # ---------------- 界面 ----------------
    def _build_ui(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT, font=(FONT, 11))
        st.configure("TButton", font=(FONT, 11), padding=(8, 4),
                     background=CARD, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD)
        st.map("TButton", background=[("active", "#f0f2f5"), ("pressed", "#e6e9ee")])
        st.configure("Small.TButton", font=(FONT, 10), padding=(6, 2),
                     background=CARD, bordercolor=BORDER,
                     lightcolor=CARD, darkcolor=CARD)
        st.map("Small.TButton", background=[("active", "#f0f2f5")])
        st.configure("Primary.TButton", font=(FONT, 11, "bold"), padding=(14, 5),
                     background=PRIMARY, foreground="#ffffff",
                     bordercolor=PRIMARY, lightcolor=PRIMARY, darkcolor=PRIMARY)
        st.map("Primary.TButton", background=[("active", PRIMARY_D), ("pressed", PRIMARY_D)])
        # 「停止对比」：白底 + 红字细框。不用整块红底 —— 那是「危险操作」的
        # 视觉重量，而这里只是「止损」，且平时是灰掉的。
        st.configure("Stop.TButton", font=(FONT, 10), padding=(8, 3),
                     background=CARD, foreground=STOP_FG,
                     bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD)
        st.map("Stop.TButton",
               background=[("active", "#fdf1ef"), ("pressed", "#f7e0dc"),
                           ("disabled", CARD)],
               foreground=[("disabled", "#c3c8ce")])
        # 进度条已删（2026-10-21），对应的 TProgressbar 样式一并去掉，
        # 免得以后有人看到样式就以为还有个进度条没接上。

        # ===== 顶部：选文件 + 操作 =====
        top = ttk.Frame(self)
        top.pack(fill=tk.X)

        bar = ttk.Frame(top, padding=(12, 6, 12, 0))
        bar.pack(fill=tk.X)
        tk.Label(bar, text=APP_TITLE, bg=BG, fg=TEXT,
                 font=(FONT, 12, "bold")).pack(side=tk.LEFT)
        tk.Label(bar, text="文字级 + 像素级混合　·　对比结果直接显示在下方",
                 bg=BG, fg=TEXT2, font=(FONT, 10)).pack(side=tk.LEFT, padx=(8, 0))

        # ===== 唯一一块拖放区（2026-10-20 刘剑二次要求：三行合成一块）=====
        # 原来有「合并框 + ① 选择文件（旧版行 / 新版行）」三层入口，实际用起来
        # 永远是「找到文件 → 拖进来」；两行手工指定既占地方，又让整块往上缩。
        # 现在只剩这一块：一次拖两个、或一份一份拖，都走**同一套**自动排位；
        # 右边三个按钮 = 浏览（可一次挑一到两个）/ 清空 / ⇅ 交换（排序反了的兜底）。
        # 拖动范围 = 整块（含图标、提示、两行文件名），全部注册成投放目标。
        dz = tk.Frame(top, bg=DZ_BG, highlightbackground=BORDER,
                      highlightthickness=1)
        dz.pack(fill=tk.X, padx=(12, 12), pady=(6, 0))
        self.drop_zone = dz
        dz.columnconfigure(2, weight=1)          # 文件名一列吃掉全部富余宽度

        self.lbl_icon = tk.Label(dz, text="⬇", bg=DZ_BG, fg=PRIMARY,
                                 font=(FONT, 15))
        self.lbl_icon.grid(row=0, column=0, sticky="w", padx=(12, 4),
                           pady=(6, 0))

        self.lbl_drop = tk.Label(dz, text=DROP_HINT, bg=DZ_BG, fg=TEXT2,
                                 font=(FONT, 10), anchor="w", justify="left")
        self.lbl_drop.grid(row=0, column=1, columnspan=2, sticky="w",
                           pady=(8, 2))

        # 两行槽位：左「旧版 / 新版」灰标签 + 右文件名。空着时是灰字引导。
        self.lbl_tag_old = tk.Label(dz, text="旧版", bg=DZ_BG, fg=TEXT2,
                                    font=(FONT, 10, "bold"), width=4, anchor="w")
        self.lbl_tag_old.grid(row=1, column=1, sticky="w", padx=(0, 2))
        self.lbl_file_old = tk.Label(dz, textvariable=self.old_slot, bg=DZ_BG,
                                     fg=TEXT, font=(FONT, 10), anchor="w")
        self.lbl_file_old.grid(row=1, column=2, sticky="ew", padx=(0, 8))
        self.lbl_tag_new = tk.Label(dz, text="新版", bg=DZ_BG, fg=TEXT2,
                                    font=(FONT, 10, "bold"), width=4, anchor="w")
        self.lbl_tag_new.grid(row=2, column=1, sticky="w", padx=(0, 2))
        self.lbl_file_new = tk.Label(dz, textvariable=self.new_slot, bg=DZ_BG,
                                     fg=TEXT, font=(FONT, 10), anchor="w")
        self.lbl_file_new.grid(row=2, column=2, sticky="ew", padx=(0, 8),
                               pady=(0, 8))

        btns = tk.Frame(dz, bg=DZ_BG)
        btns.grid(row=0, column=3, rowspan=3, sticky="e", padx=(4, 10), pady=6)
        ttk.Button(btns, text="浏览…", style="Small.TButton",
                   command=self._pick).pack(side=tk.LEFT)
        ttk.Button(btns, text="清空", style="Small.TButton",
                   command=self._clear_pair).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(btns, text="⇅ 交换", style="Small.TButton",
                   command=self._swap).pack(side=tk.LEFT, padx=(5, 0))
        self._btns_dz = btns

        # 拖放目标：整块 + 块内每一个可见件（ttk.Button 在部分 Tk 版本上不收，
        # 逐个 try 掉即可）。用户从哪条缝拖进来都算数。
        self._dz_targets = [dz, self.lbl_icon, self.lbl_drop,
                            self.lbl_tag_old, self.lbl_file_old,
                            self.lbl_tag_new, self.lbl_file_new, btns]
        if _HAS_DND:
            for _w in self._dz_targets:
                try:
                    _w.drop_target_register(DND_FILES)
                    _w.dnd_bind("<<Drop>>", self._on_drop_many)
                    _w.dnd_bind("<<DropEnter>>", self._drop_enter)
                    _w.dnd_bind("<<DropLeave>>", self._drop_leave)
                except tk.TclError:
                    pass

        f3 = ttk.Frame(top, padding=(12, 8, 12, 2))
        f3.pack(fill=tk.X)
        self.btn_run = ttk.Button(f3, text="开始对比", style="Primary.TButton",
                                  command=self._run)
        self.btn_run.pack(side=tk.LEFT)
        # 「停止对比」（2026-10-20 刘剑要求）：大面积偏移/多页文件一跑就是几分钟，
        # 选错文件时得能当场停下，不必退出软件。紧贴在「开始对比」右边。
        self.btn_stop = ttk.Button(f3, text=STOP_IDLE, style="Stop.TButton",
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(f3, text="导出 JSON", style="Small.TButton",
                   command=self._export).pack(side=tk.LEFT, padx=(5, 0))
        # 「导出 JSON」右边原来是进度条，2026-10-21 刘剑要求删掉：
        #   prog 回调的 total 是**opcodes 块数**（不是页数、也不是区域数），
        #   一格一格跳得很不均匀，看它还不如看状态栏那个百分比。
        # 位置腾给「底图灰度」——默认打开，只作用于**渲染**，不碰判定：
        #   彩色原件配上红/绿/浅蓝标注会很花，底图一灰，标注就跳出来了。
        self.gray_on = tk.BooleanVar(value=True)
        self.chk_gray = _mk_check(f3, "底图灰度", self.gray_on,
                                  self._on_gray_toggle)
        self.chk_gray.pack(side=tk.LEFT, padx=(14, 0))
        # 「开跑前提示」（2026-10-21 刘剑要求）：默认打开。
        # 命中纯图片/扫描件/PNG-JPG 时，开跑前弹窗问一句「要不要等」；
        # 转曲、DWG 图文穿插都不问 —— 判据在 core/pdfdiff_preflight。
        self.chk_warn = _mk_check(f3, "开跑前提示", self.warn_on,
                                  self._on_warn_toggle)
        self.chk_warn.pack(side=tk.LEFT, padx=(14, 0))
        tk.Label(f3, text="红=删减（旧版）　绿=新增（新版）　整页重绘画边框",
                 bg=BG, fg="#9aa1a9", font=(FONT, 9)).pack(side=tk.RIGHT)

        # ===== 中部：预览工具条 =====
        self.toolbar = ttk.Frame(self, padding=(12, 2, 12, 2))
        self.toolbar.pack(fill=tk.X)
        self.btn_prev = ttk.Button(self.toolbar, text="◀", width=3,
                                   style="Small.TButton", command=self._prev_page,
                                   state="disabled")
        self.btn_prev.pack(side=tk.LEFT)
        # ⚠️ 宽度交给文字自适应（原先是 width=7 固定字符宽）。两侧页数不同时
        # 这里要追加「（旧 1 页 · 新 2 页）」，固定 7 字符会把后半句**切掉**，
        # 只剩「1 / 2（旧 1 页 · 新」这种半截话（2026-10-20 截图核对时发现）。
        self.lbl_page = tk.Label(self.toolbar, text="– / –", bg=BG, fg=TEXT,
                                 font=(FONT, 10))
        self.lbl_page.pack(side=tk.LEFT)
        self.btn_next = ttk.Button(self.toolbar, text="▶", width=3,
                                   style="Small.TButton", command=self._next_page,
                                   state="disabled")
        self.btn_next.pack(side=tk.LEFT)
        self._zoom_out = ttk.Button(self.toolbar, text="−", width=3,
                                    style="Small.TButton",
                                    command=lambda: self._zoom(-1),
                                    state="disabled")
        self._zoom_out.pack(side=tk.LEFT, padx=(14, 0))
        self.lbl_dpi = tk.Label(self.toolbar, text="110 dpi", bg=BG, fg=TEXT2,
                                font=(FONT, 10), width=8)
        self.lbl_dpi.pack(side=tk.LEFT)
        self._zoom_in = ttk.Button(self.toolbar, text="+", width=3,
                                   style="Small.TButton",
                                   command=lambda: self._zoom(1), state="disabled")
        self._zoom_in.pack(side=tk.LEFT)
        self._btn_fit = ttk.Button(self.toolbar, text="适应宽度",
                                   style="Small.TButton",
                                   command=self._fit_width, state="disabled")
        self._btn_fit.pack(side=tk.LEFT, padx=(10, 0))

        # 分类勾选开关（颜色图例）：删减红 / 新增绿 / 位移浅蓝 / 字体深蓝
        # 默认勾删减+新增+字体，位移默认不勾（KIND_DEFAULT_ON）。
        tk.Frame(self.toolbar, bg=BORDER, width=1).pack(
            side=tk.LEFT, fill=tk.Y, padx=10, pady=2)
        for _k in ann.KIND_ORDER:
            tk.Label(self.toolbar, text="●", bg=BG, fg=self._rgb_hex(ann.COLOR_OF[_k]),
                     font=(FONT, 10)).pack(side=tk.LEFT, padx=(7, 0))
            _mk_check(self.toolbar, ann.KIND_TEXT[_k], self.kind_on[_k],
                      self._on_kind_toggle).pack(side=tk.LEFT, padx=(1, 0))

        # ⚠️ 2026-10-19 栏标题（旧版/新版 · 分桶计数）**不再放工具栏**：
        #    顶部和底部状态栏各显一份完全相同的统计，属于重复；且底部那句
        #    「旧版标红 N 处」直接用的 `stats["marked"]`，把位移（浅蓝）也算进
        #    「标红」，用户看到「标红 41」而画面红框只有 3 个（41 = 删减 3 + 位移 38）。
        #    现改为：统计只在**底部状态栏**出现一次，按当时勾选的桶分列，
        #    与画面严格同源（`_update_counts`），顶部腾出空间。

        # ===== 下部：并排标注区（主体） =====
        body = ttk.Frame(self)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self.canv = {}
        self.vsb = {}
        self.hsb = {}
        self._syncing = False          # 联动滚动防回环
        self._drag = None              # 鼠标拖拽平移状态
        self._last_zoom = 0.0          # 滚轮缩放节流时间戳（触控板会连发事件）
        for side, col in (("old", 0), ("new", 1)):
            holder = tk.Frame(body, bg=CARD,
                              highlightbackground=BORDER, highlightthickness=1)
            holder.grid(row=0, column=col, sticky="nsew",
                        padx=(6 if col else 2, 6))
            cv = tk.Canvas(holder, bg="#eef0f3", highlightthickness=0, takefocus=1)
            vsb = ttk.Scrollbar(holder, orient=tk.VERTICAL,
                                command=lambda *a, s=side: self._scroll(s, *a))
            hsb = ttk.Scrollbar(holder, orient=tk.HORIZONTAL,
                                command=lambda *a, s=side: self._hscroll(s, *a))
            cv.configure(yscrollcommand=lambda *a, s=side: self._from_bar(s, *a),
                         xscrollcommand=lambda *a, s=side: self._from_hbar(s, *a))
            vsb.pack(side=tk.RIGHT, fill=tk.Y)
            hsb.pack(side=tk.BOTTOM, fill=tk.X)
            cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            self.canv[side] = cv
            self.vsb[side] = vsb
            self.hsb[side] = hsb
            # 滚轮 = 缩放，锚定鼠标位置（滚到哪就放大到哪）。
            # 「鼠标中键上下翻页」已取消：纵向滚动改由 Shift+滚轮 / 触控板双指 承担，
            # 平移直接用左键拖动（见下面 B1-Motion）。
            cv.bind("<MouseWheel>", lambda e, s=side: self._wheel_zoom(s, e))
            cv.bind("<Button-4>", lambda e, s=side: self._wheel_zoom(s, e))
            cv.bind("<Button-5>", lambda e, s=side: self._wheel_zoom(s, e))
            # Shift+滚轮：纵向滚动（两栏同步）
            cv.bind("<Shift-MouseWheel>", lambda e, s=side: self._wheel(s, e))
            cv.bind("<Shift-Button-4>", lambda e, s=side: self._wheel(s, e))
            cv.bind("<Shift-Button-5>", lambda e, s=side: self._wheel(s, e))
            # Alt+滚轮 / 鼠标左右滚轮：横向滚动
            cv.bind("<Alt-MouseWheel>", lambda e, s=side: self._wheel_h(s, e))
            cv.bind("<Button-6>", lambda e, s=side: self._wheel_h(s, e))
            cv.bind("<Button-7>", lambda e, s=side: self._wheel_h(s, e))
            # 鼠标按住左键拖动 = 平移（放大后比滚动条快）
            cv.bind("<ButtonPress-1>", self._drag_start)
            cv.bind("<B1-Motion>", self._drag_move)
            cv.bind("<ButtonRelease-1>", lambda *_a: setattr(self, "_drag", None))

        # 状态栏：**左**＝进度 / 耗时 / 提示，**右**＝两侧分桶统计。
        # 统计只在这里出现一次（顶部栏标题已撤），且与画面同源：`_update_counts`
        # 按当前勾选的桶统计，勾掉「位移」后数字立刻跟着画面变。
        sb = tk.Frame(self, bg="#eceef1", highlightbackground=BORDER,
                      highlightthickness=0)
        sb.pack(fill=tk.X, side=tk.BOTTOM)
        sb.columnconfigure(0, weight=1)          # 左侧提示可伸缩，右侧统计贴右
        tk.Label(sb, textvariable=self.status_var, bg="#eceef1", fg=TEXT2,
                 font=(FONT, 10), anchor="w").grid(
            row=0, column=0, sticky="ew", padx=(12, 8), pady=4)
        self.lbl_old = tk.Label(sb, text="旧版 PDF", bg="#eceef1", fg=TEXT,
                                font=(FONT, 10), anchor="e")
        self.lbl_old.grid(row=0, column=1, sticky="e", padx=(8, 20), pady=4)
        self.lbl_new = tk.Label(sb, text="新版 PDF", bg="#eceef1", fg=TEXT,
                                font=(FONT, 10), anchor="e")
        self.lbl_new.grid(row=0, column=2, sticky="e", padx=(0, 16), pady=4)

    # ---------------- 拖放（唯一入口） ----------------
    def _drop_paths(self, data):
        """把 tkinterdnd2 的 `event.data` 解析成真实存在的文件路径列表。

        格式坑（macOS）：多个文件是 Tcl 列表，路径带空格时会被花括号包起来，
        `{/a b/x.pdf} {/c/y.pdf}`；单个文件也可能带花括号。先按整串判断
        是不是一个存在的文件（避免把带空格的单路径拆成两半），再退到
        `splitlist`（Tcl 列表解析，天然认花括号）。
        """
        raw = (data or "").strip()
        if not raw:
            return []
        single = raw.strip("{}").strip()
        if os.path.isfile(single):
            return [os.path.normpath(single)]
        try:
            parts = list(self.tk.splitlist(raw))
        except Exception:
            parts = [raw]
        out = []
        for p in parts:
            p = str(p).strip().strip("{}").strip()
            if p and os.path.isfile(p):
                out.append(os.path.normpath(p))
        return out

    def _dz_paint(self, active: bool):
        """拖进来时把整块点亮（不然用户不知道拖没拖对地方）。

        整块一起变色：这块现在既是投放区、也是「当前选了哪两个文件」的显示区，
        只亮提示那一行会让人以为下半截不能拖。
        """
        bg = DZ_BG_ON if active else DZ_BG
        bd = DZ_BORDER_ON if active else BORDER
        for w in self._dz_targets:
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass
        try:
            self.drop_zone.configure(highlightbackground=bd, highlightcolor=bd)
        except tk.TclError:
            pass

    def _drop_enter(self, event=None):
        self._dz_paint(True)

    def _drop_leave(self, event=None):
        self._dz_paint(False)

    def _on_drop_many(self, event, side=None):
        """拖放入口。`side` 只为兼容旧调用保留，界面已不再按侧投放。"""
        paths = self._drop_paths(getattr(event, "data", ""))
        self._dz_paint(False)
        self._assign_paths(paths, side)

    def _assign_paths(self, paths, side=None):
        """把一到多个文件分配进旧版/新版（**界面唯一入口**：拖放与「浏览…」共用）。

        规则（2026-10-20 刘剑定的操作习惯）：
        · 两个以上：按文件名自然序，早的＝旧版、晚的＝新版；多余的不收，状态栏说明。
        · 一个 + 两侧都空：先占**旧版**（第一份默认是旧的，第二份进来再排）。
        · 一个 + 只填了一侧：与已有那份一起**重新按文件名排位**（不看拖的时间先后）。
        · 一个 + 两侧都满：默认**挤掉旧版** —— 留下「现有的新版 + 刚拖进来的」，
          再按文件名排位。第三份进来的常见场景就是「上一版我拖错了/这是更新的一版」，
          所以淘汰的永远是旧的那头，而不是随机顶掉一侧。
        · `side`（"old"/"new"）：仅测试与旧调用用，显式指定某一侧时不排队、只替换那侧。
        """
        if not paths:
            self.status_var.set("拖进来的不是文件（只认 PDF / 图片 / Word / Excel 的实际路径）")
            return
        cur_o = self.old_var.get().strip()
        cur_n = self.new_var.get().strip()
        if len(paths) >= 2:
            old, new = _pair_from_paths(paths)
            note = "已按文件名排序分配（早的＝旧版）"
            if len(paths) > 2:
                note += "；一次只收两个，已取排序前两个"
        else:
            p = paths[0]
            if side in ("old", "new"):
                old, new = (p, cur_n) if side == "old" else (cur_o, p)
                note = "已放到「%s」" % ("旧版" if side == "old" else "新版")
            elif not cur_o and not cur_n:
                old, new = p, ""
                note = "已放到「旧版」（再拖一份进来就自动排位）"
            elif not cur_o:
                old, new = _pair_from_paths([cur_n, p])
                note = "已按文件名排位"
            elif not cur_n:
                old, new = _pair_from_paths([cur_o, p])
                note = "已按文件名排位"
            elif p in (cur_o, cur_n):
                old, new = cur_o, cur_n
                note = "这份已经在列表里了，未改动"
            else:
                # 挤掉旧版：{现有的新版, 刚拖进来的} 重新排位
                old, new = _pair_from_paths([cur_n, p])
                note = "已挤掉旧版（留「%s」+ 这份，重新排位）" % (
                    os.path.basename(cur_n) or "—")
        self.old_var.set(old)
        self.new_var.set(new)
        self._sync_disp()
        self._save_cfg()
        self.status_var.set("%s　旧版：%s　｜　新版：%s" % (
            note, os.path.basename(old) or "—", os.path.basename(new) or "—"))

    def _on_drop(self, event, var):
        """兼容旧调用：按变量判断是哪一侧。"""
        self._on_drop_many(event, "old" if var is self.old_var else "new")

    def _pick(self):
        """「浏览…」：一次挑**一到两个**，走与拖放**完全同一套**分配规则。

        （2026-10-21 刘剑要求）原来这里用的是单选对话框：拖放能一次给两个，
        浏览却只能一个一个来，两套入口行为不一致。改成多选后，挑一份与挑两份
        都行 —— 挑一份按「填空 / 重排」规则走，挑两份按文件名排序，与拖放逐字相同。
        """
        ps = filedialog.askopenfilenames(
            title="选择一或两个文件（旧版 + 新版，会自动排位）",
            initialdir=(os.path.dirname(self.old_var.get())
                        or os.path.dirname(self.new_var.get()) or None),
            filetypes=[("PDF / 图片 / Word / Excel",
                        "*.pdf *.png *.jpg *.jpeg *.bmp *.tif *.tiff"
                        " *.doc *.docx *.docm *.rtf *.xls *.xlsx *.xlsm"),
                       ("所有文件", "*.*")])
        if not ps:
            return
        # tkinter 正常返回 tuple；个别 Tk 版本把 Tcl 列表整串吐回来，
        # 这里统一过一次 splitlist，避免带空格的路径被劈成两段。
        if isinstance(ps, str):
            ps = self.tk.splitlist(ps)
        paths = [os.path.normpath(p) for p in ps if str(p).strip()]
        if paths:
            self._assign_paths(paths)

    def _clear_pair(self):
        """「清空」：两侧一起清掉，回到「拖进来就能用」的状态。

        对比进行中不清 —— 后台线程手里还攥着这对路径，清了只会让状态栏和
        实际在跑的东西对不上；先点「停止对比」再来。
        """
        if self.busy:
            self.status_var.set("正在对比，先点「停止对比」再清空")
            return
        self.old_var.set("")
        self.new_var.set("")
        self._sync_disp()
        self._save_cfg()
        extra = "　（下方仍是上一次的对比结果）" if self.marks is not None else ""
        self.status_var.set("已清空选择　·　把文件拖进来或点「浏览…」重新选" + extra)

    def _swap(self):
        a, b = self.old_var.get(), self.new_var.get()
        self.old_var.set(b)
        self.new_var.set(a)
        self._sync_disp()
        self._save_cfg()
        if a or b:
            self.status_var.set("已交换：旧版 ⇄ 新版")

    # ---------------- 开跑前的「纯图片类」提醒 ----------------
    def _gate_image_only(self, a, b):
        """纯图片/扫描件/PNG-JPG：开跑前先问一句，返回 False = 用户取消。

        2026-10-21 刘剑要求：这类文件走得通，但走的是 OCR —— 单页实测 100~280 秒，
        结论只能当提示。所以先摆明「慢 + 要人工核」，让用户决定要不要等。

        ⚠️ 只问纯图片类。转曲类（矢量、即使没有文字层）与 DWG 那种图文穿插
           **不问** —— 刘剑原话「转曲的不用在意，它并非每次都出现、也不是百分百
           全部都是转曲；像 DWG 中间穿插图片的也不需要提示」。判据见
           `core/pdfdiff_preflight`：文字层 → 位图 → 矢量，三级分开。
        """
        if not self.warn_on.get():
            self._slow_note = ""
            return True
        try:
            rep = pre.assess(a, b)
        except Exception:
            self._slow_note = ""
            return True                    # 体检自己出错不该挡住用户，交给引擎去跑
        if rep.get("level") != "warn":
            self._slow_note = ""
            return True
        self.status_var.set("开跑前检查：这对文件是纯图片/扫描件，等你确认…")
        self.update_idletasks()
        if self._confirm_slow(rep):
            # 这句话要**留在运行期间**：进度条一动就没人记得「为什么这么慢」了，
            # 所以存成 _slow_note，由 _run / _poll 拼进状态栏（见下）。
            self._slow_note = "纯图片类·走 OCR·%d~%d 分钟" % (
                max(1, rep["seconds"][0] // 60), max(1, rep["seconds"][1] // 60))
            self.status_var.set("已确认：" + self._slow_note + "　·　可随时点「停止对比」")
            return True
        self._slow_note = ""
        self.status_var.set("已取消（没有开始对比）　·　换个有文字层的 PDF 通常几秒就出结果")
        return False

    def _confirm_slow(self, rep):
        """模态确认框。返回 True = 仍然对比。

        自己搭 Toplevel 而不用 messagebox：要摆「两侧各一行说明 + 两段提示 + 两个按钮」，
        messagebox 摆不了这么多东西。

        ⚠️ 这里**故意不提供「以后不再提示」的勾选**（2026-10-21 刘剑定）：
           这类文件不是每次都有，点一下确认的时间比起后面几分钟的等待可以忽略，
           但一旦给了复选框，人就会习惯性勾掉，之后所有提示都形同虚设。
           要长期关掉请用上方那个「开跑前提示」开关 —— 那是个显眼的、需要主动找的开关，
           不会在每次弹窗里被顺手勾掉。
        """
        dlg = tk.Toplevel(self)
        dlg.withdraw()
        dlg.title(APP_TITLE)
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.configure(bg=BG)
        state = {"ok": False}

        card = tk.Frame(dlg, bg=BG, padx=20, pady=16)
        card.pack(fill=tk.BOTH, expand=True)

        # 标题：用琥珀色而不是红 —— 这不是错误，是「先提醒再继续」
        tk.Label(card, text="⚠  " + rep["title"], bg=BG, fg="#b26a00",
                 font=(FONT, 13, "bold"), anchor="w").pack(fill=tk.X)
        for ln in rep["lines"]:
            tk.Label(card, text=ln, bg=BG, fg=TEXT, font=(FONT, 10),
                     anchor="w", justify="left").pack(fill=tk.X, pady=(7, 0))
        tk.Frame(card, bg=BORDER, height=1).pack(fill=tk.X, pady=(12, 10))

        head, tail = pre.body_text(rep)
        for para in (head, tail):
            tk.Label(card, text=para, bg=BG, fg=TEXT2, font=(FONT, 10),
                     anchor="w", justify="left",
                     wraplength=470).pack(fill=tk.X, pady=(0, 7))

        bar = tk.Frame(card, bg=BG)
        bar.pack(fill=tk.X, pady=(10, 0))
        ttk.Label(bar, text="这次要不要等，请手动选一下").pack(side=tk.LEFT)

        def done(ok):
            state["ok"] = bool(ok)
            try:
                dlg.grab_release()
            except tk.TclError:
                pass
            dlg.destroy()

        # 按钮按 macOS 习惯：左「取消」、右「仍然对比」（主操作在最右）。
        # ⚠️ 两个按钮都**不抢焦点**，回车/空格不会替用户做决定。
        ttk.Button(bar, text="仍然对比", style="Primary.TButton",
                   command=lambda: done(True)).pack(side=tk.RIGHT)
        ttk.Button(bar, text="取消", style="Small.TButton",
                   command=lambda: done(False)).pack(side=tk.RIGHT, padx=(0, 8))
        dlg.bind("<Escape>", lambda _e: done(False))
        dlg.protocol("WM_DELETE_WINDOW", lambda: done(False))

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 3)
        dlg.geometry("+%d+%d" % (x, y))
        dlg.deiconify()
        # ⚠️ **不给按钮设焦点**：设了之后空格/回车可能把焦点按钮直接按掉，
        #    模态框会在用户还没看清时就自己关掉（测试里复现过）。
        #    焦点留在对话框本身，必须用鼠标（或 Tab 到按钮后）主动选。
        dlg.focus_set()
        dlg.grab_set()
        self.wait_window(dlg)
        return state["ok"]

    # ---------------- 对比 ----------------
    def _run(self):
        if self.busy:
            return
        a, b = self.old_var.get().strip(), self.new_var.get().strip()
        if not a or not b:
            messagebox.showwarning(APP_TITLE, "请先选择旧版和新版（PDF / 图片 / Word / Excel）")
            return
        if not os.path.isfile(a) or not os.path.isfile(b):
            messagebox.showerror(APP_TITLE, "文件路径无效，请重新选择")
            return
        # 开跑前体检：纯图片/扫描件/PNG-JPG 先问一句「要不要等」（转曲、DWG 不问）
        if not self._gate_image_only(a, b):
            return
        # Word / Excel 输入：转换发生在后台比对线程里，提前在状态栏说一声
        # （转出来的 PDF 带完整文字层，走逐字比对，通常几秒内完成）。
        try:
            import pdfdiff_office as _off
            if _off.is_office_file(a) or _off.is_office_file(b):
                _note = "Word/Excel 转 PDF 后比对"
                self._slow_note = (self._slow_note + "　·　" + _note
                                   if self._slow_note else _note)
        except Exception:
            pass
        # ⚠️ 必须在这里清标志，且必须在 `busy` 早退之后清：
        #    上一轮点过停止的话标志还立着，不清就会把新一轮立刻打死；
        #    而如果放在 busy 检查之前，用户在「停止中…」里误点「开始对比」
        #    会把还没退栈的线程的取消请求抹掉 → 停不下来。
        loc.clear_cancel()
        self._stopping = False
        self.busy = True
        self._t0 = time.time()
        self.btn_run.config(state="disabled", text="对比中…")
        self.btn_stop.config(state="normal", text=STOP_IDLE)
        self.status_var.set(
            "正在比对…" + ("　·　" + self._slow_note if self._slow_note else "")
            + "（选错了可随时点「停止对比」）")

        def work():
            try:
                # 老引擎（字符级 + 转曲区像素兜底）：识别质量久经考验，比纯像素强
                marks = ann.locate_marks(
                    a, b, prog=lambda i, t: self.q.put(("prog", i, t)))
                marks["elapsed"] = time.time() - self._t0
                self.q.put(("done", marks))
            except loc.Cancelled:
                # 用户主动中止：不是错误，单独走一条消息（不弹错误框）
                self.q.put(("cancelled",))
            except Exception as e:
                # ⚠️ 只把 str(e) 甩到界面上是不够的（2026-10-22 Windows 上一条
                #    「expected str, bytes or os.PathLike object, not NoneType」
                #    花了半天猜是哪一行）。异常一律连**完整栈**写进临时目录日志，
                #    界面文案尾部带上路径 —— 用户把那一行发过来就能定位，
                #    不用再靠复述和猜。
                self.q.put(("err", _fmt_error(e)))

        threading.Thread(target=work, daemon=True).start()

    def _stop(self):
        """停止当前比对。

        只做一件事：置取消标志。真正的退栈由引擎在下一个检查点完成
        （OCR 前后 / 区域之间 / 页之间），所以这里是**异步**的 —— 按钮先变
        「停止中…」，等后台线程退了才恢复可用。绝不 join 等待，否则界面卡死。
        """
        if not self.busy or self._stopping:
            return
        self._stopping = True
        loc.request_cancel()
        self.btn_stop.config(state="disabled", text=STOP_GOING)
        self.status_var.set("正在停止…（等当前这一小块识别收尾，通常 1~2 秒）")

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "prog":
                    i, total = msg[1], msg[2]
                    if total:
                        # ⚠️ prog 的 total 是**进度总量**，两处语义不同：
                        #   页级循环传页数（pdfdiff_locate.py:1132），
                        #   字符级 diff 传的是 opcodes 块数（:1822）。
                        #   两者混在同一个回调里，所以这里**绝不能写「第 X 页」**——
                        #   1 页文档会被显示成「正在比对第 25 页」（用户 2026-10-19 报的
                        #   「导入一页 PDF 却显示第 25 页」正是此因）。
                        pct = int(100 * i / max(1, total))
                        if not self._stopping:
                            _sn = ("　·　" + self._slow_note
                                   if self._slow_note else "")
                            self.status_var.set(f"正在比对… {pct}%{_sn}")
                    elif not self._stopping:
                        self.status_var.set("正在比对…")
                elif msg[0] == "done":
                    # 点了停止、而后台恰好已经算完（窗口只有 120ms 级）：
                    # 按用户意志**丢弃**这次结果，不覆盖画面 —— 否则刚点完停止
                    # 屏幕就刷出一份「选错的那对文件」的结果，比不刷新更误导。
                    if self._stopping:
                        self._on_cancelled()
                    else:
                        self._on_done(msg[1])
                elif msg[0] == "cancelled":
                    self._on_cancelled()
                elif msg[0] == "err":
                    self.busy = False
                    self._stopping = False
                    self.btn_run.config(state="normal", text="开始对比")
                    self.btn_stop.config(state="disabled", text=STOP_IDLE)
                    self.status_var.set(f"失败：{msg[1]}")
                    messagebox.showerror(APP_TITLE, f"失败：\n{msg[1]}")
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _on_cancelled(self):
        """已停止：恢复按钮，**保留上一次的结果画面**，不重绘不写统计。

        为什么不重绘：用户点停止是因为「选错文件了」，此时画面上的东西
        应该是他上回看的那份（或空），而不是半份新结果。
        """
        el = time.time() - self._t0
        self.busy = False
        self._stopping = False
        self.btn_run.config(state="normal", text="开始对比")
        self.btn_stop.config(state="disabled", text=STOP_IDLE)
        if self.marks is None:
            self.status_var.set(f"已停止　·　耗时 {el:.1f}s　·　未产生结果")
        else:
            self.status_var.set(
                f"已停止　·　耗时 {el:.1f}s　·　本次结果已丢弃，画面仍是上一次的结果")

    def _on_done(self, marks):
        self.busy = False
        self._stopping = False
        self.marks = marks
        self.btn_run.config(state="normal", text="开始对比")
        self.btn_stop.config(state="disabled", text=STOP_IDLE)
        self.btn_prev.config(state="normal")
        self.btn_next.config(state="normal")
        for w in (self._zoom_out, self._zoom_in, self._btn_fit):
            w.config(state="normal")
        # 统计不再写进状态文字（右侧两个 lbl 已按桶列出，且与画面同源）。
        # 旧文案「旧版标红 {marked} 处」是错的：`stats["marked"]` 含**位移（浅蓝）**，
        # 41 = 删减 3 + 位移 38，画面红框却只有 3 个 —— 数字和画面对不上。
        self._update_counts()
        _sn = ("　·　" + self._slow_note) if self._slow_note else ""
        self.status_var.set(f"完成　·　耗时 {marks.get('elapsed', 0):.1f}s{_sn}")
        self._slow_note = ""
        # 重开标注文档 + 回到第 1 页 + 自适应宽度
        self.page = 0
        self._auto_fit = True
        self._reset_view = True
        self._add_annots()
        self.render()

    # ---------------- 标注文档 ----------------
    def _rgb_hex(self, rgb) -> str:
        """把 (0..1, 0..1, 0..1) 的浮点颜色转成 Tk 用的 #RRGGBB。"""
        r, g, b = (int(round(max(0.0, min(1.0, c)) * 255)) for c in rgb)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _enabled_kinds(self) -> tuple:
        """当前勾开的颜色桶（按 KIND_ORDER 钉序）。"""
        return tuple(k for k in ann.KIND_ORDER if self.kind_on[k].get())

    def _update_counts(self):
        """状态栏右侧两侧统计，按**当前勾选的桶**统计（与画面严格同源）。

        ⚠️ 两条历史教训，都别再犯：
        1. 不能用 `stats["marked"]`：它含**所有桶**（删减+新增+位移）和像素框，
           勾掉位移后画面只有 3 个红框、数字却写 41（= 删减 3 + 位移 38）。
        2. 状态行文案不能再写成「标红 N 处」：那等于把浅蓝的位移算成红框，
           用户一眼就发现数字和画面对不上（2026-10-19 GRA-4200 实测）。
        现在每桶分列（「删减 3　位移 38」），谁是谁一目了然。
        """
        if not self.marks:
            return
        en = set(self._enabled_kinds())
        for side, lbl in (("old", self.lbl_old), ("new", self.lbl_new)):
            bk = (self.marks.get(side) or {}).get("by_kind") or {}
            bits = [f"{ann.KIND_TEXT[k]} {sum(len(v) for v in bk[k].values())}"
                    for k in ann.KIND_ORDER if k in en and bk.get(k)]
            name = "旧版 PDF" if side == "old" else "新版 PDF"
            lbl.config(text=name + ("　·　" + "　".join(bits) if bits else "　·　无标注"))

    def _on_kind_toggle(self):
        """分类开关切换：注释要**重画**（内存文档里的旧注释摘不掉，整个重开）。"""
        if not self.marks:
            return
        self._reset_view = False
        self._add_annots()
        self._update_counts()
        self.render()

    def _add_annots(self):
        """把标记画进内存里的文档对象（原文件一个字节不动）。

        每次新对比 / 切换勾选都重开干净文档 —— PDF 注释画上就摘不掉，只能重开重画。
        过滤走 `ann.filter_kinds`：没勾的桶（如默认关的位移）不画。
        """
        enabled = self._enabled_kinds()
        for side, fallback in (("old", ann.RED), ("new", ann.GREEN)):
            old = self.docs.pop(side, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            path = (self.marks.get(side) or {}).get("path")
            if not path or not os.path.isfile(path):
                continue
            try:
                doc = fitz.open(path)
                ann.paint_into(doc,
                               ann.filter_kinds(self.marks.get(side), enabled),
                               fallback)
                self.docs[side] = doc
                # 「底图灰度」要拿它做差分：标注是 draw_rect 写进内容流的，
                # 只有另开一份**没画过**的同文件，才知道哪些像素是标注。
                plain = self.docs_plain.pop(side, None)
                if plain is not None:
                    try:
                        plain.close()
                    except Exception:
                        pass
                self.docs_plain[side] = fitz.open(path)
            except Exception:
                pass
        self._cache.clear()
        self._imgs.clear()

    # ---------------- 渲染 ----------------
    def render(self):
        """按当前页 / 当前缩放重画两侧。只读渲染，不写盘。"""
        if not self.marks:
            return
        self.configure(cursor="watch")
        try:
            self.update_idletasks()
            if self._auto_fit:
                self.dpi = self._fit_dpi()
                self._auto_fit = False
            self.lbl_dpi.config(text=f"{self.dpi} dpi")
            for side in ("old", "new"):
                self._render_side(side)
            n = max(self.marks["old"].get("pages", 0),
                    self.marks["new"].get("pages", 0))
            no_, nn_ = self.marks["old"].get("pages", 0), self.marks["new"].get("pages", 0)
            _pg = f"{self.page + 1} / {n}"
            if no_ and nn_ and no_ != nn_:
                # 两侧页数不同时说明是**跟哪一侧**在翻页 —— 否则用户只看得到
                # 大的那个数字（如「1 / 25」），会以为自己导入的文件有 25 页。
                _pg += f"　（旧 {no_} 页 · 新 {nn_} 页）"
            self.lbl_page.config(text=_pg)
            self.btn_prev.config(state="normal" if self.page > 0 else "disabled")
            self.btn_next.config(state="normal" if self.page < n - 1 else "disabled")
            self._sync_scrollbars()
        finally:
            self.configure(cursor="")

    def _fit_dpi(self) -> int:
        """按画布宽度算「适应宽度」的 dpi（两张页并排，各占一半窗宽）。"""
        cv = self.canv["old"]
        w = cv.winfo_width()
        if w < 80:                       # 还没布局好，别照 1px 算
            return 110
        pw = 595.0
        try:
            doc = self.docs.get("old")
            if doc is not None and self.page < doc.page_count:
                pw = max(200.0, float(doc[self.page].rect.width))
        except Exception:
            pass
        dpi = int(w / pw * 72.0)
        return max(DPI_MIN, min(DPI_MAX, dpi))

    def _render_side(self, side: str):
        cv = self.canv[side]
        cv.delete("all")
        doc = self.docs.get(side)
        if doc is None or self.page >= doc.page_count:
            cv.configure(scrollregion=(0, 0, 10, 10))
            cv.create_text(20, 20, anchor="nw", fill="#6b7280", font=(FONT, 11),
                           text="（此侧没有这一页）" if self.marks else "选好文件点「开始对比」")
            return
        img = self._cached_pixmap(side, doc[self.page])
        self._imgs[side] = img           # ⚠️ 不留引用 = 画布空白
        cv.create_image(0, 0, anchor="nw", image=img)
        cv.configure(scrollregion=(0, 0, img.width(), img.height()))
        # 翻页才回顶部（缩放/勾选切换保持当前视角，见 _zoom/_on_kind_toggle）
        if self._reset_view:
            cv.yview_moveto(0)
            cv.xview_moveto(0)

    def _cached_pixmap(self, side: str, page) -> tk.PhotoImage:
        gray = bool(self.gray_on.get())
        key = (side, self.page, self.dpi, gray)
        img = self._cache.get(key)
        if img is not None:
            return img
        if gray:
            pp = None
            pd = self.docs_plain.get(side)
            if pd is not None and self.page < pd.page_count:
                pp = pd[self.page]
            pm = _pixmap_gray_under_annots(page, self.dpi, pp)
        else:
            pm = page.get_pixmap(dpi=self.dpi, annots=True)
        img = tk.PhotoImage(
            data=base64.b64encode(pm.tobytes("png")).decode("ascii"))
        while len(self._cache) >= CACHE_MAX * 2:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = img
        return img

    def _on_gray_toggle(self):
        """「底图灰度」开关：只换渲染方式，不重算、不碰判定结果。

        缓存键里带了 gray 标志，所以来回切换不必清空 —— 切回去时若那一版
        还在缓存里就是瞬间的；不在就重渲染当前页（一页，几十毫秒）。
        """
        if not self.marks:
            return                        # 还没跑过对比，没有画面可重绘
        self._reset_view = False          # 保持当前滚动位置
        self.render()
        self.status_var.set(
            "底图灰度：开（只影响显示，不影响对比结果）" if self.gray_on.get()
            else "底图灰度：关（按原件原色显示）")

    def _on_warn_toggle(self):
        """「开跑前提示」开关：只记偏好，不碰判定、也不重画。

        关掉之后，纯图片/扫描件会**直接开跑**（不再问），行为与 5.0 之前一致；
        勾回来就恢复提醒。选择存进「上次选择.json」，下次启动沿用。
        """
        self._save_cfg()
        if self.warn_on.get():
            self.status_var.set(
                "开跑前提示：开（纯图片/扫描件会先问一句；转曲、DWG 图文穿插不问）")
        else:
            self.status_var.set("开跑前提示：关（纯图片/扫描件直接开跑，不再询问）")

    # ---------------- 翻页 / 缩放 / 滚动 ----------------
    def _prev_page(self):
        if self.marks and self.page > 0:
            self.page -= 1
            self._reset_view = True
            self.render()

    def _next_page(self):
        if self.marks:
            n = max(self.marks["old"].get("pages", 0),
                    self.marks["new"].get("pages", 0))
            if self.page < n - 1:
                self.page += 1
                self._reset_view = True
                self.render()

    def _zoom(self, d: int, anchor=None):
        """放大/缩小。`anchor=(side, x, y)` 时以该鼠标点为锚点缩放（滚轮用）。"""
        idx = min(range(len(DPI_STEPS)),
                  key=lambda i: abs(DPI_STEPS[i] - self.dpi))
        idx = max(0, min(len(DPI_STEPS) - 1, idx + d))
        if DPI_STEPS[idx] == self.dpi:
            return                                    # 已到顶/到底，不必重绘
        keep = self._anchor_rel(anchor) if anchor else None
        self.dpi = DPI_STEPS[idx]
        self._reset_view = False
        self.render()
        if keep:
            self._anchor_restore(*keep)

    def _anchor_rel(self, anchor):
        """换算鼠标点对应的**内容相对坐标**（0~1），供缩放后复位。"""
        side, ex, ey = anchor
        cv = self.canv.get(side)
        bb = cv.bbox("all") if cv is not None else None
        if not bb or bb[2] < 2 or bb[3] < 2:
            return None
        return (side, cv.canvasx(ex) / bb[2], cv.canvasy(ey) / bb[3], ex, ey)

    def _anchor_restore(self, side, rx, ry, ex, ey):
        """缩放后让该内容点仍落在鼠标原位 —— 否则放大一下画面就飘走。"""
        cv = self.canv.get(side)
        bb = cv.bbox("all") if cv is not None else None
        if not bb or bb[2] < 2 or bb[3] < 2:
            return
        w, h = float(bb[2]), float(bb[3])
        cv.xview_moveto(max(0.0, min(1.0, (rx * w - ex) / w)))
        cv.yview_moveto(max(0.0, min(1.0, (ry * h - ey) / h)))
        self._sync_scrollbars(side)

    def _fit_width(self):
        self._auto_fit = True
        self._reset_view = False
        self.render()

    # ---------------- 联动滚动（两栏横向 + 纵向都同步） ----------------
    def _scroll(self, side, *args):
        self.canv[side].yview(*args)
        self._sync_scrollbars(side)

    def _hscroll(self, side, *args):
        self.canv[side].xview(*args)
        self._sync_scrollbars(side)

    def _from_bar(self, side, first, last):
        self.vsb[side].set(first, last)
        self._sync_scrollbars(side)

    def _from_hbar(self, side, first, last):
        self.hsb[side].set(first, last)
        self._sync_scrollbars(side)

    def _sync_scrollbars(self, src=None):
        """左右两块**横向 + 纵向**都对齐到 `src` 那一侧的位置。

        参考 2.0 `diff_preview._sync_scrollbars`：放大之后主要靠横向拖，
        横向纵向都要一起跟，否则「左边滑一次、右边滑一次」。
        """
        if self._syncing:
            return
        self._syncing = True
        try:
            order = ("old", "new")
            if src in order:
                yf = self.canv[src].yview()[0]
                xf = self.canv[src].xview()[0]
                for s in order:
                    if s != src:
                        self.canv[s].yview_moveto(yf)
                        self.canv[s].xview_moveto(xf)
            for s in order:
                self.vsb[s].set(*self.canv[s].yview())
                if self.hsb.get(s) is not None:
                    self.hsb[s].set(*self.canv[s].xview())
        except Exception:
            pass
        finally:
            self._syncing = False

    def _wheel_zoom(self, side, event):
        """滚轮 = 缩放，锚定鼠标位置（滚到哪就放大到哪）。

        ⚠️ 触控板双指滑动会连发大量 MouseWheel 事件，不节流会一次跨过好几档
        并让主线程发卡 —— 40ms 内只认第一次。
        """
        now = time.monotonic()
        if now - self._last_zoom < 0.04:
            return "break"
        d = getattr(event, "delta", 0)
        if d == 0:
            num = getattr(event, "num", 0)
            step = 1 if num == 4 else (-1 if num == 5 else 0)
        else:
            step = 1 if d > 0 else -1
        if step:
            self._last_zoom = now
            self._zoom(step, anchor=(side, event.x, event.y))
        return "break"

    def _wheel(self, side, event):
        """Shift+滚轮 / 触控板双指：**纵向**滚动（两栏同步）。

        macOS 与 Windows 的 delta 符号相反，只取方向 ±1 折算滚动单位，
        方向交给 delta 本身，不猜正负。
        """
        d = getattr(event, "delta", 0)
        if d == 0:
            num = getattr(event, "num", 0)
            step = -1 if num == 4 else (1 if num == 5 else 0)
        else:
            step = int(d / max(1, abs(d)))
        if step:
            self.canv[side].yview_scroll(-step * 3, "units")
            self._sync_scrollbars(side)
        return "break"

    def _wheel_h(self, side, event):
        """横向滚动（Shift+滚轮 / 触控板双指横滑 / 鼠标左右滚轮）。"""
        d = getattr(event, "delta", 0)
        num = getattr(event, "num", 0)
        if d == 0 and num:
            step = -1 if num == 6 else (1 if num == 7 else 0)
        elif d:
            step = int(d / max(1, abs(d)))
        else:
            step = 0
        if step:
            self.canv[side].xview_scroll(-step * 3, "units")
            self._sync_scrollbars(side)
        return "break"

    def _drag_start(self, event):
        self._drag = (event.x, event.y)

    def _drag_move(self, event):
        """按住左键拖动 = 平移（放大之后靠滚动条太慢），两栏一起跟。"""
        if self._drag is None:
            self._drag = (event.x, event.y)
            return
        x0, y0 = self._drag
        side = "old" if self.canv["old"] is event.widget else "new"
        cv = self.canv[side]
        bx0, by0 = cv.canvasx(0), cv.canvasy(0)
        self._drag = (event.x, event.y)
        bb = cv.bbox("all") or (0, 0, 1, 1)
        cv.xview_moveto(max(0.0, (bx0 - (event.x - x0)) / max(1.0, bb[2])))
        cv.yview_moveto(max(0.0, (by0 - (event.y - y0)) / max(1.0, bb[3])))
        self._sync_scrollbars(side)

    def _on_resize(self, _e=None):
        if self.marks and self._auto_fit:
            self.render()

    # ---------------- 导出 ----------------
    @staticmethod
    def _diffs_json(diffs) -> list:
        """把 diffs 转成可 JSON 序列化的 dict 列表。

        ⚠️ 老引擎返回的是 `pdfdiff_locate.LocDiff` 对象，直接塞进 `json.dump`
        会在**写文件写到一半**时才抛 TypeError —— 弹「导出失败」但半截文件
        已经落盘，看起来像导出成功了（2026-10-13 用户踩到的就是这个）。
        所以这里先在内存里转干净，序列化成功才碰磁盘。
        """
        out = []
        for it in (diffs or []):
            d = it.__dict__ if hasattr(it, "__dict__") else it
            if isinstance(d, dict):
                out.append({k: (list(v) if isinstance(v, (list, tuple)) else v)
                            for k, v in d.items()})
            else:
                out.append(str(it))
        return out

    def _export(self):
        if not self.marks:
            messagebox.showinfo(APP_TITLE, "还没有对比结果可导出")
            return
        b1 = os.path.splitext(os.path.basename(self.old_var.get()))[0]
        b2 = os.path.splitext(os.path.basename(self.new_var.get()))[0]
        ts = time.strftime("%m%d_%H%M")

        def side_json(side):
            def rects(d):
                return {str(pi): [[round(r.x0, 1), round(r.y0, 1),
                                   round(r.x1, 1), round(r.y1, 1)] for r in rs]
                        for pi, rs in d.items()}
            bk = {k: rects(v) for k, v in side["by_kind"].items()}
            return {
                "marks": rects(side["marks"]),
                "by_kind": bk,
                "boxes": {k: v for k, v in side["boxes_by_kind"].items()},
                "pages": side["pages"],
                "marked": side["stats"]["marked"],
            }

        out = {
            # 5.0（2026-10-20）：从 "3.0-classic" 改为 "5.0"。
            # 查证过全盘无任何读取方（各版 diff_app*.py 都只是写、从不读），
            # 所以改它不影响任何判定；只是让新导出的 JSON 能和 3.0/4.0 的存量文件区分开。
            # ⚠️ 存量 JSON 文件不受影响（已落盘的不会变）。
            "version": "5.0",
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "old": self.old_var.get(),
            "new": self.new_var.get(),
            "elapsed": round(self.marks.get("elapsed", 0), 2),
            "diffs": self._diffs_json(self.marks.get("diffs")),
            "old_side": side_json(self.marks["old"]),
            "new_side": side_json(self.marks["new"]),
        }
        # ⚠️ 先整体序列化成字符串（这步可能抛错），成功才写盘 ——
        #    失败时绝不留半截 JSON 假装导出成功。
        try:
            text = json.dumps(out, ensure_ascii=False, indent=1)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"导出失败（未写任何文件）：{e}")
            return
        p = filedialog.asksaveasfilename(
            title="导出 JSON", defaultextension=".json",
            initialdir=self.export_dir or None,
            # 5.0（2026-10-20）：前缀 "PDF差异3.0_" 是 3.0 时代留下的历史命名，
            # 4.0 沿用未改。5.0 起跟上版本号（同样无读取方，纯展示）。
            initialfile=f"PDF差异5.0_{b1}_vs_{b2}_{ts}.json",
            filetypes=[("JSON", "*.json")])
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
            # 导出目录单独记忆（与文件选择记忆分开）
            self.export_dir = os.path.dirname(p)
            self._save_cfg()
            self.status_var.set(f"已导出：{p}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"导出失败：{e}")

    def _on_close(self):
        self._save_cfg()
        for d in list(self.docs.values()) + list(self.docs_plain.values()):
            try:
                d.close()
            except Exception:
                pass
        self.destroy()


def _ocr_selfcheck(app) -> None:
    """启动后自检 OCR 引擎；不可用时明确告警，不让它静默失效。

    为什么必须弹窗：OCR 引擎不可用时，`_ocr_image` 会按引擎表一路降级到
    返回空串 —— 整条「转曲文字 / 图片区」识别**静默失效**，界面不报错、
    不崩溃，结果看起来只是「少标了几处」。用户会归因为「识别不准」，
    实际是根本没在认字，查起来极费劲（GRA-3021 排查时踩过同类坑）。

    最常见根因：Windows 的 VC++ 运行库版本低于 ONNX Runtime 1.31 要求的
    14.40，`import onnxruntime` 加载 .pyd 时 DLL 解析失败。
    """
    try:
        import pdfdiff_locate as loc

        if loc._rapidocr_engine() is not None:
            return  # 首选引擎正常，什么都不说
        if loc._vision_exe() or loc._OCR_EXE:
            # 还有兜底引擎：能用，但识别质量下降，状态栏提一句即可
            app.status_var.set("提示：RapidOCR 不可用，已降级到备用 OCR 引擎")
            return
        hint = ("安装微软官方 VC++ 运行库后重启电脑\n"
                "https://aka.ms/vs/17/release/vc_redist.x64.exe"
                if os.name == "nt" else
                "检查 rapidocr / onnxruntime 是否安装成功")
        messagebox.showwarning(
            APP_TITLE,
            "OCR 引擎不可用：图形区与转曲文字的差异将无法识别。\n\n"
            "程序不会崩溃，但结果是「该标出来的差异没标出来」。\n\n"
            "最常见原因：ONNX Runtime 的原生依赖加载失败。\n\n"
            "修复：" + hint)
    except (ImportError, OSError, AttributeError) as e:
        # 自检本身失败不该挡住启动，但必须留痕，不能静默
        print("warning: OCR 启动自检跳过（%s: %s）"
              % (type(e).__name__, e), file=_sys.stderr)


def main():
    app = App()
    app.after(400, lambda: _ocr_selfcheck(app))
    app.mainloop()


if __name__ == "__main__":
    main()
