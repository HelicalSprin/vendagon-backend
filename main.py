"""
Vendagon Refill Backend
- Proxies Vendolite API (login, machines, slots)
- Generates row-grouped PDF refill reports
- Manages machine_groups via Supabase
"""

import os
import io
import re
import logging
import requests
from datetime import datetime
from typing import List, Optional

import pytz
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
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

# ── Logging ──────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Vendagon Refill API",
    description="Backend for Vendagon Refill Mobile App",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ───────────────────────────────────────────────────────────────
VENDOLITE_BASE_URL = os.getenv(
    "VENDOLITE_BASE_URL", "https://ecloud.vendolite.com/api"
)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# ── Models ───────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    message: str


class SelectedMachinesRequest(BaseModel):
    machine_ids: List[int]
    display_ids: List[str] = []
    addresses: List[str] = []


# ── Vendolite helpers (kept identical to working version) ───────────────
def get_auth_header(token: str) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "user-agent": "VendoliteApp/1.0",
    }


def fetch_machines(token: str, limit: int = 1000) -> list:
    url = f"{VENDOLITE_BASE_URL}/machine/getList"
    payload = {
        "searchTypeSelected": "Machine Id",
        "searchText": "",
        "filterOptions": [],
        "sortSelected": "id",
        "sortDirection": "DESC",
        "limit": limit,
        "currentPage": 0,
    }
    resp = requests.post(url, json=payload, headers=get_auth_header(token), timeout=30)
    resp.raise_for_status()
    result = resp.json()

    machines = []
    if "data" in result:
        if isinstance(result["data"], list):
            machines = result["data"]
        elif isinstance(result["data"], dict) and "machines" in result["data"]:
            machines = result["data"]["machines"]
    return machines


