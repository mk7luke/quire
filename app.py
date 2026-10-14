"""Quire: write a booklet, see the sheet order, print a foldable PDF."""

from __future__ import annotations

import hashlib
import html
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
DB_PATH = DATA / "quire.db"
BIND_HOST = os.environ.get("QUIRE_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("QUIRE_PORT", "8883"))

TTL_SECONDS = 30 * 24 * 3600
MAX_PAGES = 64
QUIRE_SIZES = (4, 8, 12, 16)
PAPERS = {
    "letter": (612.0, 792.0),
    "a4": (595.27, 841.89),
}
PAPER_LABEL = {"letter": "US Letter", "a4": "A4"}
BODY_SIZE = 10.5
BODY_LEAD = 14.0
HEAD_SIZE = 13.0
HEAD_LEAD = 16.0
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"

_ID = re.compile(r"^[A-Za-z0-9_-]{4,24}$")
_KEY = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_TOKEN = re.compile(r"(<!--[A-Z0-9_]+-->)")
_hits: dict[str, list[float]] = {}
_page_cache: dict[str, tuple[float, str]] = {}
_fonts_ready = False

SAMPLE = [
    ("Fold this", "A quire is a stack of sheets, folded once, nested together. This booklet is one."),
    ("The outside", "The first sheet is the outside. Front cover on the right. Back cover on the left. On one sheet that is page 8 beside page 1."),
    ("Open it", "Flip the sheet on the short edge. The inside reads left to right: page 2, then page 7. The fold is the spine."),
    ("The center", "The last sheet is the middle. Its back is pages 4 and 5, side by side. That is where a staple goes."),
    ("Nest", "Put the center sheet inside the outside sheet. Fold both on the tick marks. The pages should run 1 through 8. If they do not, the flip edge was wrong."),
    ("Again", "Longer books use more quires. Print each stack, fold it, then sew the stacks in order. Do not staple forty pages through one fold. The fold will bulge."),
]


class QuireError(ValueError):
    pass


def _now() -> int:
    return int(time.time())


def _client_ip(request: Request) -> str:
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()[:64]
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()[:64]
    if request.client and request.client.host:
        return request.client.host
    return "local"


def _rate_ok(ip: str, bucket: str, limit: int, window: int = 3600) -> bool:
    now = time.time()
    key = f"{bucket}:{ip}"
    hits = [t for t in _hits.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _hits[key] = hits
        return False
    hits.append(now)
    _hits[key] = hits
    return True


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _clean(value: str, limit: int, label: str, required: bool = False) -> str:
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(lines).strip()
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if required and not text:
        raise QuireError(f"Need a {label}.")
    if len(text) > limit:
        raise QuireError(f"{label.capitalize()} is too long.")
    return text


def _paper(value: str) -> str:
    paper = (value or "letter").strip().lower()
    if paper not in PAPERS:
        raise QuireError("Paper must be letter or A4.")
    return paper


def _quire(value: str | int) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        raise QuireError("Quire must be 4, 8, 12, or 16.")
    if size not in QUIRE_SIZES:
        raise QuireError("Quire must be 4, 8, 12, or 16.")
    return size


def pad_count(written: int, quire: int) -> int:
    if written < 1:
        raise QuireError("Need at least one page.")
    if written > MAX_PAGES:
        raise QuireError("64 pages is the limit.")
    target = max(written, quire)
    rem = target % quire
    if rem:
        target += quire - rem
    if target > MAX_PAGES:
        raise QuireError("That quire size pads past 64 pages. Use a smaller quire or fewer pages.")
    return target


def waste_choice(written: int, quire: int) -> tuple[int, int, tuple[int, int] | None]:
    padded = pad_count(written, quire)
    blanks = padded - written
    better: tuple[int, int] | None = None
    for alt in QUIRE_SIZES:
        if alt == quire:
            continue
        try:
            alt_blanks = pad_count(written, alt) - written
        except QuireError:
            continue
        if alt_blanks < blanks and (better is None or alt_blanks < better[0]):
            better = (alt_blanks, alt)
    return padded, blanks, better


def impose_signature(first: int, count: int) -> list[dict[str, Any]]:
    """Saddle-stitch one signature.

    Front of sheet s is (last-2s | first+2s). Back is (first+2s+1 | last-2s-1).
    Print landscape, duplex, flip on the short edge. Classic 8-page chart:
    front 8|1, back 2|7, then front 6|3, back 4|5.
    """
    if count < 4 or count % 4:
        raise QuireError("A quire must be a multiple of 4 pages.")
    sheets = []
    for s in range(count // 4):
        front_left = first + (count - 2 * s) - 1
        front_right = first + (2 * s + 1) - 1
        back_left = first + (2 * s + 2) - 1
        back_right = first + (count - 2 * s - 1) - 1
        sheets.append({
            "index": s,
            "front": (front_left, front_right),
            "back": (back_left, back_right),
        })
    return sheets


def plan_book(written: int, quire: int) -> dict[str, Any]:
    padded = pad_count(written, quire)
    quires = []
    pdf_page = 1
    start = 1
    while start <= padded:
        sheets = impose_signature(start, quire)
        packed = []
        for sheet in sheets:
            role = "only sheet" if len(sheets) == 1 else "outside" if sheet["index"] == 0 else "center" if sheet["index"] == len(sheets) - 1 else "inner"
            packed.append({
                **sheet,
                "role": role,
                "pdf_front": pdf_page,
                "pdf_back": pdf_page + 1,
            })
            pdf_page += 2
        quires.append({
            "index": len(quires),
            "first": start,
            "last": start + quire - 1,
            "sheets": packed,
        })
        start += quire
    return {"written": written, "padded": padded, "quire": quire, "quires": quires, "pdf_pages": pdf_page - 1}


def page_role(pos: int, padded: int, quire: int) -> str:
    if pos == 1:
        return "Front cover"
    if pos == padded:
        return "Back cover"
    local = (pos - 1) % quire + 1
    if local == 1:
        return "Quire start"
    if local == quire:
        return "Quire end"
    if local in (quire // 2, quire // 2 + 1):
        return "Center"
    return ""


def ensure_fonts() -> tuple[str, str]:
    global _fonts_ready
    if _fonts_ready:
        return "Quire", "Quire-Bold"
    if Path(FONT_PATH).exists() and Path(FONT_BOLD).exists():
        pdfmetrics.registerFont(TTFont("Quire", FONT_PATH))
        pdfmetrics.registerFont(TTFont("Quire-Bold", FONT_BOLD))
        _fonts_ready = True
        return "Quire", "Quire-Bold"
    _fonts_ready = True
    return "Times-Roman", "Times-Bold"


def sheet_size(paper: str) -> tuple[float, float]:
    pw, ph = PAPERS[paper]
    return ph, pw


def wrap_text(text: str, width: float, size: float, font: str) -> list[str]:
    lines: list[str] = []
    for para in (text or "").split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for word in para.split():
            trial = word if not cur else f"{cur} {word}"
            if pdfmetrics.stringWidth(trial, font, size) <= width:
                cur = trial
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if pdfmetrics.stringWidth(word, font, size) <= width:
                cur = word
                continue
            buf = ""
            for ch in word:
                if pdfmetrics.stringWidth(buf + ch, font, size) <= width:
                    buf += ch
                else:
                    if buf:
                        lines.append(buf)
                    buf = ch
            cur = buf
        if cur:
            lines.append(cur)
    return lines


def text_box(paper: str) -> tuple[float, float]:
    sheet_w, sheet_h = sheet_size(paper)
    return sheet_w / 2 - 50, sheet_h - 78


def overflows(heading: str, body: str, paper: str) -> bool:
    font, bold = ensure_fonts()
    width, height = text_box(paper)
    used = 0.0
    if heading.strip():
        used += len(wrap_text(heading, width, HEAD_SIZE, bold)) * HEAD_LEAD + 6
    if body.strip():
        used += len(wrap_text(body, width, BODY_SIZE, font)) * BODY_LEAD
    return used > height


def _draw_ticks(c: canvas.Canvas, sheet_w: float, sheet_h: float) -> None:
    mid = sheet_w / 2
    c.setStrokeColorRGB(0.15, 0.12, 0.08)
    c.setLineWidth(0.8)
    c.line(mid, sheet_h - 2, mid, sheet_h - 12)
    c.line(mid, 2, mid, 12)


def _lookup(pages: dict[int, dict[str, str]], num: int) -> dict[str, str] | None:
    return pages.get(num)


def build_pdf(plan: dict[str, Any], pages: dict[int, dict[str, str]], paper: str, kind: str, title: str) -> bytes:
    ensure_fonts()
    font, bold = ensure_fonts()
    sheet_w, sheet_h = sheet_size(paper)
    half = sheet_w / 2
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(sheet_w, sheet_h))
    c.setTitle(title or "Quire")
    c.setAuthor("Quire")
    c.setCreator("Quire")
    width, _height = text_box(paper)

    def draw_half(origin: float, num: int, side: str) -> None:
        if kind == "dummy":
            c.setFillColorRGB(0.1, 0.08, 0.06)
            c.setFont(bold, 42)
            label = str(num)
            tw = pdfmetrics.stringWidth(label, bold, 42)
            c.drawString(origin + (half - tw) / 2, sheet_h / 2 - 8, label)
            return
        page = _lookup(pages, num)
        if page is None:
            return
        y = sheet_h - 36
        c.setFillColorRGB(0.08, 0.07, 0.05)
        if page["heading"].strip():
            c.setFont(bold, HEAD_SIZE)
            for line in wrap_text(page["heading"], width, HEAD_SIZE, bold):
                if y < 46:
                    return
                c.drawString(origin + (28 if side == "left" else 22), y, line)
                y -= HEAD_LEAD
            y -= 6
        c.setFont(font, BODY_SIZE)
        for line in wrap_text(page["body"], width, BODY_SIZE, font):
            if y < 46:
                return
            c.drawString(origin + (28 if side == "left" else 22), y, line)
            y -= BODY_LEAD
        c.setFont(font, 8)
        c.setFillColorRGB(0.35, 0.3, 0.24)
        mark = str(num)
        if side == "left":
            c.drawString(origin + 28, 18, mark)
        else:
            tw = pdfmetrics.stringWidth(mark, font, 8)
            c.drawString(origin + half - 28 - tw, 18, mark)

    for quire in plan["quires"]:
        for sheet in quire["sheets"]:
            for face in ("front", "back"):
                _draw_ticks(c, sheet_w, sheet_h)
                left, right = sheet[face]
                draw_half(0, left, "left")
                draw_half(half, right, "right")
                c.showPage()
    c.save()
    return buf.getvalue()


def reader_pdf(pages: list[dict[str, Any]], paper: str, title: str) -> bytes:
    font, bold = ensure_fonts()
    sheet_w, sheet_h = sheet_size(paper)
    page_w = sheet_w / 2
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, sheet_h))
    c.setTitle((title or "Quire") + " reader")
    c.setAuthor("Quire")
    width = page_w - 56
    for page in pages:
        y = sheet_h - 40
        c.setFillColorRGB(0.08, 0.07, 0.05)
        if page["heading"].strip():
            c.setFont(bold, HEAD_SIZE)
            for line in wrap_text(page["heading"], width, HEAD_SIZE, bold):
                if y < 40:
                    break
                c.drawString(28, y, line)
                y -= HEAD_LEAD
            y -= 6
        c.setFont(font, BODY_SIZE)
        for line in wrap_text(page["body"], width, BODY_SIZE, font):
            if y < 40:
                break
            c.drawString(28, y, line)
            y -= BODY_LEAD
        c.setFont(font, 8)
        c.setFillColorRGB(0.35, 0.3, 0.24)
        mark = str(page["position"])
        tw = pdfmetrics.stringWidth(mark, font, 8)
        if page["position"] % 2:
            c.drawString(page_w - 28 - tw, 18, mark)
        else:
            c.drawString(28, 18, mark)
        c.showPage()
    c.save()
    return buf.getvalue()


def _connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS booklets (
              id TEXT PRIMARY KEY,
              owner_hash TEXT NOT NULL,
              title TEXT NOT NULL,
              paper TEXT NOT NULL,
              quire INTEGER NOT NULL,
              created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL,
              expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pages (
              booklet_id TEXT NOT NULL,
              position INTEGER NOT NULL,
              heading TEXT NOT NULL,
              body TEXT NOT NULL,
              PRIMARY KEY (booklet_id, position)
            );
            """
        )
        conn.execute("DELETE FROM pages WHERE booklet_id IN (SELECT id FROM booklets WHERE expires_at <= ?)", (_now(),))
        conn.execute("DELETE FROM booklets WHERE expires_at <= ?", (_now(),))


def _load(conn: sqlite3.Connection, booklet_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM booklets WHERE id = ?", (booklet_id,)).fetchone()
    if row is None or int(row["expires_at"]) <= _now():
        return None
    pages = conn.execute(
        "SELECT position, heading, body FROM pages WHERE booklet_id = ? ORDER BY position",
        (booklet_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "owner_hash": row["owner_hash"],
        "title": row["title"],
        "paper": row["paper"],
        "quire": int(row["quire"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "expires_at": int(row["expires_at"]),
        "pages": [
            {"position": int(p["position"]), "heading": p["heading"], "body": p["body"]}
            for p in pages
        ],
    }


def _fetch(booklet_id: str) -> dict[str, Any] | None:
    if not _ID.match(booklet_id or ""):
        return None
    with _connect() as conn:
        return _load(conn, booklet_id)


def _require_owner(book: dict[str, Any], key: str) -> None:
    if not _KEY.match(key or "") or _hash_key(key) != book["owner_hash"]:
        raise QuireError("That booklet is gone, or the link is wrong.")


def create_booklet(title: str, paper: str, quire: int, pages: list[tuple[str, str]]) -> tuple[str, str]:
    if not pages:
        pages = [(title, "")]
    if len(pages) > MAX_PAGES:
        raise QuireError("64 pages is the limit.")
    pad_count(len(pages), quire)
    key = secrets.token_urlsafe(18)
    now = _now()
    with _connect() as conn:
        for _ in range(6):
            booklet_id = secrets.token_urlsafe(6)
            if not _ID.match(booklet_id):
                continue
            try:
                conn.execute(
                    """
                    INSERT INTO booklets (id, owner_hash, title, paper, quire, created_at, updated_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (booklet_id, _hash_key(key), title, paper, quire, now, now, now + TTL_SECONDS),
                )
            except sqlite3.IntegrityError:
                continue
            conn.executemany(
                "INSERT INTO pages (booklet_id, position, heading, body) VALUES (?, ?, ?, ?)",
                [(booklet_id, i + 1, heading, body) for i, (heading, body) in enumerate(pages)],
            )
            return booklet_id, key
    raise QuireError("Could not save the booklet. Try again.")


def _touch(conn: sqlite3.Connection, booklet_id: str) -> None:
    conn.execute("UPDATE booklets SET updated_at = ? WHERE id = ?", (_now(), booklet_id))


def update_settings(booklet_id: str, key: str, title: str, paper: str, quire: int) -> None:
    book = _fetch(booklet_id)
    if book is None:
        raise QuireError("That booklet is gone, or the link is wrong.")
    _require_owner(book, key)
    pad_count(len(book["pages"]), quire)
    with _connect() as conn:
        conn.execute(
            "UPDATE booklets SET title = ?, paper = ?, quire = ?, updated_at = ? WHERE id = ?",
            (title, paper, quire, _now(), booklet_id),
        )


def save_page(booklet_id: str, key: str, position: int, heading: str, body: str) -> None:
    book = _fetch(booklet_id)
    if book is None:
        raise QuireError("That booklet is gone, or the link is wrong.")
    _require_owner(book, key)
    if not any(p["position"] == position for p in book["pages"]):
        raise QuireError("Page is missing.")
    with _connect() as conn:
        conn.execute(
            "UPDATE pages SET heading = ?, body = ? WHERE booklet_id = ? AND position = ?",
            (heading, body, booklet_id, position),
        )
        _touch(conn, booklet_id)


def add_page(booklet_id: str, key: str, heading: str, body: str) -> int:
    book = _fetch(booklet_id)
    if book is None:
        raise QuireError("That booklet is gone, or the link is wrong.")
    _require_owner(book, key)
    if len(book["pages"]) >= MAX_PAGES:
        raise QuireError("64 pages is the limit.")
    position = len(book["pages"]) + 1
    with _connect() as conn:
        conn.execute(
            "INSERT INTO pages (booklet_id, position, heading, body) VALUES (?, ?, ?, ?)",
            (booklet_id, position, heading, body),
        )
        _touch(conn, booklet_id)
    return position


def fill_pages(booklet_id: str, key: str) -> None:
    book = _fetch(booklet_id)
    if book is None:
        raise QuireError("That booklet is gone, or the link is wrong.")
    _require_owner(book, key)
    padded = pad_count(len(book["pages"]), book["quire"])
    missing = padded - len(book["pages"])
    if missing <= 0:
        return
    with _connect() as conn:
        start = len(book["pages"]) + 1
        conn.executemany(
            "INSERT INTO pages (booklet_id, position, heading, body) VALUES (?, ?, '', '')",
            [(booklet_id, pos) for pos in range(start, start + missing)],
        )
        _touch(conn, booklet_id)


def move_page(booklet_id: str, key: str, position: int, direction: str) -> int:
    book = _fetch(booklet_id)
    if book is None:
        raise QuireError("That booklet is gone, or the link is wrong.")
    _require_owner(book, key)
    other = position - 1 if direction == "up" else position + 1
    positions = {p["position"] for p in book["pages"]}
    if position not in positions or other not in positions:
        raise QuireError("Nothing to move.")
    with _connect() as conn:
        conn.execute("UPDATE pages SET position = -1 WHERE booklet_id = ? AND position = ?", (booklet_id, position))
        conn.execute("UPDATE pages SET position = ? WHERE booklet_id = ? AND position = ?", (position, booklet_id, other))
        conn.execute("UPDATE pages SET position = ? WHERE booklet_id = ? AND position = -1", (other, booklet_id))
        _touch(conn, booklet_id)
    return other


def delete_page(booklet_id: str, key: str, position: int) -> None:
    book = _fetch(booklet_id)
    if book is None:
        raise QuireError("That booklet is gone, or the link is wrong.")
    _require_owner(book, key)
    if len(book["pages"]) <= 1:
        raise QuireError("Keep at least one page.")
    if not any(p["position"] == position for p in book["pages"]):
        raise QuireError("Page is missing.")
    with _connect() as conn:
        conn.execute("DELETE FROM pages WHERE booklet_id = ? AND position = ?", (booklet_id, position))
        rows = conn.execute(
            "SELECT position FROM pages WHERE booklet_id = ? ORDER BY position",
            (booklet_id,),
        ).fetchall()
        for index, row in enumerate(rows, start=1):
            if int(row["position"]) != index:
                conn.execute(
                    "UPDATE pages SET position = ? WHERE booklet_id = ? AND position = ?",
                    (index, booklet_id, int(row["position"])),
                )
        _touch(conn, booklet_id)


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d %b %Y")


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return (slug or "booklet")[:40]


def _snippet(page: dict[str, str] | None) -> str:
    if page is None:
        return ""
    text = page["heading"].strip() or " ".join(page["body"].split())
    if len(text) > 72:
        return text[:69] + "..."
    return text


def _waste_line(written: int, quire: int) -> str:
    padded, blanks, better = waste_choice(written, quire)
    line = f"{written} written. {padded} on the sheet."
    if blanks == 0:
        line += " No blanks."
    elif blanks == 1:
        line += " 1 blank pad."
    else:
        line += f" {blanks} blank pads."
    if better:
        line += f" A quire of {better[1]} adds {better[0]} instead."
    return line


def sheet_list_text(plan: dict[str, Any], paper: str, title: str) -> str:
    lines = [
        f"{title or 'Booklet'}",
        f"{PAPER_LABEL[paper]}, quires of {plan['quire']}, {plan['padded']} pages ({plan['written']} written).",
        "Print landscape, double sided, flip on the short edge.",
        "",
    ]
    for quire in plan["quires"]:
        lines.append(f"Quire {quire['index'] + 1}, pages {quire['first']} to {quire['last']}")
        for sheet in quire["sheets"]:
            fl, fr = sheet["front"]
            bl, br = sheet["back"]
            lines.append(
                f"  Sheet {sheet['index'] + 1} {sheet['role']} (PDF {sheet['pdf_front']} and {sheet['pdf_back']})"
            )
            lines.append(f"    front  {fl} | {fr}")
            lines.append(f"    back   {bl} | {br}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _e(value: str) -> str:
    return html.escape(value or "", quote=True)


def _asset_version(*paths: Path) -> str:
    stamp = 0.0
    for path in paths:
        try:
            stamp = max(stamp, path.stat().st_mtime)
        except OSError:
            pass
    return str(int(stamp))


def _shell(name: str) -> str:
    html_path = STATIC / name
    css_path = STATIC / "style.css"
    js_path = STATIC / "home.js"
    stamp = max(
        html_path.stat().st_mtime,
        css_path.stat().st_mtime if css_path.exists() else 0.0,
        js_path.stat().st_mtime if js_path.exists() else 0.0,
    )
    cached = _page_cache.get(name)
    if cached and cached[0] == stamp:
        return cached[1]
    page = html_path.read_text(encoding="utf-8")
    css = css_path.read_text(encoding="utf-8") if css_path.exists() else ""
    page = re.sub(
        r"""[ \t]*<link[^>]+href=["']/static/style\.css[^"']*["'][^>]*>\s*""",
        "",
        page,
        flags=re.I,
    )
    page = re.sub(r"</head>", f'<style id="quire-css">\n{css}\n</style>\n</head>', page, count=1, flags=re.I)
    version = _asset_version(js_path, css_path)
    page = re.sub(
        r"""src=(["'])/static/home\.js(?:\?[^"']*)?\1""",
        rf"src=\1/static/home.js?v={version}\1",
        page,
    )
    _page_cache[name] = (stamp, page)
    return page


def _fill(template: str, mapping: dict[str, str]) -> str:
    parts = _TOKEN.split(template)
    return "".join(mapping.get(part, part) for part in parts)


def _options(selected: str, pairs: list[tuple[str, str]]) -> str:
    bits = []
    for value, label in pairs:
        mark = " selected" if value == selected else ""
        bits.append(f'<option value="{_e(value)}"{mark}>{_e(label)}</option>')
    return "".join(bits)


def _half_html(num: int, page: dict[str, str] | None, padded: int, quire: int) -> str:
    role = page_role(num, padded, quire)
    role_html = f'<span class="role">{_e(role)}</span>' if role else ""
    if page is None:
        return (
            f'<div class="half blank"><span class="num">{num}</span>{role_html}'
            "<p>Blank on the sheet</p></div>"
        )
    heading = f"<strong>{_e(page['heading'])}</strong>" if page["heading"].strip() else ""
    raw = _snippet(page) if not page["heading"].strip() else " ".join(page["body"].split())[:72]
    body = f"<p>{_e(raw)}</p>" if raw else ""
    return f'<div class="half"><span class="num">{num}</span>{role_html}{heading}{body}</div>'


def sheets_html(plan: dict[str, Any], pages: dict[int, dict[str, str]]) -> str:
    blocks = ['<h2>Sheets</h2>', f'<p class="hint">{_e(_waste_line(plan["written"], plan["quire"]))}</p>']
    for quire in plan["quires"]:
        blocks.append('<section class="quire">')
        blocks.append(
            f'<p class="quire-head">Quire {quire["index"] + 1} of {len(plan["quires"])}. '
            f'Pages {quire["first"]} to {quire["last"]}. Outside sheet first.</p>'
        )
        for sheet in quire["sheets"]:
            fl, fr = sheet["front"]
            bl, br = sheet["back"]
            blocks.append('<article class="sheet">')
            blocks.append(
                f'<header>Sheet {sheet["index"] + 1}, {sheet["role"]}. '
                f'PDF {sheet["pdf_front"]} and {sheet["pdf_back"]}</header>'
            )
            blocks.append('<p class="face-label">Front</p><div class="spread">')
            blocks.append(_half_html(fl, pages.get(fl), plan["padded"], plan["quire"]))
            blocks.append('<div class="fold"></div>')
            blocks.append(_half_html(fr, pages.get(fr), plan["padded"], plan["quire"]))
            blocks.append("</div>")
            blocks.append('<p class="face-label">Back, after a short edge flip</p><div class="spread">')
            blocks.append(_half_html(bl, pages.get(bl), plan["padded"], plan["quire"]))
            blocks.append('<div class="fold"></div>')
            blocks.append(_half_html(br, pages.get(br), plan["padded"], plan["quire"]))
            blocks.append("</div></article>")
        blocks.append("</section>")
    return "".join(blocks)


def print_html(plan: dict[str, Any], paper: str, title: str, links: dict[str, str]) -> str:
    paper_name = PAPER_LABEL[paper]
    buttons = []
    if links.get("imposed"):
        buttons.append(f'<a class="btn primary" href="{links["imposed"]}">Download imposed PDF</a>')
    if links.get("dummy"):
        buttons.append(f'<a class="btn" href="{links["dummy"]}">Download numbered dummy</a>')
    if links.get("reader"):
        buttons.append(f'<a class="btn" href="{links["reader"]}">Download reader PDF</a>')
    return (
        "<h2>Print</h2>"
        '<div class="steps">'
        '<div class="step"><span class="badge">1</span><p>Download the imposed PDF. Fold the numbered dummy first if this is a new printer.</p></div>'
        f'<div class="step"><span class="badge">2</span><p>Print {paper_name}, landscape, double sided, flip on the short edge. Do not scale. If the back is upside down, reprint and flip on the long edge.</p></div>'
        '<div class="step"><span class="badge">3</span><p>Fold each sheet on the center ticks. Put the center sheet inside the outside sheet. Stack quires in order and staple or sew the fold.</p></div>'
        "</div>"
        f'<div class="row-actions">{"".join(buttons)}</div>'
        f'<pre class="sheet-list" id="sheet-list">{_e(sheet_list_text(plan, paper, title))}</pre>'
        '<button type="button" class="btn sm" id="copy-list" data-copy="sheet-list">Copy sheet list</button>'
    )


def write_html(book: dict[str, Any], key: str) -> str:
    booklet_id = book["id"]
    base = f"/b/{booklet_id}/{key}"
    plan = plan_book(len(book["pages"]), book["quire"])
    paper_opts = _options(book["paper"], [("letter", "US Letter"), ("a4", "A4")])
    quire_opts = _options(
        str(book["quire"]),
        [("4", "4, one folded sheet"), ("8", "8, a thin signature"), ("12", "12"), ("16", "16, a sewn section")],
    )
    cards = []
    for page in book["pages"]:
        pos = page["position"]
        role = page_role(pos, plan["padded"], book["quire"])
        role_html = f'<span class="role">{_e(role)}</span>' if role else ""
        clip = '<span class="clip">Clipped in print</span>' if overflows(page["heading"], page["body"], book["paper"]) else ""
        up_dis = " disabled" if pos == 1 else ""
        down_dis = " disabled" if pos == len(book["pages"]) else ""
        cards.append(
            f'<article class="page-card" id="p{pos}">'
            f"<header><span class=\"badge\">{pos}</span>{role_html}{clip}</header>"
            f'<form method="post" action="{base}/page/{pos}">'
            '<label class="field"><span>Heading</span>'
            f'<input name="heading" type="text" maxlength="72" autocomplete="off" value="{_e(page["heading"])}" />'
            "</label>"
            '<label class="field"><span>Body</span>'
            f'<textarea name="body" maxlength="900" data-count="count-{pos}">{_e(page["body"])}</textarea>'
            f'<span class="count" id="count-{pos}"></span>'
            "</label>"
            '<button type="submit" class="btn primary sm">Save page</button>'
            "</form>"
            '<div class="row-actions">'
            f'<form method="post" action="{base}/move"><input type="hidden" name="dir" value="up" /><input type="hidden" name="position" value="{pos}" /><button class="btn sm" type="submit"{up_dis}>Up</button></form>'
            f'<form method="post" action="{base}/move"><input type="hidden" name="dir" value="down" /><input type="hidden" name="position" value="{pos}" /><button class="btn sm" type="submit"{down_dis}>Down</button></form>'
            f'<form method="post" action="{base}/delete" data-confirm="Remove this page?"><input type="hidden" name="position" value="{pos}" /><button class="btn sm danger" type="submit">Remove</button></form>'
            "</div></article>"
        )
    fill = ""
    if plan["padded"] > len(book["pages"]):
        fill = (
            f'<form method="post" action="{base}/fill">'
            f'<button class="btn" type="submit">Add pages up to {plan["padded"]}</button>'
            "</form>"
        )
    return (
        "<h2>Write</h2>"
        f'<p class="hint" id="waste-line">{_e(_waste_line(len(book["pages"]), book["quire"]))}</p>'
        f'<form class="card" method="post" action="{base}/settings">'
        '<label class="field"><span>Title</span>'
        f'<input name="title" type="text" maxlength="60" required value="{_e(book["title"])}" /></label>'
        f'<label class="field"><span>Paper</span><select name="paper">{paper_opts}</select></label>'
        f'<label class="field"><span>Quire</span><select name="quire" id="quire-select">{quire_opts}</select></label>'
        '<button class="btn sm" type="submit">Save setup</button>'
        "</form>"
        + "".join(cards)
        + f'<form class="card" method="post" action="{base}/add">'
        '<label class="field"><span>New heading</span><input name="heading" type="text" maxlength="72" autocomplete="off" /></label>'
        '<label class="field"><span>New body</span><textarea name="body" maxlength="900"></textarea></label>'
        '<button class="btn primary" type="submit">Add page</button>'
        "</form>"
        + fill
    )


def render_home(status: str = "", status_class: str = "", title: str = "") -> str:
    page = _shell("index.html")
    return (
        page.replace("<!--STATUS-->", _e(status))
        .replace("<!--STATUS_CLASS-->", status_class)
        .replace("<!--TITLE-->", _e(title))
    )


def render_work(
    mode: str,
    title_tag: str,
    eyebrow: str,
    h1: str,
    lede: str,
    status: str,
    status_class: str,
    owner: str,
    write: str,
    sheets: str,
    printed: str,
    written: int,
) -> str:
    return _fill(
        _shell("work.html"),
        {
            "<!--MODE-->": mode,
            "<!--TITLE_TAG-->": _e(title_tag),
            "<!--EYEBROW-->": _e(eyebrow),
            "<!--H1-->": _e(h1),
            "<!--LEDE-->": _e(lede),
            "<!--STATUS-->": _e(status),
            "<!--STATUS_CLASS-->": status_class,
            "<!--OWNER-->": owner,
            "<!--WRITE-->": write,
            "<!--SHEETS-->": sheets,
            "<!--PRINT-->": printed,
            "<!--WRITTEN-->": str(written),
        },
    )


def _owner_box(owner_url: str, read_url: str, expires_at: int) -> str:
    return (
        "<p class=\"hint\">This page is the key. Bookmark it. The read only link can see sheets and print, not edit.</p>"
        '<p class="label">Owner link</p>'
        f'<p class="url-box" id="owner-url">{_e(owner_url)}</p>'
        '<button type="button" class="btn sm" id="copy-owner" data-copy="owner-url">Copy owner link</button>'
        '<p class="label">Read only link</p>'
        f'<p class="url-box" id="read-url">{_e(read_url)}</p>'
        '<button type="button" class="btn sm" id="copy-read" data-copy="read-url">Copy read only link</button>'
        f'<p class="hint">Open until {_e(_when(expires_at))}.</p>'
    )


def _pages_map(book: dict[str, Any]) -> dict[int, dict[str, str]]:
    return {p["position"]: p for p in book["pages"]}


def _abs(request: Request, path: str) -> str:
    base = str(request.base_url).rstrip("/")
    return base + path


def _pdf_response(data: bytes, filename: str) -> Response:
    return Response(
        data,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _gone() -> HTMLResponse:
    return HTMLResponse(_shell("gone.html"), status_code=404)


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "pages": plan["written"],
        "padded": plan["padded"],
        "quire": plan["quire"],
        "blanks": plan["padded"] - plan["written"],
        "pdf_pages": plan["pdf_pages"],
        "print": "landscape, double sided, flip on the short edge",
        "quires": [
            {
                "quire": quire["index"] + 1,
                "first": quire["first"],
                "last": quire["last"],
                "sheets": [
                    {
                        "sheet": sheet["index"] + 1,
                        "role": sheet["role"],
                        "pdf": [sheet["pdf_front"], sheet["pdf_back"]],
                        "front": list(sheet["front"]),
                        "back": list(sheet["back"]),
                    }
                    for sheet in quire["sheets"]
                ],
            }
            for quire in plan["quires"]
        ],
    }


app = FastAPI(title="Quire", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    init_db()
    ensure_fonts()


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def _headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    if request.method == "POST":
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > 100_000:
            return Response("Too large.", status_code=413)
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'"
    )
    path = request.url.path
    if path.startswith("/b/"):
        response.headers["X-Robots-Tag"] = "noindex"
    if path == "/" or path.startswith("/b/") or path == "/map" or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache"
    if path.startswith("/static/") and "v=" in str(request.url.query):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


@app.get("/health")
def health() -> dict[str, Any]:
    with _connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True, "service": "quire"}


@app.get("/robots.txt")
def robots() -> Response:
    return Response((STATIC / "robots.txt").read_text(encoding="utf-8"), media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(render_home())


@app.get("/api/plan")
def api_plan(pages: int = 8, quire: int = 8) -> JSONResponse:
    try:
        size = _quire(quire)
        if pages < 1 or pages > MAX_PAGES:
            raise QuireError("Pages must be 1 to 64.")
        plan = plan_book(pages, size)
    except QuireError as err:
        return JSONResponse({"error": str(err)}, status_code=400)
    return JSONResponse(_public_plan(plan))


@app.get("/map", response_class=HTMLResponse)
def map_page(pages: int = 8, quire: int = 8, paper: str = "letter") -> HTMLResponse:
    try:
        paper_name = _paper(paper)
        size = _quire(quire)
        if pages < 1 or pages > MAX_PAGES:
            raise QuireError("Pages must be 1 to 64.")
        plan = plan_book(pages, size)
    except QuireError as err:
        return HTMLResponse(render_home(str(err), " bad"), status_code=400)
    query = f"pages={pages}&quire={size}&paper={paper_name}"
    printed = print_html(
        plan,
        paper_name,
        f"{pages} pages",
        {"dummy": f"/map.pdf?{query}"},
    )
    adjust = (
        '<form class="card" action="/map" method="get">'
        '<label class="field"><span>Pages</span>'
        f'<input name="pages" type="number" min="1" max="64" value="{pages}" /></label>'
        '<label class="field"><span>Quire</span><select name="quire">'
        + _options(str(size), [("4", "4"), ("8", "8"), ("12", "12"), ("16", "16")])
        + "</select></label>"
        '<label class="field"><span>Paper</span><select name="paper">'
        + _options(paper_name, [("letter", "US Letter"), ("a4", "A4")])
        + "</select></label>"
        '<button class="btn sm" type="submit">Update</button></form>'
    )
    page = render_work(
        "mode-map",
        "Quire sheet order",
        "No account",
        "Sheet order",
        _waste_line(pages, size) + f" {PAPER_LABEL[paper_name]}.",
        "",
        "",
        "",
        "",
        sheets_html(plan, {}) + adjust,
        printed,
        pages,
    )
    return HTMLResponse(page)


@app.get("/map.pdf")
def map_pdf(request: Request, pages: int = 8, quire: int = 8, paper: str = "letter") -> Response:
    if not _rate_ok(_client_ip(request), "pdf", 40):
        return Response("Too many PDFs from this network. Try again in an hour.", status_code=429)
    try:
        paper_name = _paper(paper)
        size = _quire(quire)
        if pages < 1 or pages > MAX_PAGES:
            raise QuireError("Pages must be 1 to 64.")
        plan = plan_book(pages, size)
    except QuireError as err:
        return Response(str(err), status_code=400)
    data = build_pdf(plan, {}, paper_name, "dummy", f"{pages} page dummy")
    return _pdf_response(data, f"quire-{pages}-dummy.pdf")


@app.post("/api/booklets")
async def create(request: Request) -> Response:
    ip = _client_ip(request)
    if not _rate_ok(ip, "create", 12):
        return HTMLResponse(render_home("Too many booklets from this network. Try again in an hour.", " bad"), status_code=429)
    form = await request.form()
    try:
        if str(form.get("sample") or "") == "1":
            title, paper, size, pages = "Fold this", "letter", 8, list(SAMPLE)
        else:
            title = _clean(str(form.get("title") or ""), 60, "title", required=True)
            paper = _paper(str(form.get("paper") or "letter"))
            size = _quire(str(form.get("quire") or "8"))
            pages = [(title, "")]
        booklet_id, key = create_booklet(title, paper, size, pages)
    except QuireError as err:
        return HTMLResponse(render_home(str(err), " bad", str(form.get("title") or "")), status_code=400)
    return RedirectResponse(f"/b/{booklet_id}/{key}", status_code=303)


def _book_or_gone(booklet_id: str) -> dict[str, Any] | None:
    return _fetch(booklet_id)


@app.get("/b/{booklet_id}", response_class=HTMLResponse)
def read_book(request: Request, booklet_id: str) -> HTMLResponse:
    book = _book_or_gone(booklet_id)
    if book is None:
        return _gone()
    plan = plan_book(len(book["pages"]), book["quire"])
    pages = _pages_map(book)
    links = {
        "imposed": f"/b/{booklet_id}/imposed.pdf",
        "dummy": f"/b/{booklet_id}/dummy.pdf",
        "reader": f"/b/{booklet_id}/reader.pdf",
    }
    page = render_work(
        "mode-read",
        book["title"],
        "Read only",
        book["title"],
        f"This link cannot edit. Printing is fine. Open until {_when(book['expires_at'])}.",
        "",
        "",
        "",
        "",
        sheets_html(plan, pages),
        print_html(plan, book["paper"], book["title"], links),
        len(book["pages"]),
    )
    return HTMLResponse(page)


def owner_book(request: Request, booklet_id: str, key: str, status: str = "", code: int = 200) -> HTMLResponse:
    book = _book_or_gone(booklet_id)
    if book is None or not _KEY.match(key):
        return _gone()
    try:
        _require_owner(book, key)
    except QuireError:
        return _gone()
    plan = plan_book(len(book["pages"]), book["quire"])
    pages = _pages_map(book)
    links = {
        "imposed": f"/b/{booklet_id}/imposed.pdf",
        "dummy": f"/b/{booklet_id}/dummy.pdf",
        "reader": f"/b/{booklet_id}/reader.pdf",
    }
    owner_url = _abs(request, f"/b/{booklet_id}/{key}")
    read_url = _abs(request, f"/b/{booklet_id}")
    page = render_work(
        "mode-owner",
        book["title"],
        "Owner link. Bookmark it.",
        book["title"],
        "Page 1 is the front cover. The last page of the last quire is the back cover.",
        status,
        " bad" if status else "",
        _owner_box(owner_url, read_url, book["expires_at"]),
        write_html(book, key),
        sheets_html(plan, pages),
        print_html(plan, book["paper"], book["title"], links),
        len(book["pages"]),
    )
    return HTMLResponse(page, status_code=code)


def _render_owner(request: Request, booklet_id: str, key: str, status: str = "", code: int = 200) -> HTMLResponse:
    return owner_book(request, booklet_id, key, status, code)


@app.post("/b/{booklet_id}/{key}/settings")
async def post_settings(booklet_id: str, key: str, request: Request) -> Response:
    form = await request.form()
    if not _rate_ok(_client_ip(request), "edit", 180):
        return _render_owner(request, booklet_id, key, "Too many edits from this network. Try again in an hour.", 429)
    try:
        update_settings(
            booklet_id,
            key,
            _clean(str(form.get("title") or ""), 60, "title", required=True),
            _paper(str(form.get("paper") or "letter")),
            _quire(str(form.get("quire") or "8")),
        )
    except QuireError as err:
        return _render_owner(request, booklet_id, key, str(err), 400)
    return RedirectResponse(f"/b/{booklet_id}/{key}#write", status_code=303)


@app.post("/b/{booklet_id}/{key}/page/{position}")
async def post_page(booklet_id: str, key: str, position: int, request: Request) -> Response:
    form = await request.form()
    if not _rate_ok(_client_ip(request), "edit", 180):
        return _render_owner(request, booklet_id, key, "Too many edits from this network. Try again in an hour.", 429)
    try:
        save_page(
            booklet_id,
            key,
            position,
            _clean(str(form.get("heading") or ""), 72, "heading"),
            _clean(str(form.get("body") or ""), 900, "body"),
        )
    except QuireError as err:
        return _render_owner(request, booklet_id, key, str(err), 400)
    return RedirectResponse(f"/b/{booklet_id}/{key}#p{position}", status_code=303)


@app.post("/b/{booklet_id}/{key}/add")
async def post_add(booklet_id: str, key: str, request: Request) -> Response:
    form = await request.form()
    if not _rate_ok(_client_ip(request), "edit", 180):
        return _render_owner(request, booklet_id, key, "Too many edits from this network. Try again in an hour.", 429)
    try:
        position = add_page(
            booklet_id,
            key,
            _clean(str(form.get("heading") or ""), 72, "heading"),
            _clean(str(form.get("body") or ""), 900, "body"),
        )
    except QuireError as err:
        return _render_owner(request, booklet_id, key, str(err), 400)
    return RedirectResponse(f"/b/{booklet_id}/{key}#p{position}", status_code=303)


@app.post("/b/{booklet_id}/{key}/fill")
async def post_fill(booklet_id: str, key: str, request: Request) -> Response:
    if not _rate_ok(_client_ip(request), "edit", 180):
        return _render_owner(request, booklet_id, key, "Too many edits from this network. Try again in an hour.", 429)
    try:
        fill_pages(booklet_id, key)
    except QuireError as err:
        return _render_owner(request, booklet_id, key, str(err), 400)
    return RedirectResponse(f"/b/{booklet_id}/{key}#write", status_code=303)


@app.post("/b/{booklet_id}/{key}/move")
async def post_move(booklet_id: str, key: str, request: Request) -> Response:
    form = await request.form()
    if not _rate_ok(_client_ip(request), "edit", 180):
        return _render_owner(request, booklet_id, key, "Too many edits from this network. Try again in an hour.", 429)
    direction = str(form.get("dir") or "")
    if direction not in {"up", "down"}:
        return _render_owner(request, booklet_id, key, "Nothing to move.", 400)
    try:
        position = int(str(form.get("position") or "0"))
        landed = move_page(booklet_id, key, position, direction)
    except (QuireError, ValueError) as err:
        message = str(err) if isinstance(err, QuireError) else "Nothing to move."
        return _render_owner(request, booklet_id, key, message, 400)
    return RedirectResponse(f"/b/{booklet_id}/{key}#p{landed}", status_code=303)


@app.post("/b/{booklet_id}/{key}/delete")
async def post_delete(booklet_id: str, key: str, request: Request) -> Response:
    form = await request.form()
    if not _rate_ok(_client_ip(request), "edit", 180):
        return _render_owner(request, booklet_id, key, "Too many edits from this network. Try again in an hour.", 429)
    try:
        delete_page(booklet_id, key, int(str(form.get("position") or "0")))
    except (QuireError, ValueError) as err:
        message = str(err) if isinstance(err, QuireError) else "Page is missing."
        return _render_owner(request, booklet_id, key, message, 400)
    return RedirectResponse(f"/b/{booklet_id}/{key}#write", status_code=303)


def _book_pdf(request: Request, booklet_id: str, kind: str) -> Response:
    if not _rate_ok(_client_ip(request), "pdf", 40):
        return Response("Too many PDFs from this network. Try again in an hour.", status_code=429)
    book = _fetch(booklet_id)
    if book is None:
        return Response("That booklet is gone.", status_code=404)
    plan = plan_book(len(book["pages"]), book["quire"])
    slug = _slug(book["title"])
    if kind == "reader":
        data = reader_pdf(book["pages"], book["paper"], book["title"])
        return _pdf_response(data, f"{slug}-reader.pdf")
    pages = _pages_map(book) if kind == "imposed" else {}
    data = build_pdf(plan, pages, book["paper"], "book" if kind == "imposed" else "dummy", book["title"])
    suffix = "imposed" if kind == "imposed" else "dummy"
    return _pdf_response(data, f"{slug}-{suffix}.pdf")


@app.get("/b/{booklet_id}/imposed.pdf")
def imposed_pdf(request: Request, booklet_id: str) -> Response:
    return _book_pdf(request, booklet_id, "imposed")


@app.get("/b/{booklet_id}/dummy.pdf")
def dummy_pdf(request: Request, booklet_id: str) -> Response:
    return _book_pdf(request, booklet_id, "dummy")


@app.get("/b/{booklet_id}/reader.pdf")
def reader_download(request: Request, booklet_id: str) -> Response:
    return _book_pdf(request, booklet_id, "reader")


@app.get("/b/{booklet_id}/{key}", response_class=HTMLResponse)
def owner_page(request: Request, booklet_id: str, key: str) -> HTMLResponse:
    return _render_owner(request, booklet_id, key)


def _self_check() -> None:
    eight = plan_book(8, 8)
    sheets = eight["quires"][0]["sheets"]
    assert sheets[0]["front"] == (8, 1), sheets[0]
    assert sheets[0]["back"] == (2, 7), sheets[0]
    assert sheets[1]["front"] == (6, 3), sheets[1]
    assert sheets[1]["back"] == (4, 5), sheets[1]
    four = plan_book(4, 4)
    assert four["quires"][0]["sheets"][0]["front"] == (4, 1)
    assert four["quires"][0]["sheets"][0]["back"] == (2, 3)
    two = plan_book(10, 8)
    assert two["padded"] == 16
    assert len(two["quires"]) == 2
    assert two["quires"][1]["sheets"][0]["front"] == (16, 9)
    seen = []
    for quire in two["quires"]:
        for sheet in quire["sheets"]:
            seen.extend(sheet["front"])
            seen.extend(sheet["back"])
    assert sorted(seen) == list(range(1, 17))
    twelve = plan_book(12, 12)
    seen12 = []
    for sheet in twelve["quires"][0]["sheets"]:
        seen12.extend(sheet["front"])
        seen12.extend(sheet["back"])
    assert sorted(seen12) == list(range(1, 13))
    assert pad_count(6, 8) == 8
    assert waste_choice(10, 8)[2] == (2, 4)
    data = build_pdf(eight, {1: {"heading": "Hi", "body": "There"}}, "letter", "book", "Test")
    assert data.startswith(b"%PDF")
    assert len(re.findall(rb"/Type\s*/Page[^s]", data)) == 4
    dummy = build_pdf(eight, {}, "a4", "dummy", "Dummy")
    assert dummy.startswith(b"%PDF")
    reader = reader_pdf([{"position": 1, "heading": "Hi", "body": "There"}], "letter", "Test")
    assert reader.startswith(b"%PDF")
    print("quire ok")


if __name__ == "__main__":
    _self_check()
