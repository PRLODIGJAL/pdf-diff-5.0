# -*- coding: utf-8 -*-
"""开跑前的「输入体检」：这对文件是不是**纯图片/扫描件**，要不要先问一句。

为什么要有这个模块（2026-10-21 刘剑要求）
------------------------------------------------------------------
纯图片 PDF、纯扫描件、以及 PNG/JPG 直接投进来的图片，**都能跑**，但它们走的是
OCR 路线：单页实测 100~280 秒（原生 PDF 只要几秒），结论只能当「提示器」，
每条都得人工核。所以开跑前先说一句，比跑到一半才发现要等五分钟好。

⚠️ 只提醒**纯图片类**，不提醒转曲类（2026-10-21 刘剑补充）：
   「转曲类的就不用在意了，因为它并不是每次都出现，也并不是百分百全部都是转曲。
     只对那种图片类、还有导入 PNG 的，就是纯图片类进行提示。
     像 DWG 那种中间穿插图片类的也不需要提示。」
   所以判据里必须把「扫描件」和「转曲（矢量）」分开 —— 两者都是「没有文字层」，
   但一个是**整页位图**、一个是**矢量线条**。

判据：文字层 → 位图 → 矢量，三级
------------------------------------------------------------------
逐页看，先看文字层（这是决定准确率的那条线，见项目说明第十四节）：

    文字字符 ≥ MIN_TEXT_CHARS           → 文字页（逐字比对，快且准）
    无文字 + 位图覆盖 ≥ IMG_COVER_MIN   → 图片页（扫描件/标签图，只能 OCR）
    无文字 + 位图覆盖 <  IMG_COVER_MIN  → 矢量页（转曲/图形，按图形比对）

整份的结论：

    有文字页                             → text / mixed   （**不提醒**）
    没有文字页，图片页占多数或全部        → scan           （**提醒**）
    没有文字页，矢量页更多               → vector         （不提醒，按刘剑这条）

⚠️ 图像覆盖率**不能单独当判据**：`GRA-4161 rev01` 整页是一张图（覆盖 1.00）但
   文字层 2/2 完整，引擎走逐字比对，快且准；`DWG` 图像覆盖 24% 且文字层完整。
   这两种都不该被打扰 —— 所以「有文字页」永远优先。

只读、不渲染、不 OCR
------------------------------------------------------------------
全程只调 `get_text` / `get_images` / `get_image_rects` / `get_drawings`，
**不 get_pixmap、不调 OCR、不写缓存**。实测 3 页 DWG 约 0.5 秒、1 页 IFU 约 0.02 秒；
超过 `MAX_PAGES` 页时等间隔抽样（只影响提示精度，不影响判定）。
本模块**不在判定链上**，`pdfdiff_locate` / `pdf_annotate` / `pdfdiff_embed` 都没引用它。
"""
import os
import re

try:
    import pymupdf as fitz
except ImportError:                       # pragma: no cover
    import fitz  # type: ignore

# 引擎的「转曲文字」判据（pdfdiff_embed 第 440~444 行）。**必须复用同一套**，
# 不能自己另定：这里判的「转曲」要和引擎实际走块级/OCR 的对象一致，
# 否则提示和真实行为对不上（见项目说明第十四节）。
try:
    from pdfdiff_embed import (  # noqa: E402
        VECTOR_TEXT_H_MIN, VECTOR_TEXT_H_MAX,
        VECTOR_TEXT_ITEMS_MIN, VECTOR_ROW_Y_TOL,
    )
except Exception:                         # pragma: no cover  独立自测也能跑
    VECTOR_TEXT_H_MIN = 3.0
    VECTOR_TEXT_H_MAX = 15.0
    VECTOR_TEXT_ITEMS_MIN = 50
    VECTOR_ROW_Y_TOL = 8.0

# 与 core/pdfdiff_img.IMG_EXT_MAP 保持一致的图片后缀（那份管「能不能折成 PDF」，
# 这里管「要不要提醒」，认同一批格式，避免「能拖进来却不提醒」）
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".jpe", ".bmp", ".tif", ".tiff",
             ".gif", ".webp")