def fetch_machine_slots(token: str, machine_id: int) -> list:
    url = f"{VENDOLITE_BASE_URL}/machineSlot/getAllSlots"
    resp = requests.post(
        url,
        json={"machineId": machine_id},
        headers=get_auth_header(token),
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    return result.get("data", [])


def parse_slot_status(qty: int, max_qty: int, enabled: bool, issue: bool) -> str:
    if not enabled:
        return "disabled"
    if issue:
        return "issue"
    if qty == 0:
        return "empty"
    if max_qty > 0 and qty / max_qty < 0.5:
        return "low"
    return "good"


def _extract_current_qty(raw: dict) -> int:
    """Vendolite returns slot quantity in different places depending on the
    machine/firmware. Try every known location and use the first non-zero one,
    falling back to 0 only if nothing is set anywhere.
    """
    # 1. Nested stock array — sum of qty across stock entries
    stock_list = raw.get("stock") or []
    if isinstance(stock_list, list) and stock_list:
        total = 0
        found_any = False
        for st in stock_list:
            if not isinstance(st, dict):
                continue
            for key in ("qty", "quantity", "currentQty", "stockQty", "remainingQty"):
                if key in st and st[key] is not None:
                    try:
                        total += int(st[key])
                        found_any = True
                        break
                    except (TypeError, ValueError):
                        pass
        if found_any:
            return total

    # 2. Top-level fields on the slot itself
    for key in (
        "currentQty", "current_qty", "currentQuantity",
        "qty", "quantity",
        "stockQty", "remainingQty",
        "currentStock", "stockCount",
    ):
        if key in raw and raw[key] is not None:
            try:
                return int(raw[key])
            except (TypeError, ValueError):
                pass

    return 0


def _extract_max_qty(raw: dict) -> int:
    for key in ("stockLimit", "maxQty", "max_qty", "capacity", "maxCapacity"):
        if key in raw and raw[key] is not None:
            try:
                v = int(raw[key])
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    return 1


def normalize_slot(raw: dict) -> Optional[dict]:
    """Turn a raw Vendolite slot into the canonical shape used by the PDF."""
    # Skip spacer/disabled-width slots
    if raw.get("slotWidth", 0) == 0 and raw.get("enable", 0) == 0:
        return None

    current_qty = _extract_current_qty(raw)
    max_qty = _extract_max_qty(raw)
    enabled = bool(raw.get("enable", 0))
    issue = bool(raw.get("slotIssueFound", 0))
    status = parse_slot_status(current_qty, max_qty, enabled, issue)

    return {
        "slot_name": raw.get("slotName", "?"),
        "row_number": raw.get("rowNumber"),
        "column_number": raw.get("coloumnNumber"),  # Vendolite's spelling
        "product_name": raw.get("client_level_product.name") or "Unknown",
        "current_qty": current_qty,
        "max_qty": max_qty,
        "enabled": enabled,
        "issue_found": issue,
        "refill_needed": max(0, max_qty - current_qty) if enabled else 0,
        "status": status,
    }


# ── Auth ─────────────────────────────────────────────────────────────────
@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest):
    """Authenticate with Vendolite and return a bearer token."""
    url = f"{VENDOLITE_BASE_URL}/company/login"
    try:
        resp = requests.post(
            url,
            json={"username": body.username, "password": body.password},
            headers={"content-type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        if "token" not in result:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return TokenResponse(token=result["token"], message="Login successful")
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except requests.RequestException as e:
        logger.error(f"Login request failed: {e}")
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")


# ── Machines ─────────────────────────────────────────────────────────────
@app.get("/machines/list")
def get_machine_list(token: str):
    """List all machines for the refill screen.

    Resilient: a single bad record won't drop the others.
    Returns {data: [...], skipped: N, count: N}.
    """
    try:
        machines = fetch_machines(token)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    result = []
    skipped = 0
    for m in machines:
        try:
            mid = m.get("id")
            if mid is None:
                skipped += 1
                continue
            result.append(
                {
                    "id": int(mid),
                    "display_id": str(m.get("machineDisplayId") or mid),
                    "address": str(m.get("addressLine1") or m.get("address") or ""),
                    "operation_status": str(m.get("operationStatus") or ""),
                    "cloud_status": str(m.get("cloudStatus") or ""),
                }
            )
        except Exception as e:
            logger.warning(f"Skipped machine due to error: {e}")
            skipped += 1

    return {"data": result, "skipped": skipped, "count": len(result)}


@app.get("/machines/slots/{machine_id}")
def get_machine_slots(machine_id: int, token: str, display_id: str = ""):
    """Return slot grid for one machine."""
    try:
        raw_slots = fetch_machine_slots(token, machine_id)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    slots = []
    for raw in raw_slots:
        norm = normalize_slot(raw)
        if not norm:
            continue
        norm["slot_id"] = raw.get("id")
        norm["product_id"] = raw.get("client_level_product.displayProductId", "")
        norm["price"] = (raw.get("client_level_product.cost") or 0) / 100
        slots.append(norm)

    empty = sum(1 for s in slots if s["status"] == "empty")
    low = sum(1 for s in slots if s["status"] == "low")
    good = sum(1 for s in slots if s["status"] == "good")

    return {
        "machine_id": machine_id,
        "machine_display_id": display_id or str(machine_id),
        "slots": slots,
        "total_slots": len(slots),
        "empty_slots": empty,
        "low_slots": low,
        "good_slots": good,
    }


# ── PDF generator (new row-grouped design) ──────────────────────────────
def _natural_slot_key(s: dict):
    """Sort by (row, column) for natural A1, A2, ... B1, B2 order."""
    r = s.get("row_number")
    c = s.get("column_number")
    if isinstance(r, int) and isinstance(c, int):
        return (r, c)
    name = (s.get("slot_name") or "").strip().upper()
    m = re.match(r"([A-Z]+)\s*[Xx]?\s*(\d+)", name)
    if m:
        letters, num = m.group(1), int(m.group(2))
        row_idx = 0
        for ch in letters:
            row_idx = row_idx * 26 + (ord(ch) - ord("A") + 1)
        return (row_idx, num)
    return (9999, 9999)


def _row_letter(n: int) -> str:
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _machine_section(story, label, address, slots, styles):
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist).strftime("%d %b %Y, %I:%M %p IST")

    # Plain text header — machine ID + name on top line, timestamp below
    name_line = label
    if address:
        name_line = f"{label}  —  {address}"
    story.append(Paragraph(f"<b>{name_line}</b>", styles["machine_header"]))
    story.append(Paragraph(now, styles["machine_sub"]))
    story.append(Spacer(1, 10))

    if not slots:
        story.append(Paragraph("<i>No slot data available.</i>", styles["meta"]))
        return

    slots = sorted(slots, key=_natural_slot_key)

    rows: dict = {}
    for s in slots:
        r = s.get("row_number")
        if not isinstance(r, int):
            r, _ = _natural_slot_key(s)
        rows.setdefault(r, []).append(s)

    page_w = A4[0] - 30 * mm
    col_widths = [
        page_w * 0.12,
        page_w * 0.52,
        page_w * 0.10,
        page_w * 0.10,
        page_w * 0.16,
    ]

    for r_key in sorted(rows.keys()):
        row_slots = sorted(rows[r_key], key=_natural_slot_key)
        letter = _row_letter(r_key) if r_key < 1000 else "?"

        row_needs = sum(
            1 for s in row_slots
            if (s.get("status") or "").lower() in ("empty", "low", "issue")
        )
        row_refill_units = sum(
            max(0, int(s.get("max_qty", 0) or 0) - int(s.get("current_qty", 0) or 0))
            for s in row_slots
            if (s.get("status") or "").lower() != "disabled"
        )

        banner = Paragraph(
            f'<font size="13" color="white"><b>Row {letter}</b></font> &nbsp;&nbsp; '
            f'<font size="9" color="#D6E8FF">'
            f'{len(row_slots)} slots &nbsp; • &nbsp; '
            f'{row_needs} need refill &nbsp; • &nbsp; '
            f'{row_refill_units} units</font>',
            styles["row_band"],
        )
        banner_tbl = Table(
            [[banner]],
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
        story.append(banner_tbl)

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
        for i, s in enumerate(row_slots, start=1):
            st = (s.get("status") or "").lower()
            if st in ("empty", "low", "issue"):
                ts.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#FFC2C2"))
            elif st == "disabled":
                ts.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#E0E0E0"))
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 10))

    story.append(Spacer(1, 4))
    legend = (
        '<font backcolor="#FFC2C2"><b>&nbsp;&nbsp;&nbsp;&nbsp;</b></font> Needs refill &nbsp;&nbsp;&nbsp; '
        '<font backcolor="#E0E0E0"><b>&nbsp;&nbsp;&nbsp;&nbsp;</b></font> Disabled &nbsp;&nbsp;&nbsp; '
        'White = OK'
    )
    story.append(Paragraph(legend, styles["meta"]))


