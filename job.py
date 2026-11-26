"""Isolated worker: runs one PDF job in its own process so a crash or memory
spike never takes down the Streamlit server.

Usage: python job.py <in.pdf> <out.pdf> <result.json> '<opts json>'
"""
import json
import sys

import pdfslim


def main():
    src, dst, res, opts = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
    with open(src, "rb") as f:
        data = f.read()
    a = pdfslim.analyze(data)
    out = None
    rep = None
    v = {"pages": [], "checked": 0, "total": 0}
    kept = True
    if opts.get("run", True):
        o = {k: opts[k] for k in ("images", "dpi", "quality", "strip_editor", "strip_metadata", "subset_fonts")}
        if opts.get("mode") == "auto":
            p = pdfslim.PRESETS[a["recommended"]]
            o.update(images=p["images"], dpi=p["dpi"], quality=p["quality"])
        out_bytes, r = pdfslim.compress(data, **o)
        rep = r.__dict__
        kept = len(out_bytes) >= len(data)
        if kept:
            out_bytes = data
        else:
            v = pdfslim.verify(data, out_bytes)
        with open(dst, "wb") as f:
            f.write(out_bytes)
        out = len(out_bytes)
    with open(res, "w") as f:
        json.dump({"analysis": a, "report": rep, "verify": v, "kept": kept, "size": out}, f)


if __name__ == "__main__":
    main()
