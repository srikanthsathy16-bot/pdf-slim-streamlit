"""PDF Slim engine (Python / PyMuPDF).

Quality rules:
- Text, fonts and vector line work are never rasterised: only re-packed losslessly.
- Line-art / bitonal / masked / palette images stay lossless.
- Only continuous-tone images that exceed the target DPI *where they are placed*
  are resampled (Lanczos) and saved as high-quality JPEG.
- Every page is rendered before/after and compared (PSNR).
"""
from __future__ import annotations

import gc
import io
import math
from dataclasses import dataclass, field

import numpy as np
import pymupdf
from PIL import Image

PRESETS = {
    "lossless": {"label": "Lossless", "images": False, "dpi": 0, "quality": 0},
    "prepress": {"label": "Prepress 300 DPI", "images": True, "dpi": 300, "quality": 92},
    "print": {"label": "Print 200 DPI", "images": True, "dpi": 200, "quality": 85},
}
SKIP_FILTERS = ("JBIG2Decode", "CCITTFaxDecode", "JPXDecode")


def _key(doc, xref, key):
    try:
        t, v = doc.xref_get_key(xref, key)
        return None if t == "null" else v
    except Exception:
        return None


def _raw_len(doc, xref) -> int:
    """Stored (compressed) stream length, read from /Length without loading the stream."""
    v = _key(doc, xref, "Length")
    try:
        if v and v.endswith(" R"):
            v = doc.xref_object(int(v.split()[0])).strip()
        return int(float(v))
    except Exception:
        try:
            return len(doc.xref_stream_raw(xref) or b"")
        except Exception:
            return 0


def analyze(data: bytes) -> dict:
    doc = pymupdf.open(stream=data, filetype="pdf")
    cats = dict(images=0, fonts=0, content=0, forms=0, metadata=0, other=0)
    font_files, contents, images = set(), set(), []
    for x in range(1, doc.xref_length()):
        if _key(doc, x, "Type") == "/FontDescriptor":
            for k in ("FontFile", "FontFile2", "FontFile3"):
                v = _key(doc, x, k)
                if v and v.endswith(" R"):
                    font_files.add(int(v.split()[0]))
    pages = []
    for p in doc:
        pages.append((p.rect.width, p.rect.height))
        for c in p.get_contents():
            contents.add(c)
    for x in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_stream(x):
                continue
        except Exception:
            continue
        n = _raw_len(doc, x)
        st, ty = _key(doc, x, "Subtype"), _key(doc, x, "Type")
        if st == "/Image":
            cats["images"] += n
            images.append(dict(xref=x, w=int(_key(doc, x, "Width") or 0), h=int(_key(doc, x, "Height") or 0),
                               bytes=n, filter=(_key(doc, x, "Filter") or "none")))
        elif x in font_files:
            cats["fonts"] += n
        elif x in contents:
            cats["content"] += n
        elif st == "/Form":
            cats["forms"] += n
        elif ty == "/Metadata":
            cats["metadata"] += n
        else:
            cats["other"] += n
    images.sort(key=lambda i: -i["bytes"])
    total = len(data)
    rec = "prepress" if cats["images"] > total * 0.25 and any(i["bytes"] > 200_000 for i in images) else "lossless"
    doc.close()
    return dict(total=total, pages=pages, cats=cats, images=images[:50], image_count=len(images), recommended=rec)


def _placements(doc) -> dict[int, tuple[float, float]]:
    """xref -> largest placed size (width_pt, height_pt) across all pages."""
    placed: dict[int, tuple[float, float]] = {}
    for page in doc:
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                rects = page.get_image_rects(xref, transform=True)
            except Exception:
                continue
            for r, m in rects:
                w, h = math.hypot(m.a, m.b), math.hypot(m.c, m.d)
                pw, ph = placed.get(xref, (0.0, 0.0))
                placed[xref] = (max(pw, w), max(ph, h))
    return placed


