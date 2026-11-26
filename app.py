import io
import zipfile

import streamlit as st
from PIL import Image, ImageDraw

import pdfslim

st.set_page_config(page_title="PDF Slim — shrink PDFs without losing detail", page_icon="📄", layout="wide")

st.markdown("""
<style>
.block-container{padding-top:2rem;max-width:1150px}
.bar{display:flex;height:10px;border-radius:5px;overflow:hidden;background:#e3e0d8;margin:4px 0 6px}
.bar span{display:block;height:100%}
.legend{font:12px ui-monospace,Consolas,monospace;opacity:.75;display:flex;flex-wrap:wrap;gap:4px 14px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.agent{border-left:3px solid #c2410c;padding:6px 0 6px 12px;margin:10px 0}
</style>""", unsafe_allow_html=True)

def fmt(b: float) -> str:
    return f"{b / 1048576:.2f} MB" if b >= 1048576 else f"{b / 1024:.1f} KB"

# ---------------- sidebar settings ----------------
with st.sidebar:
    st.header("Settings")
    mode = st.radio("Quality mode", ["auto", "lossless", "prepress", "print", "custom"], format_func=lambda m: {
        "auto": "Auto (recommended)", "lossless": "Lossless", "prepress": "Prepress 300 DPI",
        "print": "Print 200 DPI", "custom": "Custom"}[m])
    st.caption({
        "auto": "Reads each file and picks Lossless for vector drawings, or Prepress 300 DPI when oversized photos make up most of the file.",
        "lossless": "Repacks the file only. Every page renders identically.",
        "prepress": "Photos above 300 DPI at their printed size → 300 DPI, JPEG quality 92.",
        "print": "Photos above 200 DPI → 200 DPI, JPEG quality 85.",
        "custom": "Your own DPI and quality. Text, vectors and line art still stay lossless.",
    }[mode])
    c_dpi, c_q = 300, 90
    if mode == "custom":
        c_dpi = st.number_input("Target DPI", 72, 1200, 300, 25)
        c_q = st.slider("JPEG quality", 60, 98, 90)
    st.divider()
    st.subheader("Clean-up")
    strip_editor = st.checkbox("Remove editor data & thumbnails", True, help="Invisible data such as Illustrator PieceInfo and page thumbnails.")
    strip_meta = st.checkbox("Remove XMP metadata", False)
    subset = st.checkbox("Subset embedded fonts (experimental)", False)
    st.divider()
    st.caption("Built on [PyMuPDF](https://pymupdf.readthedocs.io/) · Text and vectors are never rasterised.")

st.title("📄 PDF Slim")
st.write("Shrink PDFs without losing text, line work or detail. Upload drawings, reports or scans; several at once is fine.")

files = st.file_uploader("Drop PDFs here", type=["pdf"], accept_multiple_files=True, label_visibility="collapsed")


@st.cache_data(show_spinner=False, max_entries=20)
def run_analysis(data: bytes):
    return pdfslim.analyze(data)


@st.cache_data(show_spinner=False, max_entries=20)
def run_compress(data: bytes, opts: tuple):
    o = dict(opts)
    out, rep = pdfslim.compress(data, **o)
    kept = len(out) >= len(data)
    if kept:
        out = data
    v = {"pages": [], "checked": 0, "total": 0} if kept else pdfslim.verify(data, out)
    return out, rep.__dict__, v, kept


CATS = [("images", "Images", "#c2410c"), ("fonts", "Fonts", "#2563eb"), ("content", "Page drawing", "#0f766e"),
        ("forms", "Blocks / forms", "#7c3aed"), ("metadata", "Metadata", "#a16207"), ("other", "Other", "#9ca3af")]


def explain(a):
    pct = lambda k: round(100 * a["cats"][k] / a["total"])
    w, h = a["pages"][0]
    s = f"**{len(a['pages'])} page{'s' if len(a['pages']) > 1 else ''}** ({w / 72 * 25.4:.0f}×{h / 72 * 25.4:.0f} mm). "
    if a["recommended"] == "prepress":
        big = a["images"][0]
        s += (f"Raster images make up **{pct('images')}%** of the file ({a['image_count']} images, largest "
              f"{big['w']}×{big['h']} px, {fmt(big['bytes'])}). Recommendation: **Prepress 300 DPI**. Oversized photos get "
              f"resampled to 300 DPI at their printed size. Line art and all vector content stay exact.")
    else:
        vec = pct("content") + pct("forms") + pct("fonts")
        s += (f"Mostly vector content and fonts ({vec}%). Recommendation: **Lossless**. The saving comes from re-packing "
              f"the file, so the output is pixel-identical to the original.")
    return s