# 一页有多少字符才算「有文字层」。
# 取 10 而不是 0：扫描件上常有页码/水印被留成 1~2 个字符的文字对象，
# 那几行撑不起逐字比对；10 能挡住这类假文字层。
MIN_TEXT_CHARS = 10

# 一页里位图要覆盖多少面积，才算「图片页」而不是「带了个 logo 的矢量页」。
# 扫描件/标签图实测 0.85~1.00；转曲页即使带小图标也远低于这个数。
IMG_COVER_MIN = 0.35

# 最多逐页扫多少页。超过就等间隔抽样 —— 体检只为「要不要提醒」，
# 不值得为 200 页文档花几秒钟。
MAX_PAGES = 60

# 「转曲占比」两个口径的提醒阈值（2026-10-21 刘剑拍板）。
# 只用「墨迹占比」或只用「行数占比」都会误伤：
#   · 11/12.pdf：转曲行 8.8% 但墨迹 24%（留了完整文字层，走逐字，不该拦）
#   · M4506：墨迹 62.8% 但行数占比 40.7%（文字层还剩大半，准确率尚可，不拦）
#   · 4048：墨迹 85% 且行数占比 83.9%（正文几乎全转曲、文字层只剩 9~15 行，该拦）
# 所以取「墨迹 ≥ 80% 或 行数占比 ≥ 60%」，两者命中其一才提醒。
CURVE_INK_MIN = 0.80        # 转曲墨迹面积 / 页面面积
CURVE_LINE_MIN = 0.60       # 转曲行数 / (转曲行 + 文字层行数)

KIND_TEXT = "text"          # 每页都有文字层 → 原生 / 转曲带隐形字 / 图文混排
KIND_OFFICE = "office"      # Word / Excel 输入（转 PDF 后自带文字层 → 不提醒）
KIND_MIXED = "mixed"        # 有文字页，也有一部分页没有文字层
KIND_SCAN = "scan"          # 没有文字页、以整页位图为主 → 扫描件 / 标签图（要提醒）
KIND_VECTOR = "vector"      # 没有文字页、以矢量线条为主 → 转曲（不提醒）
KIND_IMAGE = "image"        # 本身就是 PNG/JPG 等图片文件（要提醒）
KIND_EMPTY = "empty"        # 0 页 / 全是空白页
KIND_UNKNOWN = "unknown"    # 打不开 / 读不了（交给引擎报错，这里不拦）

KIND_CN = {
    KIND_TEXT: "文字层完整",
    KIND_OFFICE: "Word/Excel（转 PDF 后逐字比对）",
    KIND_MIXED: "部分页没有文字层",
    KIND_SCAN: "整页图像，没有文字层",
    KIND_VECTOR: "矢量图形，没有文字层",
    KIND_IMAGE: "图片文件",
    KIND_EMPTY: "空文档",
    KIND_UNKNOWN: "读不出结构",
}

# 单页 OCR 路线的实测耗时区间（秒）：纯转曲合成样本 102 秒、4048 抹掉文字层后
# 276 秒、3021 标签对 561 秒（单页约 280 秒）；取 60~300 是个诚实的粗区间。
SEC_PER_PAGE_LOW = 60
SEC_PER_PAGE_HIGH = 300

_SQUASH = re.compile(r"\s+")


def _squash(s):
    return _SQUASH.sub("", s or "")


def _page_text_chars(pg):
    try:
        return len(_squash(pg.get_text("text")))
    except Exception:
        return 0


def _image_cover(pg):
    """位图覆盖面积 / 页面面积。"""
    try:
        pa = abs(pg.rect.width * pg.rect.height) or 1.0
        area = 0.0
        for im in pg.get_images(full=True):
            for r in pg.get_image_rects(im[0]):
                area += abs(r.width * r.height)
        return min(1.0, area / pa)
    except Exception:
        return 0.0


def _is_blank(pg, chars, cover):
    """空白页：没字、没位图、没矢量。这种页不该被算成「只能看图」。"""
    if chars >= MIN_TEXT_CHARS or cover >= 0.01:
        return False
    try:
        return not pg.get_drawings()
    except Exception:
        return False


