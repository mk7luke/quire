# Quire

Write a short booklet, or count the pages you already have. See which page lands on which side of the sheet. Print a PDF you can fold.

Live: https://quire.holyx.link

## What it does

- Writer: title, pages, reorder, quire size, letter or A4. Owner link is the key. Read only link can print.
- Sheet diagram: each signature, front and back, fold in the middle.
- Imposed PDF for the copier. Numbered dummy to fold first. Reader PDF for a phone proof.
- Order path needs no account: `/map?pages=8&quire=8`
- Script: `GET /api/plan?pages=8&quire=8`

## Print

Landscape, double sided, flip on the short edge. Do not scale. If the back comes out upside down, flip on the long edge instead.

Page 1 is the front cover, on the right of the outside sheet. The last page of that quire is on the left. Nest the center sheet inside the outside sheet. Stack quires in order.

No creep shift. Trim after folding if the edges step.

## Limits

- 64 pages. Saved booklets last 30 days.
- 12 new booklets per IP per hour. 40 PDFs per IP per hour.
- Quires of 4, 8, 12, or 16.
- No accounts. Lose the owner link and the booklet stays read only.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8883
```

Health: `GET /health`

MIT. Built on holy-hermes.