results = []
for f in files or []:
    data = f.getvalue()
    with st.container(border=True):
        head = st.columns([4, 1])
        head[0].markdown(f"**{f.name}**")
        head[1].markdown(f"<div style='text-align:right;opacity:.7'>{fmt(len(data))}</div>", unsafe_allow_html=True)
        try:
            with st.spinner("Reading file structure…"):
                a = run_analysis(data)
        except Exception as e:
            st.error(f"Couldn't open this PDF ({e}). If it's password-protected, remove the password first.")
            continue

        tot = sum(a["cats"].values()) or 1
        bar = "".join(f"<span style='width:{100 * a['cats'][k] / tot}%;background:{c}'></span>" for k, _, c in CATS if a["cats"][k])
        leg = "".join(f"<span><i style='background:{c}'></i>{n} {fmt(a['cats'][k])}</span>" for k, n, c in CATS if a["cats"][k])
        st.markdown(f"<div class='bar'>{bar}</div><div class='legend'>{leg}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='agent'>\n\n{explain(a)}\n\n</div>", unsafe_allow_html=True)

        chosen = a["recommended"] if mode == "auto" else mode
        base = {"images": True, "dpi": int(c_dpi), "quality": int(c_q), "label": f"Custom {c_dpi} DPI / Q{c_q}"} \
            if chosen == "custom" else pdfslim.PRESETS[chosen]
        opts = (("images", base["images"]), ("dpi", base["dpi"]), ("quality", base["quality"]),
                ("strip_editor", strip_editor), ("strip_metadata", strip_meta), ("subset_fonts", subset))
        try:
            with st.spinner(f"Compressing with {base['label']} and checking every page…"):
                out, rep, v, kept = run_compress(data, opts)
        except Exception as e:
            st.error(f"Compression failed: {e}")
            continue

        saved = 1 - len(out) / len(data)
        m1, m2, m3 = st.columns([1.1, 2.2, 1])
        m1.metric("New size", fmt(len(out)), f"-{saved * 100:.1f}%" if saved > 0.0005 else "no change", delta_color="inverse")
        if kept:
            m2.success("**Unchanged.** This file is already well optimised, so you get the original back byte for byte.")
        else:
            min_p = min(p["psnr"] for p in v["pages"])
            chg = max(p["changed"] for p in v["pages"])
            if min_p >= 999:
                m2.success(f"**Pixel-identical.** All {v['checked']}/{v['total']} checked pages render exactly like the original.")
            elif min_p >= 40:
                m2.success(f"**Visually identical.** Worst page PSNR {min_p:.1f} dB; {chg * 100:.3f}% of pixels changed noticeably. "
                           f"Checked {v['checked']}/{v['total']} pages.")
            else:
                m2.warning(f"**Check detail.** Worst page PSNR {min_p:.1f} dB. Try Prepress or Lossless.")
        name = f.name.rsplit(".", 1)[0] + "-slim.pdf"
        m3.download_button("⬇ Download", out, file_name=name, mime="application/pdf", type="primary", use_container_width=True, key=f"dl-{f.file_id}")
        m3.caption(f"Mode: {base['label']}" + (" (auto)" if mode == "auto" else ""))
        results.append((name, out, len(data)))

        with st.expander("What changed"):
            st.code("\n".join([
                f"Images found: {rep['images_total']}",
                f"Resampled/re-encoded: {rep['recompressed']} (downsampled {rep['downsampled']})",
                f"Kept lossless (line art, masks, bitonal, palette): {rep['kept_lossless']}",
                f"Left as-is (already efficient or small): {rep['skipped']}", *rep["notes"]]), language=None)

        if not kept:
            with st.expander("🔍 Compare detail: zoom into both files side by side"):
                c = st.columns(4)
                pg = c[0].selectbox("Page", range(len(a["pages"])), format_func=lambda i: str(i + 1), key=f"pg-{f.file_id}")
                dpi = c[1].selectbox("Zoom", [150, 300, 600], index=1, format_func=lambda d: f"{d} DPI", key=f"z-{f.file_id}")
                fx = c[2].slider("Across (%)", 0, 100, 50, key=f"x-{f.file_id}") / 100
                fy = c[3].slider("Down (%)", 0, 100, 50, key=f"y-{f.file_id}") / 100
                ov_png, pw, ph = pdfslim.render_page(out, pg, 900)
                ov = Image.open(io.BytesIO(ov_png)).convert("RGB")
                side = 600 / dpi * 72
                sx, sy = ov.width / pw, ov.height / ph
                cx, cy = fx * pw, fy * ph
                x0 = max(0, min(pw - side, cx - side / 2)); y0 = max(0, min(ph - side, cy - side / 2))
                ImageDraw.Draw(ov).rectangle([x0 * sx, y0 * sy, (x0 + side) * sx, (y0 + side) * sy], outline=(194, 65, 12), width=3)
                v1, v2, v3 = st.columns([1.2, 1, 1])
                v1.image(ov, caption="Page (orange box = area shown)", use_container_width=True)
                v2.image(pdfslim.render_region(data, pg, fx, fy, dpi), caption="Original", use_container_width=True)
                v3.image(pdfslim.render_region(out, pg, fx, fy, dpi), caption="Compressed", use_container_width=True)

if len(results) > 1:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n, b, _ in results:
            z.writestr(n, b)
    before = sum(r[2] for r in results); after = sum(len(r[1]) for r in results)
    st.download_button(f"⬇ Download all ({len(results)} files, {fmt(before)} → {fmt(after)})", buf.getvalue(),
                       file_name="pdf-slim.zip", mime="application/zip", type="primary")

if not files:
    with st.expander("How it keeps the detail", expanded=True):
        st.markdown("""
- **Text, vector lines, hatching and fonts are never converted to images.** They're only repacked: duplicate objects merged, unused ones removed, streams recompressed at maximum effort, objects packed into object streams.
- **Raster images are measured where they're placed on the page.** Only photos that exceed the target DPI *at their printed size* are resampled (Lanczos), then saved as high-quality JPEG.
- **Line-art images stay pixel-exact.** Scanned drawings, black-and-white images, masks, palette images and logos are never JPEG'd.
- **Every result is checked.** Each page of the original and the output is rendered and compared (PSNR), and you can zoom into any area of both side by side at up to 600 DPI.
- If a file can't get smaller, you get the original back unchanged.
""")
