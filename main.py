"""
Vendagon Refill Backend
- Proxies Vendolite API
- Generates matrix-style PDF reports (slots in natural A1,A2,B1... order)
- Manages machine_groups via Supabase
"""

import os
import io
import re
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Table,
    TableStyle,
    Paragraph,
    Spacer,
    PageBreak,
)

# ----------------------------- Config -----------------------------
VENDOLITE_BASE = "https://ecloud.vendolite.com/api"
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

app = FastAPI(title="Vendagon Refill Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ----------------------------- Helpers -----------------------------
def _natural_slot_key(slot: dict):
    """
    Sort slots by their physical position in the machine:
      row first, then column.
    Falls back to parsing slot_name like 'A x 1' / 'A1' so machines that
    don't return row_number/column_number still sort sensibly.
    """
    row = slot.get("row_number")
    col = slot.get("column_number")
    if isinstance(row, int) and isinstance(col, int):
        return (row, col)

    name = (slot.get("slot_name") or "").strip().upper()
    # Match things like "A1", "A 1", "A x 1", "AA 12"
    m = re.match(r"([A-Z]+)\s*[Xx]?\s*(\d+)", name)
    if m:
        letters, num = m.group(1), int(m.group(2))
        # Convert column letters to a number (A=1, B=2, ... AA=27)
        row_idx = 0
        for ch in letters:
            row_idx = row_idx * 26 + (ord(ch) - ord("A") + 1)
        return (row_idx, num)
    return (9999, 9999)


def _status_color(status: str):
    """Two-tone background: red tint = needs refill, white = ok, grey = disabled."""
    s = (status or "").lower()
    if s in ("empty", "low", "issue"):
        return colors.HexColor("#FDE2E2")
    if s == "disabled":
        return colors.HexColor("#ECECEC")
    return colors.white


def _vendolite_slots(token: str, machine_id: int) -> List[dict]:
    """Fetch slot list for a machine from Vendolite. Returns [] on failure."""
    try:
        r = httpx.post(
            f"{VENDOLITE_BASE}/machineSlot/getAllSlots",
            headers={"Authorization": f"Bearer {token}"},
            json={"machineId": int(machine_id)},
            timeout=30.0,
        )
        if r.status_code != 200:
            return []
        data = r.json()
        # Vendolite shapes vary — normalise to list
        if isinstance(data, dict):
            for key in ("data", "result", "slots"):
                if key in data and isinstance(data[key], list):
                    return data[key]
            return []
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def _normalise_slot(raw: dict) -> dict:
    """
    Vendolite returns inconsistent field names across machines/firmwares.
    Map everything to the canonical shape the app + PDF expect.
    """
    def pick(*keys, default=None):
        for k in keys:
            if k in raw and raw[k] is not None:
                return raw[k]
        return default

    current = pick("current_qty", "currentQty", "currentQuantity", "qty", default=0) or 0
    max_qty = pick("max_qty", "maxQty", "maxQuantity", "capacity", default=0) or 0
    enabled = pick("enabled", "isEnabled", "active", default=True)
    issue = pick("issue_found", "issueFound", "hasIssue", default=False) or False
    name = pick("slot_name", "slotName", "name", default="?") or "?"
    product = pick("product_name", "productName", default="-") or "-"

    try:
        current = int(current)
    except Exception:
        current = 0
    try:
        max_qty = int(max_qty)
    except Exception:
        max_qty = 0

    if not enabled:
        status = "disabled"
    elif issue:
        status = "issue"
    elif current == 0:
        status = "empty"
    elif max_qty > 0 and current < max_qty * 0.5:
        status = "low"
    else:
        status = "good"

    return {
        "slot_id": pick("slot_id", "slotId", "id"),
        "slot_name": name,
        "row_number": pick("row_number", "rowNumber", "row"),
        "column_number": pick("column_number", "columnNumber", "column"),
        "product_name": product,
        "product_id": pick("product_id", "productId", default=""),
        "current_qty": current,
        "max_qty": max_qty,
        "enabled": bool(enabled),
        "issue_found": bool(issue),
        "refill_needed": max(0, max_qty - current),
        "price": pick("price", "sellingPrice", default=0.0) or 0.0,
        "status": status,
    }


# ----------------------------- Auth -----------------------------
class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(body: LoginIn):
    try:
        r = httpx.post(
            f"{VENDOLITE_BASE}/auth/login",
            json={"username": body.username, "password": body.password},
            timeout=30.0,
        )
        if r.status_code != 200:
            raise HTTPException(401, "Invalid credentials")
        data = r.json()
        # Vendolite returns the token in various shapes; cover them
        token = (
            data.get("token")
            or data.get("accessToken")
            or (data.get("data") or {}).get("token")
        )
        if not token:
            raise HTTPException(401, "Login response missing token")
        return {"token": token}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Login failed: {e}")


# ----------------------------- Machines -----------------------------
@app.get("/machines/list")
def list_machines(token: str = Query(...)):
    """
    Resilient machine listing. Important:
    - We DO NOT drop a machine just because one optional field is missing.
    - We catch per-machine normalisation errors so one bad row can't
      hide the rest from the app.
    """
    try:
        r = httpx.post(
            f"{VENDOLITE_BASE}/machine/getAllMachines",
            headers={"Authorization": f"Bearer {token}"},
            json={},
            timeout=30.0,
        )
        if r.status_code != 200:
            raise HTTPException(r.status_code, "Failed to fetch machines")
        payload = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Machine list error: {e}")

    raw_list = []
    if isinstance(payload, dict):
        for key in ("data", "result", "machines"):
            if key in payload and isinstance(payload[key], list):
                raw_list = payload[key]
                break
    elif isinstance(payload, list):
        raw_list = payload

    machines = []
    skipped = []
    for m in raw_list:
        try:
            mid = m.get("id") or m.get("machineId") or m.get("machine_id")
            display = (
                m.get("display_id")
                or m.get("displayId")
                or m.get("name")
                or (str(mid) if mid else "?")
            )
            address = (
                m.get("address")
                or m.get("location")
                or m.get("siteName")
                or ""
            )
            if mid is None:
                # We need at least an id to be useful
                skipped.append({"reason": "missing id", "raw": str(m)[:120]})
                continue
            machines.append(
                {
                    "id": int(mid),
                    "display_id": str(display),
                    "address": str(address),
                }
            )
        except Exception as e:
            skipped.append({"reason": str(e), "raw": str(m)[:120]})

    return {"machines": machines, "skipped": skipped, "count": len(machines)}


@app.get("/machines/slots/{machine_id}")
def machine_slots(
    machine_id: int,
    token: str = Query(...),
    display_id: Optional[str] = Query(None),
):
    raw = _vendolite_slots(token, machine_id)
    slots = [_normalise_slot(s) for s in raw]
    slots.sort(key=_natural_slot_key)
    return {
        "machine_id": machine_id,
        "display_id": display_id or str(machine_id),
        "slots": slots,
    }


# ----------------------------- PDF -----------------------------
def _row_letter(idx_one_based: int) -> str:
    """1->A, 2->B, ... 27->AA."""
    n = idx_one_based
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _machine_section(story, machine_label, address, slots, styles):
    story.append(Paragraph(f"<b>Machine:</b> {machine_label}", styles["h1"]))
    if address:
        story.append(Paragraph(f"<b>Address:</b> {address}", styles["meta"]))
    story.append(
        Paragraph(
            f"<b>Generated:</b> {datetime.now().strftime('%d %b %Y, %H:%M')}",
            styles["meta"],
        )
    )
    story.append(Spacer(1, 6))

    if not slots:
        story.append(Paragraph("<i>No slot data available.</i>", styles["meta"]))
        return

    # Summary line — actionable totals only
    total = len(slots)
    needs_refill_count = 0
    disabled_count = 0
    total_refill = 0
    total_capacity = 0
    total_current = 0
    for s in slots:
        st = (s.get("status") or "").lower()
        if st == "disabled":
            disabled_count += 1
            continue
        if st in ("empty", "low", "issue"):
            needs_refill_count += 1
        cur = int(s.get("current_qty", 0) or 0)
        mx = int(s.get("max_qty", 0) or 0)
        total_current += cur
        total_capacity += mx
        total_refill += max(0, mx - cur)

    summary_text = (
        f"Total slots: <b>{total}</b> &nbsp; "
        f"Current: <b>{total_current}</b> / <b>{total_capacity}</b> &nbsp; "
        f"Units to refill: <b>{total_refill}</b>"
    )
    breakdown_text = (
        f"Slots needing refill: <b>{needs_refill_count}</b> &nbsp; "
        f"Disabled: <b>{disabled_count}</b>"
    )
    story.append(Paragraph(summary_text, styles["meta"]))
    story.append(Paragraph(breakdown_text, styles["meta"]))
    story.append(Spacer(1, 12))

    # Group slots by row_number (or derived row), preserving order
    rows: dict = {}
    for s in slots:
        r = s.get("row_number")
        if not isinstance(r, int):
            r, _ = _natural_slot_key(s)
        rows.setdefault(r, []).append(s)
    sorted_row_keys = sorted(rows.keys())

    page_w = A4[0] - 24 * mm
    # Slot | Product | Current | Max | Refill
    col_widths = [
        page_w * 0.12,  # slot
        page_w * 0.52,  # product (lots of room — no truncation)
        page_w * 0.10,  # current
        page_w * 0.10,  # max
        page_w * 0.16,  # refill
    ]

    for r_key in sorted_row_keys:
        row_slots = sorted(rows[r_key], key=_natural_slot_key)
        letter = _row_letter(r_key) if r_key < 1000 else "?"

        # Row header bar
        row_needs = sum(
            1 for s in row_slots
            if (s.get("status") or "").lower() in ("empty", "low", "issue")
        )
        row_refill_units = sum(
            max(0, int(s.get("max_qty", 0) or 0) - int(s.get("current_qty", 0) or 0))
            for s in row_slots
            if (s.get("status") or "").lower() != "disabled"
        )
        header_para = Paragraph(
            f'<font size="13" color="white"><b>Row {letter}</b></font> &nbsp;&nbsp; '
            f'<font size="9" color="#D6E8FF">'
            f'{len(row_slots)} slots &nbsp; • &nbsp; '
            f'{row_needs} need refill &nbsp; • &nbsp; '
            f'{row_refill_units} units</font>',
            styles["row_band"],
        )
        header_tbl = Table(
            [[header_para]],
            colWidths=[page_w],
            style=TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#005FCC")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            ),
        )
        story.append(header_tbl)

        # Slot table for this row
        data = [
            [
                Paragraph("<b>Slot</b>", styles["th"]),
                Paragraph("<b>Product</b>", styles["th"]),
                Paragraph("<b>Current</b>", styles["th"]),
                Paragraph("<b>Max</b>", styles["th"]),
                Paragraph("<b>Refill</b>", styles["th"]),
            ]
        ]
        for s in row_slots:
            st = (s.get("status") or "").lower()
            name = s.get("slot_name", "?")
            product = s.get("product_name") or "-"
            if st == "disabled":
                data.append(
                    [
                        Paragraph(f"<b>{name}</b>", styles["td"]),
                        Paragraph("<i>disabled</i>", styles["td_muted"]),
                        Paragraph("—", styles["td_muted"]),
                        Paragraph("—", styles["td_muted"]),
                        Paragraph("—", styles["td_muted"]),
                    ]
                )
                continue
            cur = int(s.get("current_qty", 0) or 0)
            mx = int(s.get("max_qty", 0) or 0)
            refill = max(0, mx - cur)
            refill_cell = (
                f'<font size="11"><b>{refill}</b></font>'
                if refill > 0
                else '<font color="#999999">0</font>'
            )
            data.append(
                [
                    Paragraph(f"<b>{name}</b>", styles["td"]),
                    Paragraph(product, styles["td"]),
                    Paragraph(f'<font size="11"><b>{cur}</b></font>', styles["td"]),
                    Paragraph(str(mx), styles["td"]),
                    Paragraph(refill_cell, styles["td"]),
                ]
            )

        t = Table(data, colWidths=col_widths, repeatRows=1)
        ts = TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EAF2FF")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (1, 0), (1, -1), "LEFT"),
                ("ALIGN", (2, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
        # Tint rows that need refill
        for i, s in enumerate(row_slots, start=1):
            st = (s.get("status") or "").lower()
            if st in ("empty", "low", "issue"):
                ts.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FFC2C2"))
            elif st == "disabled":
                ts.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#E0E0E0"))
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 10))

    # Footer legend
    story.append(Spacer(1, 4))
    legend = (
        '<font backcolor="#FFC2C2"><b>&nbsp;&nbsp;&nbsp;&nbsp;</b></font> Needs refill &nbsp;&nbsp;&nbsp; '
        '<font backcolor="#E0E0E0"><b>&nbsp;&nbsp;&nbsp;&nbsp;</b></font> Disabled &nbsp;&nbsp;&nbsp; '
        'White = OK'
    )
    story.append(Paragraph(legend, styles["meta"]))


def _build_pdf(machines: List[dict]) -> bytes:
    """machines = [{id, display_id, address, slots:[...]}, ...]"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title="Vendagon Refill Report",
    )

    base = getSampleStyleSheet()
    styles = {
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontSize=14, spaceAfter=2),
        "meta": ParagraphStyle("meta", parent=base["Normal"], fontSize=9, leading=11),
        "row_band": ParagraphStyle("row_band", parent=base["Normal"], fontSize=11, leading=14, textColor=colors.white),
        "th": ParagraphStyle("th", parent=base["Normal"], fontSize=9, alignment=1, textColor=colors.HexColor("#003B80")),
        "td": ParagraphStyle("td", parent=base["Normal"], fontSize=9, leading=12),
        "td_muted": ParagraphStyle("td_muted", parent=base["Normal"], fontSize=9, leading=12, textColor=colors.HexColor("#888888")),
    }

    story = []
    for i, m in enumerate(machines):
        if i > 0:
            story.append(PageBreak())
        label = m.get("display_id") or str(m.get("id"))
        _machine_section(story, label, m.get("address", ""), m.get("slots", []), styles)

    doc.build(story)
    return buf.getvalue()


def _pdf_filename() -> str:
    now = datetime.now()
    return now.strftime("%b%d_%H%M") + ".pdf"


@app.get("/refill/pdf/machine/{machine_id}")
def pdf_single(
    machine_id: int,
    token: str = Query(...),
    display_id: Optional[str] = Query(None),
    address: Optional[str] = Query(None),
):
    raw = _vendolite_slots(token, machine_id)
    slots = [_normalise_slot(s) for s in raw]
    slots.sort(key=_natural_slot_key)
    pdf = _build_pdf(
        [
            {
                "id": machine_id,
                "display_id": display_id or str(machine_id),
                "address": address or "",
                "slots": slots,
            }
        ]
    )
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_pdf_filename()}"'},
    )


class SelectedIn(BaseModel):
    machine_ids: List[int]
    display_ids: List[str] = []
    addresses: List[str] = []


@app.post("/refill/pdf/selected")
def pdf_selected(body: SelectedIn, token: str = Query(...)):
    machines = []
    for i, mid in enumerate(body.machine_ids):
        raw = _vendolite_slots(token, mid)
        slots = [_normalise_slot(s) for s in raw]
        slots.sort(key=_natural_slot_key)
        machines.append(
            {
                "id": mid,
                "display_id": body.display_ids[i] if i < len(body.display_ids) else str(mid),
                "address": body.addresses[i] if i < len(body.addresses) else "",
                "slots": slots,
            }
        )
    pdf = _build_pdf(machines)
    return StreamingResponse(
        io.BytesIO(pdf),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_pdf_filename()}"'},
    )


# ----------------------------- Groups (Supabase) -----------------------------
def _sb_headers():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Supabase not configured")
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


class GroupIn(BaseModel):
    name: str
    machine_ids: List[int] = []
    display_ids: List[str] = []
    addresses: List[str] = []


@app.get("/groups")
def list_groups():
    r = httpx.get(
        f"{SUPABASE_URL}/rest/v1/machine_groups?select=*&order=created_at.desc",
        headers=_sb_headers(),
        timeout=30.0,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.post("/groups")
def create_group(body: GroupIn):
    r = httpx.post(
        f"{SUPABASE_URL}/rest/v1/machine_groups",
        headers=_sb_headers(),
        json=body.model_dump(),
        timeout=30.0,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.put("/groups/{group_id}")
def update_group(group_id: str, body: GroupIn):
    r = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/machine_groups?id=eq.{group_id}",
        headers=_sb_headers(),
        json=body.model_dump(),
        timeout=30.0,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


@app.delete("/groups/{group_id}")
def delete_group(group_id: str):
    r = httpx.delete(
        f"{SUPABASE_URL}/rest/v1/machine_groups?id=eq.{group_id}",
        headers=_sb_headers(),
        timeout=30.0,
    )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return {"deleted": True}


# ----------------------------- Health -----------------------------
@app.get("/")
def root():
    return {"ok": True, "service": "vendagon-refill-backend"}