def _glyph_rows(pg):
    """一页里的「文字样矢量簇」（转曲后的字形轮廓）。

    判据与引擎一致（`pdfdiff_embed`：有 fill、高 ∈[3,15]pt、path 项数 ≥50），
    再用行聚合（y 容差 8pt）把相邻字形拼成「行」。返回 (每行外接矩形列表,
    这些行的墨迹总面积)。

    ⚠️ 用 `get_cdrawings` 不用 `get_drawings`：结果路径数一致，但快 2~8 倍
    （4048 单页 0.94s → 0.34s），这是「1 秒内判出转曲比例」的关键。
    ⚠️ 引擎判据分不出「矢量图标」和「转曲文字」——图标也满足 fill + 高 3~15pt +
    items≥50。所以行数占比会偏高，必须和墨迹占比一起看，且阈值只认「占比很大」。
    """
    try:
        dws = pg.get_cdrawings()
    except Exception:
        return [], 0.0
    rects = []
    for d in dws:
        if not d.get("fill") or d.get("rect") is None:
            continue
        r = fitz.Rect(d["rect"])
        if VECTOR_TEXT_H_MIN <= r.height <= VECTOR_TEXT_H_MAX and \
                len(d.get("items") or []) >= VECTOR_TEXT_ITEMS_MIN:
            rects.append(r)
    if not rects:
        return [], 0.0
    rects.sort(key=lambda r: (r.y0 + r.y1) / 2.0)
    rows, cur_y = [], None
    for r in rects:
        cy = (r.y0 + r.y1) / 2.0
        if rows and abs(cy - cur_y) <= VECTOR_ROW_Y_TOL:
            rows[-1].append(r)
            cur_y = sum((x.y0 + x.y1) / 2.0 for x in rows[-1]) / len(rows[-1])
        else:
            rows.append([r])
            cur_y = cy
    out = []
    area = 0.0
    for grp in rows:
        u = fitz.Rect(grp[0])
        for g in grp[1:]:
            u |= g
        out.append(u)
        area += abs(u.width * u.height)
    return out, area


def _text_line_count(pg):
    """一页的文字层行数（只对有文字的页调用）。"""
    try:
        dd = pg.get_text("dict")
        return sum(len(b.get("lines", [])) for b in dd["blocks"])
    except Exception:
        return 0


def _curve_metrics(pg):
    """单页转曲占比：返回 (行数占比, 墨迹占比)。无文字层时行数占比按 1.0 算。"""
    glyph_rows, ink = _glyph_rows(pg)
    pa = abs(pg.rect.width * pg.rect.height) or 1.0
    ink_frac = min(1.0, ink / pa)
    if not glyph_rows:
        return 0.0, ink_frac
    chars = _page_text_chars(pg)
    if chars < MIN_TEXT_CHARS:
        # 这页没有文字层，却有一堆字形簇 → 行数占比按「全部转曲」算
        return 1.0, ink_frac
    tl = _text_line_count(pg)
    line_frac = len(glyph_rows) / max(1, len(glyph_rows) + tl)
    return line_frac, ink_frac


