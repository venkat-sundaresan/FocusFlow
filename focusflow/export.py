"""CSV, JSON and PDF export of sessions and pointer statistics. The PDF writer is self-contained (standard
Helvetica fonts, no third-party library) so exporting works on a fresh install."""
import csv
import io
import json
import time
from pathlib import Path

from .store import SESSION_FIELDS

CSV_COLUMNS = ("id", "started", "mode", "task", "profile", "completed", "seconds", "raw_distance",
               "stabilised_distance", "raw_changes", "stabilised_changes", "raw_corrective",
               "stabilised_corrective", "successes", "incorrect", "near_misses", "accidental", "erratic_flags",
               "targets")


def _stamp(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def sessions_csv(sessions):
    buf = io.StringIO()
    wr = csv.writer(buf, lineterminator="\n")
    wr.writerow(CSV_COLUMNS)
    for s in sessions:
        wr.writerow([_stamp(s["ts_start"]) if c == "started" else s.get(c, "") for c in CSV_COLUMNS])
    return buf.getvalue()


def bundle(store, demo=False):
    """Everything exportable, as plain data."""
    return {
        "app": "FocusFlow", "exported": _stamp(time.time()), "demo_data": bool(demo),
        "note": "All values were measured from real Windows mouse input and recorded task clicks."
                if not demo else "DEMO DATA - generated samples, not real measurements.",
        "sessions": store.sessions(1000, demo),
        "mode_averages": store.mode_summary(demo),
        "pointer_totals": store.pointer_totals(0.0, demo),
        "event_totals": store.stats(demo),
    }


def bundle_json(store, demo=False):
    return json.dumps(bundle(store, demo), indent=2, default=str)


def export_all(store, folder, fmt, demo=False):
    """Write one export file into `folder`; returns its Path."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stem = "focusflow-" + time.strftime("%Y%m%d-%H%M%S") + ("-DEMO" if demo else "")
    path = folder / f"{stem}.{fmt}"
    if fmt == "csv":
        path.write_text(sessions_csv(store.sessions(1000, demo)), encoding="utf-8")
    elif fmt == "json":
        path.write_text(bundle_json(store, demo), encoding="utf-8")
    elif fmt == "pdf":
        path.write_bytes(report_pdf(bundle(store, demo)))
    else:
        raise ValueError(fmt)
    return path


# ---- PDF ---------------------------------------------------------------------------------------------------
def _n(value, spec=".0f"):
    """Format a possibly-missing number: stored rows can have NULL fields, and a report must never crash on them."""
    return "-" if value is None else format(value, spec)


def _esc(text):
    text = str(text).encode("cp1252", "replace").decode("cp1252")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _Page:
    W, H = 595, 842

    def __init__(self):
        self.ops = []

    def text(self, x, y, s, size=10, bold=False, color=(0.1, 0.1, 0.12)):
        self.ops.append(f"{color[0]} {color[1]} {color[2]} rg BT /{'F2' if bold else 'F1'} {size} Tf "
                        f"{x:.1f} {y:.1f} Td ({_esc(s)}) Tj ET")

    def rect(self, x, y, w, h, color):
        self.ops.append(f"{color[0]} {color[1]} {color[2]} rg {x:.1f} {y:.1f} {max(w, 0):.1f} {h:.1f} re f")

    def line(self, x1, y1, x2, y2, color=(0.8, 0.8, 0.84)):
        self.ops.append(f"{color[0]} {color[1]} {color[2]} RG 0.6 w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def stream(self):
        return "\n".join(self.ops).encode("latin-1")


def report_pdf(data):
    """A4 report: summary, per-mode comparison with bars, then session history."""
    pages = [_Page()]
    y = [800.0]

    def need(h):
        if y[0] - h < 50:
            pages.append(_Page())
            y[0] = 800.0
        return pages[-1]

    def heading(s, size=14):
        pg = need(size + 14)
        y[0] -= size + 8
        pg.text(48, y[0], s, size, True)
        y[0] -= 4

    def para(s, size=9.5, color=(0.25, 0.27, 0.3)):
        pg = need(size + 6)
        y[0] -= size + 4
        pg.text(48, y[0], s, size, False, color)

    pg = pages[0]
    pg.rect(0, 790, _Page.W, 52, (0.07, 0.09, 0.11))
    pg.text(48, 810, "FocusFlow performance report", 18, True, (1, 1, 1))
    pg.text(48, 796, "Generated " + data["exported"], 9, False, (0.7, 0.75, 0.8))
    y[0] = 780
    para(data["note"], 9, (0.7, 0.1, 0.1) if data["demo_data"] else (0.25, 0.27, 0.3))

    pt = data["pointer_totals"]
    heading("Everyday pointer use")
    if pt["n"]:
        saved = 1 - pt["stabilised_distance"] / pt["raw_distance"] if pt["raw_distance"] else 0
        para(f"Recorded over {pt['seconds'] / 60:.0f} minutes of active use in {pt['n']} one-minute samples.")
        para(f"Pointer distance: raw {pt['raw_distance']:.0f} px, stabilised {pt['stabilised_distance']:.0f} px "
             f"({saved * 100:.0f}% less travel).")
        para(f"Direction changes: raw {pt['raw_changes']}, stabilised {pt['stabilised_changes']}.  "
             f"Corrective movements: raw {pt['raw_corrective']}, stabilised {pt['stabilised_corrective']}.")
    else:
        para("No everyday pointer statistics recorded yet.")

    ev = data["event_totals"]
    heading("Assisted clicking")
    para(f"{ev['total_assisted']} assisted clicks ({ev['dwell_clicks']} dwell).  "
         f"{ev['near_miss_detected']} near-misses detected, {ev['near_miss_corrected']} corrected.  "
         f"{ev['prevented']} accidental clicks prevented.")

    heading("Experiment comparison (average per completed session)")
    modes = data["mode_averages"]
    if not modes:
        para("No completed experiment sessions yet. Run the Live Lab task in each mode to compare them.")
    else:
        cols = [("Mode", 48), ("Runs", 200), ("Time s", 236), ("Raw px", 284), ("Stab px", 334), ("Dir chg", 384),
                ("Corr", 430), ("Near", 468), ("Wrong", 504), ("Accid", 545)]
        pg = need(20)
        y[0] -= 14
        for name, x in cols:
            pg.text(x, y[0], name, 8, True)
        pg.line(48, y[0] - 3, 560, y[0] - 3)
        top = max((m["raw_distance"] or 0 for m in modes.values()), default=1) or 1
        for mode, m in modes.items():
            pg = need(16)
            y[0] -= 13
            vals = [mode[:26], m["n"], _n(m["seconds"], ".1f"), _n(m["raw_distance"]),
                    _n(m["stabilised_distance"]), _n(m["stabilised_changes"]),
                    _n(m["stabilised_corrective"]), _n(m["near_misses"], ".1f"), _n(m["incorrect"], ".1f"),
                    _n(m["accidental"], ".1f")]
            for (name, x), v in zip(cols, vals):
                pg.text(x, y[0], v, 8)
        heading("Stabilised pointer distance by mode (shorter bar = steadier pointer)", 10)
        for mode, m in modes.items():
            pg = need(18)
            y[0] -= 14
            pg.text(48, y[0], mode[:30], 8)
            pg.rect(200, y[0] - 2, 330 * (m["raw_distance"] or 0) / top, 4, (0.8, 0.82, 0.86))
            pg.rect(200, y[0] + 2, 330 * (m["stabilised_distance"] or 0) / top, 5, (0.0, 0.62, 0.45))
        para("Grey = raw input distance, green = distance of the pointer you actually used.", 8)

    heading("Session history (most recent first)")
    sess = data["sessions"][:40]
    if not sess:
        para("No sessions recorded.")
    for s in sess:
        para(f"{_stamp(s['ts_start'])}  {s['mode']}  {_n(s['seconds'], '.1f')}s  ok {_n(s['successes'])}/{_n(s['targets'])}  "
             f"wrong {_n(s['incorrect'])}  near {_n(s['near_misses'])}  accidental {_n(s['accidental'])}  "
             f"raw {_n(s['raw_distance'])}px -> {_n(s['stabilised_distance'])}px", 8)
    para("Movement flags describe measured behaviour only; they are not a medical assessment.", 8)
    return _assemble(pages)


def _assemble(pages):
    objs = []                                       # 1-indexed object bodies (bytes)

    def add(body):
        objs.append(body if isinstance(body, bytes) else body.encode("latin-1"))
        return len(objs)

    cat = add("")                                   # 1 catalog, 2 pages: patched below
    pages_id = add("")
    f1 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    f2 = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    for p in pages:
        data = p.stream()
        cid = add(b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")
        pid = add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {_Page.W} {_Page.H}] "
                  f"/Resources << /Font << /F1 {f1} 0 R /F2 {f2} 0 R >> >> /Contents {cid} 0 R >>")
        kids.append(pid)
    objs[cat - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode()
    objs[pages_id - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                          f"/Count {len(kids)} >>").encode()
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, xref)
    return bytes(out)
