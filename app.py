import hmac
import io
import json
import os
import subprocess
import sys
import tempfile
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

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(tempfile.gettempdir(), "pdfslim")
os.makedirs(WORK, exist_ok=True)


# ---------------- password gate ----------------
def check_password() -> bool:
    """Team password gate. Password lives in Streamlit Secrets as APP_PASSWORD."""
    if st.session_state.get("auth_ok"):
        return True
    try:
        expected = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        expected = ""
    if not expected:
        st.error("Password not configured. Add APP_PASSWORD in the app's Settings → Secrets.")
        return False
    st.title("📄 PDF Slim")
    st.caption("Team access. Enter the password to continue.")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        ok = st.form_submit_button("Enter", type="primary")
    if ok:
        if hmac.compare_digest(pw.encode(), str(expected).encode()):
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("Wrong password.")
    return False


if not check_password():
    st.stop()


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
    if st.button("🔒 Log out"):
        st.session_state.clear()
        st.rerun()
    st.caption("Built on [PyMuPDF](https://pymupdf.readthedocs.io/) · Text and vectors are never rasterised.")

st.title("📄 PDF Slim")
st.write("Shrink PDFs without losing text, line work or detail. Upload drawings, reports or scans; several at once is fine.")

files = st.file_uploader("Drop PDFs here", type=["pdf"], accept_multiple_files=True, label_visibility="collapsed")

if mode in ("auto", "lossless", "prepress", "print"):
    base = pdfslim.PRESETS["prepress" if mode == "auto" else mode]
    label = "Auto" if mode == "auto" else base["label"]
else:
    base = {"images": True, "dpi": int(c_dpi), "quality": int(c_q)}
    label = f"Custom {c_dpi} DPI / Q{c_q}"
opts = {"mode": mode, "images": base["images"], "dpi": base["dpi"], "quality": base["quality"],
        "strip_editor": strip_editor, "strip_metadata": strip_meta, "subset_fonts": subset}
opts_key = json.dumps(opts, sort_keys=True)