def probe(path, max_pages=MAX_PAGES):
    """体检**单个文件**，返回全裸类型 dict（可 JSON 化）。

    键：path / kind / pages / pages_seen / text_pages / image_pages /
        vector_pages / chars / cover / sampled / is_image / note
    """
    out = {
        "path": path or "",
        "kind": KIND_UNKNOWN,
        "pages": 0,
        "pages_seen": 0,
        "text_pages": 0,
        "image_pages": 0,
        "vector_pages": 0,
        "chars": 0,
        "cover": 0.0,
        "sampled": False,
        "is_image": False,
        "curve_line": 0.0,       # 转曲行数占比（抽样页平均）
        "curve_ink": 0.0,        # 转曲墨迹占比（抽样页平均）
        "curve_heavy": False,    # 是否「转曲占比高到该提醒」
        "note": "",
    }
    if not path or not os.path.isfile(path):
        out["note"] = "文件不存在"
        return out

    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        # 图片文件：折成单页 PDF 后必然没有文字层，一步到位
        out.update(kind=KIND_IMAGE, pages=1, pages_seen=1, image_pages=1,
                   is_image=True, cover=1.0,
                   note="图片文件，折成单页 PDF 后走扫描件链路")
        return out

    # Word / Excel（2026-10-21 加）：转 PDF 后**自带完整文字层**，走原生逐字比对
    # （快且准），不属于「纯图片类」—— 不提醒。判据只看扩展名，不为体检去起 Office
    # （体检必须 1 秒内出结论）。真正转不出来时由转换层如实报错，不在这里拦。
    import pdfdiff_office as _off
    if _off.is_office_file(path):
        out.update(kind=KIND_OFFICE,
                   note="Word/Excel 文档，转成 PDF 后自带文字层、走逐字比对")
        return out

    try:
        doc = fitz.open(path)
    except Exception as e:                # 打不开：不在这一层拦，让引擎如实报错
        out["note"] = "打开失败：%s" % e
        return out
    try:
        n = doc.page_count
        out["pages"] = n
        if n == 0:
            out["kind"] = KIND_EMPTY
            out["note"] = "文档没有页面"
            return out

        # 页数多就等间隔抽样（首尾必取），只影响提示精度
        if n > max_pages:
            step = max(1, n // max_pages)
            idx = list(range(0, n, step))[:max_pages]
            if idx[-1] != n - 1:
                idx[-1] = n - 1
            out["sampled"] = True
        else:
            idx = list(range(n))

        tp = ip = vp = 0
        chars = 0
        covers = []
        line_fracs = []
        ink_fracs = []
        for i in idx:
            pg = doc[i]
            c = _page_text_chars(pg)
            chars += c
            cv = _image_cover(pg)
            covers.append(cv)
            if c >= MIN_TEXT_CHARS:
                tp += 1
            elif _is_blank(pg, c, cv):
                pass                       # 空白页：三类都不算
            elif cv >= IMG_COVER_MIN:
                ip += 1                    # 整页位图 → 扫描件/标签图
            else:
                vp += 1                    # 矢量线条 → 转曲/图形
            # 转曲占比：只对「有转曲嫌疑」的页才值得算，但 get_cdrawings 便宜，
            # 这里对每页都算，两个口径取页平均（与 curve_probe 的整份合计语义一致）
            lf, ifr = _curve_metrics(pg)
            line_fracs.append(lf)
            ink_fracs.append(ifr)
        seen = len(idx)
        out.update(pages_seen=seen, text_pages=tp, image_pages=ip,
                   vector_pages=vp, chars=chars,
                   cover=round(sum(covers) / seen, 3) if covers else 0.0)
        if seen:
            out["curve_line"] = round(sum(line_fracs) / seen, 3)
            out["curve_ink"] = round(sum(ink_fracs) / seen, 3)
            out["curve_heavy"] = (out["curve_ink"] >= CURVE_INK_MIN
                                  or out["curve_line"] >= CURVE_LINE_MIN)

        # 抽样时按比例推算回整份（只用于提示与耗时估算）
        if out["sampled"] and seen:
            scale = n / float(seen)
            out["text_pages"] = int(round(tp * scale))
            out["image_pages"] = int(round(ip * scale))
            out["vector_pages"] = int(round(vp * scale))

        if tp + ip + vp == 0:
            out["kind"] = KIND_EMPTY       # 全是空白页
        elif tp and ip + vp == 0:
            out["kind"] = KIND_TEXT
        elif tp:
            out["kind"] = KIND_MIXED
        elif ip > vp:
            out["kind"] = KIND_SCAN
        else:
            out["kind"] = KIND_VECTOR
        return out
    except Exception as e:                # 读页失败：同样交给引擎
        out["note"] = "读取失败：%s" % e
        return out
    finally:
        try:
            doc.close()
        except Exception:
            pass


def warn_kinds(probe_result):
    """这一侧的值不值得「开跑前问一句」。纯图片类，或转曲占比高到接近全转曲。"""
    if probe_result["kind"] in (KIND_IMAGE, KIND_SCAN):
        return True
    return bool(probe_result.get("curve_heavy"))


def _is_curve(s):
    """这一侧触发提醒的，是「高转曲占比」而不是纯图片类。"""
    return s["kind"] not in (KIND_IMAGE, KIND_SCAN) and bool(s.get("curve_heavy"))


def assess(old_path, new_path, max_pages=MAX_PAGES):
    """体检一对输入。

    返回：{level, severity, old, new, sides, slow_pages, seconds, reasons,
           title, lines}
      level    : "ok" | "warn"
      severity : 0 不提醒 / 3 纯图片类（要提醒）
    """
    sides = {"old": probe(old_path, max_pages), "new": probe(new_path, max_pages)}
    need = []
    slow_pages = 0
    for key, name in (("old", "旧版"), ("new", "新版")):
        s = sides[key]
        if warn_kinds(s):
            need.append((key, name, s))
            slow_pages += max(1, s["pages"]) if s["kind"] == KIND_IMAGE \
                else s["pages"]

    level = "warn" if need else "ok"
    reasons = []
    for _key, name, s in need:
        if s["kind"] == KIND_IMAGE:
            reasons.append("%s是图片文件（%s）" % (name, os.path.splitext(
                s["path"])[1].lower().lstrip(".").upper()))
        elif _is_curve(s):
            reasons.append("%s文字大多是转曲（墨迹 %.0f%%、转曲行 %.0f%%）"
                           % (name, s["curve_ink"] * 100, s["curve_line"] * 100))
        else:
            reasons.append("%s整份 %d 页都是图像、没有文字层" % (name, s["pages"]))
    # 标题：区分「纯图片」和「高转曲」两种提醒原因，别把转曲说成扫描件
    has_img = any(s["kind"] in (KIND_IMAGE, KIND_SCAN) for _k, _n, s in need)
    has_crv = any(_is_curve(s) for _k, _n, s in need)
    if need and not has_img and has_crv:
        title = ("这两份文字大多是转曲（只能当提示看）" if len(need) >= 2
                 else "其中一份文字大多是转曲（只能当提示看）")
    else:
        title = ("这两份都是纯图片/扫描件" if len(need) >= 2
                 else "其中一份是纯图片/扫描件") if need else ""
    return {
        "level": level,
        "severity": 3 if need else 0,
        "old": sides["old"],
        "new": sides["new"],
        "sides": sides,
        "slow_pages": slow_pages,
        "seconds": (slow_pages * SEC_PER_PAGE_LOW,
                    slow_pages * SEC_PER_PAGE_HIGH),
        "reasons": reasons,
        # 只有一侧是需要提醒的，标题不能写「这对文件」—— 另一侧可能完全正常，
        # 说成「两份都不行」会让人以为没救了，直接放弃对比。
        "warn_sides": len(need),
        "title": title,
        "lines": side_lines(sides),
    }


def side_lines(sides):
    """给弹窗用：每侧一行「旧版　文件名　·　几页　·　什么情况」。"""
    out = []
    for key, name in (("old", "旧版"), ("new", "新版")):
        s = sides[key]
        fn = os.path.basename(s["path"]) or "—"
        if len(fn) > 34:
            fn = fn[:32] + "…"
        if not s["pages"]:
            out.append("%s　%s　·　%s" % (name, fn, KIND_CN.get(s["kind"], "未知")))
            continue
        bits = ["%d 页" % s["pages"]]
        # 高转曲优先显示「文字多为转曲」，别再写「文字层完整」——
        # 4048 有 9~15 行地址的文字层，但正文全转曲，写「完整」会误导
        if _is_curve(s):
            bits.append("文字多为转曲（墨迹 %.0f%%）" % (s["curve_ink"] * 100))
        else:
            bits.append(KIND_CN.get(s["kind"], "未知"))
            # 只有「不是整页图、但图占了大半」才补一句覆盖率，
            # 否则会写成「整页图像，没有文字层　·　整页图像」（重复且啰嗦）
            if s["kind"] == KIND_SCAN and s["cover"] < 0.9:
                bits.append("约 %d%% 面积是图像" % int(round(s["cover"] * 100)))
        out.append("%s　%s　·　%s" % (name, fn, "　·　".join(bits)))
    return out


def body_text(rep):
    """弹窗正文（纯文字，便于测试与复用）：(标题下第一段, 第二段)。"""
    slow = rep["slow_pages"]
    lo, hi = rep["seconds"]
    lo_m, hi_m = max(1, lo // 60), max(1, hi // 60)
    crv = any(_is_curve(s) for _k, _n, s in
              ((k, n, rep["sides"][k]) for k, n in (("old", "旧"), ("new", "新"))))
    if crv:
        # 高转曲：多数还有少量文字层（如 4048 的地址几行），但正文只能看图
        head = ("转曲是把文字变成了图形轮廓：剩下那点文字层远不足以逐字比对，"
                "大部分内容只能按图形认字，结论只能当提示，每条都要人工核对。")
    elif rep.get("warn_sides", 2) <= 1:
        head = ("其中一份没有可读的文字层，那一份只能把页面当成图片来认字，"
                "结论只能当提示，每条都要人工核对；另一份有文字层，照常逐字比对。")
    else:
        head = ("这两份都没有可读的文字层，比对时只能把页面当成图片来认字，"
                "结论只能当提示，每条都要人工核对。")
    tail = ("按已测案例，这类文件单页要 %d~%d 分钟（原生 PDF 只要几秒）；"
            "本对约 %d 页要走这条路，粗估 %d~%d 分钟，等得久可以点「停止对比」中止。"
            % (SEC_PER_PAGE_LOW // 60, SEC_PER_PAGE_HIGH // 60,
               slow, lo_m, hi_m))
    return head, tail


def summary_line(rep):
    """状态栏一句话（确认继续之后用）。"""
    if rep["level"] != "warn":
        return ""
    crv = any(_is_curve(rep["sides"][k]) for k in ("old", "new"))
    what = "转曲类·走图形认字" if crv else "纯图片类·走 OCR"
    return "已确认%s：约 %d 页，粗估 %d~%d 分钟" % (
        what, rep["slow_pages"], max(1, rep["seconds"][0] // 60),
        max(1, rep["seconds"][1] // 60))


# ==================================================================
# 自检：不依赖真实语料，造「原生 / 纯图 / 纯矢量 / 混合 / 图片」五类
# ==================================================================
def _make_text_pdf(fp, pages=1):
    d = fitz.open()
    for _ in range(pages):
        p = d.new_page(width=300, height=200)
        p.insert_text((20, 40), "Warning: do not reuse the device.", fontsize=9)
        p.insert_text((20, 60), "使用期限过后请勿继续使用本器械。", fontsize=9,
                      fontname="china-s")
    d.save(fp)
    d.close()
    return fp


def _make_image_pdf(fp, png):
    """纯图像页：整页一张位图、无文字层（模拟 BarTender 标签/扫描件）。"""
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    p.insert_image(fitz.Rect(0, 0, 300, 200), filename=png)
    d.save(fp)
    d.close()
    return fp


def _make_vector_pdf(fp):
    """纯矢量页：无文字层、无位图，只有一堆小填充路径（模拟转曲后的字形轮廓）。"""
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    for i in range(60):
        x = 15 + (i % 12) * 22
        y = 30 + (i // 12) * 22
        p.draw_rect(fitz.Rect(x, y, x + 12, y + 12), color=(0, 0, 0),
                    fill=(0, 0, 0), width=0)
    d.save(fp)
    d.close()
    return fp


def _make_curve_pdf(fp, rows=6):
    """真·转曲页：无文字层、无位图，只有一堆高复杂度 fill 矢量簇。

    每个「字形」画成一个 60 顶点的锯齿形填充 polyline（`get_cdrawings` 会
    报告 items≈60 ≥ VECTOR_TEXT_ITEMS_MIN），高度落在 [3,15]pt 内、有 fill，
    满足引擎的「转曲文字簇」判据 —— 但没有文字层。模拟 4048 那类正文全转曲。
    """
    import math
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    for r in range(rows):
        y = 26 + r * 22
        for k in range(12):
            x = 16 + k * 22
            pts = []
            for i in range(61):          # 61 个顶点 → items≈60
                fx = x + i * (12.0 / 60.0)
                fy = y - 5.0 + 3.0 * math.sin(i * math.pi / 4.0)
                pts.append((fx, fy))
            pts.append((x, y + 6.0))     # 闭合回底边
            p.draw_polyline(pts, color=(0, 0, 0), fill=(0, 0, 0), width=0)
    d.save(fp)
    d.close()
    return fp


def self_test():
    import tempfile

    ok = 0

    def chk(cond, msg, extra=""):
        nonlocal ok
        if cond:
            ok += 1
            print("  ✓ %s%s" % (msg, ("  " + str(extra)) if extra else ""))
        else:
            print("  ✗ %s%s" % (msg, ("  → " + str(extra)) if extra else ""))

    tmp = tempfile.mkdtemp(prefix="pdftest_pre_")
    png = os.path.join(tmp, "src.png")
    d = fitz.open()
    p = d.new_page(width=300, height=200)
    p.draw_rect(fitz.Rect(0, 0, 300, 200), fill=(0.92, 0.92, 0.92), width=0)
    p.insert_text((20, 60), "LABEL", fontsize=24)
    p.get_pixmap(dpi=72).save(png)
    d.close()

    T1 = _make_text_pdf(os.path.join(tmp, "t1.pdf"))
    T3 = _make_text_pdf(os.path.join(tmp, "t3.pdf"), pages=3)
    I1 = _make_image_pdf(os.path.join(tmp, "i1.pdf"), png)
    V1 = _make_vector_pdf(os.path.join(tmp, "v1.pdf"))
    C1 = _make_curve_pdf(os.path.join(tmp, "c1.pdf"))   # 真转曲：无文字层、全是字形簇
    M = os.path.join(tmp, "m.pdf")                 # 一页文字 + 一页纯图
    d = fitz.open(T1)
    _make_image_pdf(os.path.join(tmp, "_i.pdf"), png)
    d.insert_pdf(fitz.open(os.path.join(tmp, "_i.pdf")))
    d.save(M)
    d.close()

    print("[1] 分档")
    chk(probe(T1)["kind"] == KIND_TEXT, "有文字层 → text")
    chk(probe(T3)["pages"] == 3 and probe(T3)["kind"] == KIND_TEXT, "3 页全文字 → text")
    chk(probe(I1)["kind"] == KIND_SCAN, "整页位图无文字 → scan")
    chk(probe(I1)["image_pages"] == 1, "图片页计数正确")
    chk(probe(V1)["kind"] == KIND_VECTOR, "纯矢量无文字 → vector（转曲）")
    chk(probe(V1)["vector_pages"] == 1 and probe(V1)["image_pages"] == 0,
        "矢量页与图片页分开计数")
    chk(probe(M)["kind"] == KIND_MIXED, "一页文字+一页纯图 → mixed")
    chk(probe(png)["kind"] == KIND_IMAGE and probe(png)["is_image"],
        "PNG → image")
    chk(probe("/不存在.pdf")["kind"] == KIND_UNKNOWN, "不存在 → unknown")

    print("\n[2] 谁该提醒（纯图片类 + 高转曲占比）")
    chk(assess(T1, T3)["level"] == "ok", "原生一对 → 不提醒")
    chk(assess(I1, I1)["level"] == "warn", "纯扫描件 → 提醒")
    chk(assess(I1, I1)["severity"] == 3, "severity=3")
    chk(assess(T1, I1)["level"] == "warn", "一原一扫描 → 提醒")
    chk(assess(png, T1)["level"] == "warn", "PNG 直接投 → 提醒")
    chk(assess(V1, V1)["level"] == "ok", "纯矢量（小图标、非转曲字形）→ **不**提醒")
    chk(assess(T1, V1)["level"] == "ok", "原生 vs 转曲 → 不提醒")
    chk(assess(T1, M)["level"] == "ok", "原生 vs 混合 → 不提醒")
    chk(assess(V1, I1)["level"] == "warn", "转曲 vs 扫描 → 提醒（有一侧是扫描件）")
    chk(assess(I1, I1)["title"] == "这两份都是纯图片/扫描件",
        "双侧都是图片 → 标题写「这两份」", assess(I1, I1)["title"])
    chk(assess(T1, I1)["title"] == "其中一份是纯图片/扫描件",
        "只有一侧是图片 → 标题写「其中一份」", assess(T1, I1)["title"])
    chk("其中一份" in body_text(assess(T1, I1))[0]
        and "另一份有文字层" in body_text(assess(T1, I1))[0],
        "单侧时正文点明另一份照常逐字比对")

    # ---- 高转曲占比提醒（2026-10-21 刘剑补充：转曲 >80~90% 也拦）----
    cr = probe(C1)
    chk(cr["kind"] == KIND_VECTOR and cr["vector_pages"] == 1,
        "真转曲页 → vector", cr["kind"])
    chk(cr["curve_heavy"], "真转曲页曲线占比高 → curve_heavy",
        (cr["curve_line"], cr["curve_ink"]))
    chk(assess(C1, C1)["level"] == "warn", "高转曲一对 → 提醒")
    chk(assess(T1, C1)["level"] == "warn", "原生 vs 高转曲 → 提醒")
    chk(assess(C1, C1)["title"] == "这两份文字大多是转曲（只能当提示看）",
        "双侧转曲 → 标题写「文字大多是转曲」", assess(C1, C1)["title"])
    chk(assess(T1, C1)["title"] == "其中一份文字大多是转曲（只能当提示看）",
        "单侧转曲 → 标题写「其中一份」", assess(T1, C1)["title"])
    chk("转曲" in assess(C1, C1)["reasons"][0],
        "reason 里写「转曲」不是「图像」", assess(C1, C1)["reasons"])

    print("\n[3] 文案与数据")
    r = assess(I1, I1)
    chk(len(r["lines"]) == 2, "每侧一行说明", len(r["lines"]))
    chk("整页图像" in r["lines"][0], "写明整页图像", r["lines"][0])
    chk(r["slow_pages"] == 2, "两侧各 1 页慢页 → 2", r["slow_pages"])
    lo, hi = r["seconds"]
    chk(0 < lo < hi, "耗时是区间", (lo, hi))
    head, tail = body_text(r)
    chk("分钟" in tail and "人工核对" in head, "正文说清慢+要人工核")
    chk("分钟" in summary_line(r), "状态栏一句话可用", summary_line(r))
    chk(assess(T1, T3)["title"] == "" and summary_line(assess(T1, T3)) == "",
        "不提醒时没有标题/状态文案")

    print("\n[4] 只读、幂等、不上判定链")
    a1, a2 = probe(I1), probe(I1)
    chk(a1 == a2, "同一文件两次结果一致")
    _b = (os.path.getsize(I1), os.path.getmtime(I1))
    probe(I1)
    chk(_b == (os.path.getsize(I1), os.path.getmtime(I1)), "体检不改文件")
    here = os.path.dirname(os.path.abspath(__file__))
    bad = [fn for fn in ("pdfdiff_locate.py", "pdf_annotate.py", "pdfdiff_embed.py")
           if os.path.isfile(os.path.join(here, fn))
           and "pdfdiff_preflight" in open(os.path.join(here, fn),
                                           encoding="utf-8").read()]
    chk(not bad, "判定链没引用体检模块", bad or "")
    # 只看**生产代码**（跳过模块 docstring；self_test 里造样本要 get_pixmap，
    # 那是测试夹具，也不算）
    src = open(os.path.abspath(__file__), encoding="utf-8").read()
    prod = src.split('"""', 2)[2].split("def _make_text_pdf", 1)[0]
    chk("get_pixmap" not in prod and "_ocr" not in prod,
        "体检的生产代码不渲染、不 OCR")

    print("\n  %d 项通过" % ok)
    return 0 if ok >= 25 else 1


if __name__ == "__main__":
    raise SystemExit(self_test())