def generate_refill_pdf(machines_data: List[dict]) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Vendagon Refill Report",
    )

    base = getSampleStyleSheet()
    styles = {
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontSize=14, spaceAfter=2),
        "meta": ParagraphStyle("meta", parent=base["Normal"], fontSize=9, leading=11),
        "machine_header": ParagraphStyle("machine_header", parent=base["Normal"], fontSize=18, leading=22, textColor=colors.black),
        "machine_sub": ParagraphStyle("machine_sub", parent=base["Normal"], fontSize=10, leading=12, textColor=colors.HexColor("#666666")),
        "row_band": ParagraphStyle("row_band", parent=base["Normal"], fontSize=11, leading=14, textColor=colors.white),
        "th": ParagraphStyle("th", parent=base["Normal"], fontSize=9, alignment=1, textColor=colors.HexColor("#003B80")),
        "td": ParagraphStyle("td", parent=base["Normal"], fontSize=9, leading=12),
        "td_muted": ParagraphStyle("td_muted", parent=base["Normal"], fontSize=9, leading=12, textColor=colors.HexColor("#888888")),
    }

    story = []
    for i, m in enumerate(machines_data):
        if i > 0:
            story.append(PageBreak())
        _machine_section(
            story,
            m.get("machine_display_id") or str(m.get("machine_id", "?")),
            m.get("address", ""),
            m.get("slots", []),
            styles,
        )

    doc.build(story)
    return buf.getvalue()