def run_job(file_id: str, data: bytes) -> dict:
    """Run compression in a separate process; return result dict or {'error': ...}."""
    jobs = st.session_state.setdefault("jobs", {})
    key = f"{file_id}|{opts_key}"
    if key in jobs:
        return jobs[key]
    # forget older results for this file to save memory
    for k in [k for k in jobs if k.startswith(file_id + "|")]:
        old = jobs.pop(k)
        if old.get("out_path") and os.path.exists(old["out_path"]):
            os.remove(old["out_path"])
    src = os.path.join(WORK, f"{file_id}.in.pdf")
    dst = os.path.join(WORK, f"{file_id}.{abs(hash(opts_key))}.out.pdf")
    res = os.path.join(WORK, f"{file_id}.json")
    with open(src, "wb") as f:
        f.write(data)
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "job.py"), src, dst, res, opts_key],
                           capture_output=True, text=True, timeout=900, cwd=HERE)
    except subprocess.TimeoutExpired:
        return {"error": "Took longer than 15 minutes. The file is too heavy for the free server."}
    if p.returncode != 0 or not os.path.exists(res):
        tail = (p.stderr or "").strip().splitlines()[-1:] or [f"exit code {p.returncode}"]
        killed = p.returncode in (-9, 137) or "MemoryError" in (p.stderr or "")
        msg = ("Ran out of memory on the free server. Try a smaller file, or split it into parts."
               if killed else f"Processing failed: {tail[0]}")
        return {"error": msg}
    with open(res) as f:
        r = json.load(f)
    os.remove(res)
    r["out_path"] = dst
    r["src_path"] = src
    jobs[key] = r
    return r


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
    fid = f.file_id
    size_in = f.size
    with st.container(border=True):
        head = st.columns([4, 1])
        head[0].markdown(f"**{f.name}**")
        head[1].markdown(f"<div style='text-align:right;opacity:.7'>{fmt(size_in)}</div>", unsafe_allow_html=True)

        with st.spinner(f"Compressing ({label}) and checking every page…"):
            r = run_job(fid, f.getvalue())
        if "error" in r:
            st.error(r["error"])
            continue
        a = r["analysis"]

        tot = sum(a["cats"].values()) or 1
        bar = "".join(f"<span style='width:{100 * a['cats'][k] / tot}%;background:{c}'></span>" for k, _, c in CATS if a["cats"][k])
        leg = "".join(f"<span><i style='background:{c}'></i>{n} {fmt(a['cats'][k])}</span>" for k, n, c in CATS if a["cats"][k])
        st.markdown(f"<div class='bar'>{bar}</div><div class='legend'>{leg}</div>", unsafe_allow_html=True)
        st.markdown(f"<div class='agent'>\n\n{explain(a)}\n\n</div>", unsafe_allow_html=True)

        kept, v, rep = r["kept"], r["verify"], r["report"]
        size_out = r["size"]
        saved = 1 - size_out / size_in
        m1, m2, m3 = st.columns([1.1, 2.2, 1])
        m1.metric("New size", fmt(size_out), f"-{saved * 100:.1f}%" if saved > 0.0005 else "no change", delta_color="inverse")
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
        with open(r["out_path"], "rb") as fh:
            m3.download_button("⬇ Download", fh.read(), file_name=name, mime="application/pdf", type="primary",
                               width="stretch", key=f"dl-{fid}")
        used = a["recommended"] if mode == "auto" else mode
        m3.caption(f"Mode: {pdfslim.PRESETS.get(used, {'label': label})['label']}" + (" (auto)" if mode == "auto" else ""))
        results.append((name, r["out_path"], size_in, size_out))

        with st.expander("What changed"):
            st.code("\n".join([
                f"Images found: {rep['images_total']}",
                f"Resampled/re-encoded: {rep['recompressed']} (downsampled {rep['downsampled']})",
                f"Kept lossless (line art, masks, bitonal, palette): {rep['kept_lossless']}",
                f"Left as-is (already efficient or small): {rep['skipped']}", *rep["notes"]]), language=None)

        if not kept and st.toggle("🔍 Compare detail: zoom into both files side by side", key=f"cmp-{fid}"):
            c = st.columns(4)
            pg = c[0].selectbox("Page", range(len(a["pages"])), format_func=lambda i: str(i + 1), key=f"pg-{fid}")
            dpi = c[1].selectbox("Zoom", [150, 300, 600], index=1, format_func=lambda d: f"{d} DPI", key=f"z-{fid}")
            fx = c[2].slider("Across (%)", 0, 100, 50, key=f"x-{fid}") / 100
            fy = c[3].slider("Down (%)", 0, 100, 50, key=f"y-{fid}") / 100
            try:
                with open(r["out_path"], "rb") as fh:
                    out_bytes = fh.read()
                with open(r["src_path"], "rb") as fh:
                    src_bytes = fh.read()
                ov_png, pw, ph = pdfslim.render_page(out_bytes, pg, 900)
                ov = Image.open(io.BytesIO(ov_png)).convert("RGB")
                side = 600 / dpi * 72
                sx, sy = ov.width / pw, ov.height / ph
                x0 = max(0, min(pw - side, fx * pw - side / 2)); y0 = max(0, min(ph - side, fy * ph - side / 2))
                ImageDraw.Draw(ov).rectangle([x0 * sx, y0 * sy, (x0 + side) * sx, (y0 + side) * sy], outline=(194, 65, 12), width=3)
                v1, v2, v3 = st.columns([1.2, 1, 1])
                v1.image(ov, caption="Page (orange box = area shown)", width="stretch")
                v2.image(pdfslim.render_region(src_bytes, pg, fx, fy, dpi), caption="Original", width="stretch")
                v3.image(pdfslim.render_region(out_bytes, pg, fx, fy, dpi), caption="Compressed", width="stretch")
                del out_bytes, src_bytes
            except Exception as e:
                st.warning(f"Couldn't render the comparison: {e}")

if len(results) > 1:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for n, path, _, _ in results:
            z.write(path, n)
    before = sum(r[2] for r in results); after = sum(r[3] for r in results)
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