def _is_line_art(arr: np.ndarray) -> bool:
    """Few distinct colours, or mostly one background colour (paper) -> keep lossless."""
    flat = arr.reshape(-1, arr.shape[2]) if arr.ndim == 3 else arr.reshape(-1, 1)
    step = max(1, flat.shape[0] // 60000)
    s = (flat[::step] >> 2).astype(np.int64)
    codes = s[:, 0] if s.shape[1] == 1 else (s[:, 0] << 12) | (s[:, 1] << 6) | s[:, 2]
    _, counts = np.unique(codes, return_counts=True)
    return len(counts) <= 48 or counts.max() / codes.size >= 0.6


@dataclass
class Report:
    images_total: int = 0
    recompressed: int = 0
    downsampled: int = 0
    kept_lossless: int = 0
    skipped: int = 0
    notes: list[str] = field(default_factory=list)


def compress(data: bytes, images=True, dpi=300, quality=92, strip_editor=True,
             strip_metadata=False, subset_fonts=False, log=lambda m: None, **_) -> tuple[bytes, Report]:
    doc = pymupdf.open(stream=data, filetype="pdf")
    rep = Report()

    if strip_editor or strip_metadata:
        for page in doc:
            if strip_editor:
                for k in ("Thumb", "PieceInfo"):
                    doc.xref_set_key(page.xref, k, "null")
        root = doc.pdf_catalog()
        if strip_editor:
            doc.xref_set_key(root, "PieceInfo", "null")
        if strip_metadata:
            doc.del_xml_metadata()
            doc.set_metadata({})

    if images and dpi > 0:
        log("Measuring where each image is placed on the page…")
        placed = _placements(doc)
        masks = set()
        for x in range(1, doc.xref_length()):
            if _key(doc, x, "Subtype") == "/Image":
                for k in ("SMask", "Mask"):
                    v = _key(doc, x, k)
                    if v and v.endswith(" R"):
                        masks.add(int(v.split()[0]))
        for x in range(1, doc.xref_length()):
            if _key(doc, x, "Subtype") != "/Image":
                continue
            rep.images_total += 1
            filt = _key(doc, x, "Filter") or ""
            cs = _key(doc, x, "ColorSpace") or ""
            bpc = int(_key(doc, x, "BitsPerComponent") or 0)
            if (x in masks or _key(doc, x, "ImageMask") == "true" or bpc == 1 or "Indexed" in cs
                    or "Separation" in cs or "DeviceN" in cs or any(f in filt for f in SKIP_FILTERS)):
                rep.kept_lossless += 1
                continue
            before = _raw_len(doc, x)
            if before < 20_000 or x not in placed:
                rep.skipped += 1
                continue
            try:
                pix = pymupdf.Pixmap(doc, x)
            except Exception:
                rep.skipped += 1
                continue
            pymupdf.TOOLS.store_shrink(100)  # keep only our own copy of the pixels
            if pix.alpha:
                pix = pymupdf.Pixmap(pix, 0)
            if pix.n not in (1, 3):
                rep.kept_lossless += 1
                continue
            W, H = pix.width, pix.height
            pw, ph = placed[x]
            eff = min(W / (pw / 72), H / (ph / 72)) if pw and ph else 0
            need_down = eff > dpi * 1.3
            is_jpeg = "DCTDecode" in filt
            if is_jpeg and not need_down:
                rep.skipped += 1
                continue
            tw, th = W, H
            if need_down:
                s = dpi / eff
                tw, th = max(1, round(W * s)), max(1, round(H * s))
                # cheap in-place 2^k box shrink first (keeps >= target), saves lots of memory
                k = int(math.floor(math.log2(W / tw))) - 1
                if k >= 1:
                    pix.shrink(k)
            pn, pw_, ph_ = pix.n, pix.width, pix.height
            arr = np.frombuffer(pix.samples_mv, dtype=np.uint8).reshape(ph_, pw_, pn)
            if _is_line_art(arr):
                rep.kept_lossless += 1
                del arr, pix
                pymupdf.TOOLS.store_shrink(100)
                gc.collect()
                continue
            im = Image.frombuffer("L" if pn == 1 else "RGB", (pw_, ph_), pix.samples_mv, "raw", "L" if pn == 1 else "RGB", 0, 1) if pix.stride == pw_ * pn else Image.fromarray(arr if pn == 3 else arr[:, :, 0])
            if (tw, th) != (pw_, ph_):
                # reduce in strips' worth of memory: integer pre-reduce, then Lanczos
                f = min(pw_ // tw, ph_ // th)
                if f >= 2:
                    im = im.reduce(f)
                im = im.resize((tw, th), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True, subsampling=0 if quality >= 90 else 2)
            jpg = buf.getvalue()
            del im, arr, buf
            gc.collect()
            if len(jpg) > before * 0.9:
                rep.skipped += 1
                continue
            n_out = pix.n
            del pix
            pymupdf.TOOLS.store_shrink(100)  # drop MuPDF's decoded-image cache
            doc.update_stream(x, jpg, compress=0)
            doc.xref_set_key(x, "Filter", "/DCTDecode")
            doc.xref_set_key(x, "DecodeParms", "null")
            doc.xref_set_key(x, "Decode", "null")
            doc.xref_set_key(x, "Width", str(tw))
            doc.xref_set_key(x, "Height", str(th))
            doc.xref_set_key(x, "BitsPerComponent", "8")
            if not cs:
                doc.xref_set_key(x, "ColorSpace", "/DeviceGray" if n_out == 1 else "/DeviceRGB")
            rep.recompressed += 1
            rep.downsampled += int(need_down)
            rep.notes.append(f"img #{x}: {W}×{H} @{eff:.0f}dpi → {tw}×{th}, {before // 1024} KB → {len(jpg) // 1024} KB")

    if subset_fonts:
        try:
            log("Subsetting fonts…")
            doc.subset_fonts()
        except Exception as e:
            rep.notes.append(f"Font subsetting skipped: {e}")

    pymupdf.TOOLS.store_shrink(100)
    gc.collect()
    log("Rebuilding file: dedupe objects, recompress streams, object streams…")
    out = doc.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                      use_objstms=1, compression_effort=100, clean=False)
    doc.close()
    return out, rep


def verify(a: bytes, b: bytes, max_pages=40, dpi=72) -> dict:
    da, db = pymupdf.open(stream=a, filetype="pdf"), pymupdf.open(stream=b, filetype="pdf")
    if len(da) != len(db):
        return dict(ok=False, error="Page count changed", pages=[], checked=0, total=len(da))
    res = []
    n = min(len(da), max_pages)
    for i in range(n):
        side = max(da[i].rect.width, da[i].rect.height)
        z = min(dpi / 72, 1600 / side)
        m = pymupdf.Matrix(z, z)
        xa = np.frombuffer(da[i].get_pixmap(matrix=m, colorspace=pymupdf.csGRAY).samples, np.uint8).astype(np.int16)
        xb = np.frombuffer(db[i].get_pixmap(matrix=m, colorspace=pymupdf.csGRAY).samples, np.uint8).astype(np.int16)
        if xa.shape != xb.shape:
            res.append(dict(page=i + 1, psnr=0.0, changed=1.0))
            continue
        d = xa - xb
        mse = float(np.mean(d.astype(np.float64) ** 2))
        res.append(dict(page=i + 1, psnr=999.0 if mse == 0 else 10 * math.log10(65025 / mse),
                        changed=float(np.mean(np.abs(d) > 24))))
    total = len(da)
    da.close(); db.close()
    gc.collect()
    return dict(ok=True, pages=res, checked=n, total=total)


def render_page(data: bytes, page: int, max_px=1200) -> tuple[bytes, float, float]:
    doc = pymupdf.open(stream=data, filetype="pdf")
    p = doc[page]
    z = min(max_px / p.rect.width, max_px / p.rect.height, 3)
    png = p.get_pixmap(matrix=pymupdf.Matrix(z, z)).tobytes("png")
    w, h = p.rect.width, p.rect.height
    doc.close()
    return png, w, h


def render_region(data: bytes, page: int, fx: float, fy: float, dpi: int, px=600) -> bytes:
    """Render a px×px window centred at fraction (fx, fy) of the page, at `dpi`."""
    doc = pymupdf.open(stream=data, filetype="pdf")
    p = doc[page]
    side = px / dpi * 72
    r = p.rect
    x0 = min(max(r.x0, r.x0 + fx * r.width - side / 2), r.x1 - side)
    y0 = min(max(r.y0, r.y0 + fy * r.height - side / 2), r.y1 - side)
    clip = pymupdf.Rect(max(r.x0, x0), max(r.y0, y0), max(r.x0, x0) + side, max(r.y0, y0) + side) & r
    png = p.get_pixmap(matrix=pymupdf.Matrix(dpi / 72, dpi / 72), clip=clip).tobytes("png")
    doc.close()
    return png