def _build_machine_data(token: str, machine_id: int, display_id: str, address: str) -> dict:
    raw_slots = fetch_machine_slots(token, machine_id)
    slots = [s for s in (normalize_slot(r) for r in raw_slots) if s]
    return {
        "machine_id": machine_id,
        "machine_display_id": display_id or str(machine_id),
        "address": address or "",
        "slots": slots,
    }


def _pdf_filename(prefix: str = "refill") -> str:
    ist = pytz.timezone("Asia/Kolkata")
    return datetime.now(ist).strftime(f"{prefix}_%b%d_%H%M.pdf")


# ── PDF endpoints ────────────────────────────────────────────────────────
@app.get("/refill/pdf/machine/{machine_id}")
def download_machine_refill_pdf(
    machine_id: int, token: str, display_id: str = "", address: str = ""
):
    try:
        data = _build_machine_data(token, machine_id, display_id, address)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    pdf_bytes = generate_refill_pdf([data])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_pdf_filename()}"'
        },
    )


@app.post("/refill/pdf/selected")
def download_selected_refill_pdf(body: SelectedMachinesRequest, token: str):
    all_data = []
    for i, mid in enumerate(body.machine_ids):
        display_id = body.display_ids[i] if i < len(body.display_ids) else str(mid)
        address = body.addresses[i] if i < len(body.addresses) else ""
        try:
            all_data.append(_build_machine_data(token, mid, display_id, address))
        except Exception as e:
            logger.warning(f"Skipped machine {mid} in selected PDF: {e}")
            continue
    pdf_bytes = generate_refill_pdf(all_data)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_pdf_filename()}"'
        },
    )


@app.post("/refill/pdf/all")
def download_all_refill_pdf(token: str):
    try:
        machines = fetch_machines(token)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    all_data = []
    for m in machines:
        mid = m.get("id")
        if mid is None:
            continue
        display_id = str(m.get("machineDisplayId") or mid)
        address = str(m.get("addressLine1") or m.get("address") or "")
        try:
            all_data.append(_build_machine_data(token, mid, display_id, address))
        except Exception as e:
            logger.warning(f"Skipped machine {mid} in all-PDF: {e}")
            continue
    pdf_bytes = generate_refill_pdf(all_data)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{_pdf_filename("all")}"'
        },
    )


# ── Machine Groups (Supabase) ───────────────────────────────────────────
def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


@app.get("/groups")
def get_groups():
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/machine_groups?select=*&order=created_at.desc",
        headers=supabase_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return {"data": resp.json()}


@app.post("/groups")
def create_group(body: dict):
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    payload = {
        "name": body.get("name"),
        "machine_ids": body.get("machine_ids", []),
        "display_ids": body.get("display_ids", []),
        "addresses": body.get("addresses", []),
    }
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/machine_groups",
        headers=supabase_headers(),
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return {"data": resp.json()}


@app.put("/groups/{group_id}")
def update_group(group_id: str, body: dict):
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    payload = {
        "name": body.get("name"),
        "machine_ids": body.get("machine_ids", []),
        "display_ids": body.get("display_ids", []),
        "addresses": body.get("addresses", []),
    }
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/machine_groups?id=eq.{group_id}",
        headers=supabase_headers(),
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return {"success": True}


@app.delete("/groups/{group_id}")
def delete_group(group_id: str):
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    resp = requests.delete(
        f"{SUPABASE_URL}/rest/v1/machine_groups?id=eq.{group_id}",
        headers=supabase_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return {"success": True}


# ── Health ───────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return {"ok": True, "service": "vendagon-refill-backend"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/debug/raw_slots/{machine_id}")
def debug_raw_slots(machine_id: int, token: str):
    """Return the unparsed Vendolite slot payload so we can debug field shapes."""
    try:
        raw = fetch_machine_slots(token, machine_id)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")
    # Return only the first 3 slots to keep the response small
    return {"machine_id": machine_id, "sample_slots": raw[:3], "total": len(raw)}
