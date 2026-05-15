"""
Vendagon Refill Backend
- Proxies Vendolite API (login, machines, slots)
- Generates row-grouped PDF refill reports (parallel slot fetching)
- Manages machine_groups via Supabase
"""

import os
import io
import re
import logging
import requests
from datetime import datetime
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytz
from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

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
    version="2.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Acceptable: API consumed by mobile app only.
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

# Health % thresholds — single source of truth, matched by the Flutter client.
HEALTH_THRESHOLD_CRITICAL = 30.0
HEALTH_THRESHOLD_LOW = 60.0

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


class GroupRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    machine_ids: List[int] = []
    display_ids: List[str] = []
    addresses: List[str] = []


# ── Token extraction (accepts both header and legacy query param) ────────
def extract_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> str:
    """Prefer Authorization: Bearer; fall back to ?token=... for back-compat
    with older clients still in the field. New code should always use the
    header form to keep tokens out of access logs."""
    if authorization:
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return authorization.strip()
    if token:
        return token
    raise HTTPException(status_code=401, detail="Missing auth token")


# ── Vendolite helpers ────────────────────────────────────────────────────
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
    if "data" in result:
        if isinstance(result["data"], list):
            return result["data"]
        if isinstance(result["data"], dict) and "machines" in result["data"]:
            return result["data"]["machines"]
    return []


def fetch_machine_slots(token: str, machine_id: int) -> list:
    url = f"{VENDOLITE_BASE_URL}/machineSlot/getAllSlots"
    resp = requests.post(
        url,
        json={"machineId": machine_id},
        headers=get_auth_header(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])


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


def _product_name(raw: dict) -> str:
    """Vendolite returns product info either as a nested object OR as
    dotted-key flat fields. Try both."""
    # Flat dotted key (what we've observed in production)
    flat = raw.get("client_level_product.name")
    if flat:
        return str(flat)
    # Nested object (defensive)
    nested = raw.get("client_level_product")
    if isinstance(nested, dict):
        n = nested.get("name")
        if n:
            return str(n)
    return "Unknown"


def normalize_slot(raw: dict) -> Optional[dict]:
    """Turn a raw Vendolite slot into the canonical shape used by the PDF."""
    if raw.get("slotWidth", 0) == 0 and raw.get("enable", 0) == 0:
        return None

    stock_list = raw.get("stock") or []
    current_qty = 0
    if isinstance(stock_list, list):
        for s in stock_list:
            if isinstance(s, dict):
                for k in ("qty", "quantity", "currentQty"):
                    if k in s and s[k] is not None:
                        try:
                            current_qty += int(s[k])
                        except (TypeError, ValueError):
                            pass
                        break
    # Fall back to top-level current* fields if stock array is empty
    if current_qty == 0:
        for k in ("currentStock", "currentQty", "qty", "quantity"):
            v = raw.get(k)
            if v is not None:
                try:
                    current_qty = int(v)
                    break
                except (TypeError, ValueError):
                    pass

    max_qty = raw.get("stockLimit") or 1
    try:
        max_qty = int(max_qty)
    except (TypeError, ValueError):
        max_qty = 1
    if max_qty <= 0:
        max_qty = 1

    enabled = bool(raw.get("enable", 0))
    issue = bool(raw.get("slotIssueFound", 0))
    status = parse_slot_status(current_qty, max_qty, enabled, issue)

    return {
        "slot_name": raw.get("slotName", "?"),
        "row_number": raw.get("rowNumber"),
        "column_number": raw.get("coloumnNumber"),  # Vendolite's spelling
        "product_name": _product_name(raw),
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
    except requests.HTTPError as exc:
        # Don't mask infrastructure errors as auth failures.
        status = getattr(getattr(exc, "response", None), "status_code", 500)
        if status in (401, 403):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        raise HTTPException(
            status_code=502,
            detail=f"Vendolite returned an error ({status})",
        )
    except requests.RequestException as e:
        logger.error(f"Login request failed: {e}")
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")


# ── Machines ─────────────────────────────────────────────────────────────
@app.get("/machines/list")
def get_machine_list(request: Request):
    token = extract_token(request, request.headers.get("authorization"))
    try:
        machines = fetch_machines(token)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 500)
        if status == 401:
            raise HTTPException(status_code=401, detail="Unauthorized")
        raise HTTPException(status_code=502, detail="Vendolite error")
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
def get_machine_slots(machine_id: int, request: Request, display_id: str = ""):
    token = extract_token(request, request.headers.get("authorization"))
    try:
        raw_slots = fetch_machine_slots(token, machine_id)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 500)
        if status == 401:
            raise HTTPException(status_code=401, detail="Unauthorized")
        raise HTTPException(status_code=502, detail="Vendolite error")
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


# ── PDF generator (row-grouped design) ──────────────────────────────────
def _natural_slot_key(s: dict):
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
    """1→A, 2→B, ..., 26→Z, 27→AA. Matches Flutter _rowLabel."""
    if n <= 0:
        return "?"
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(ord("A") + r) + s
    return s


def _machine_section(story, label, address, slots, styles):
    ist = pytz.timezone("Asia/Kolkata")
    now = datetime.now(ist).strftime("%d %b %Y, %I:%M %p IST")

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
                data.append([
                    Paragraph(f"<b>{name}</b>", styles["td"]),
                    Paragraph("<i>disabled</i>", styles["td_muted"]),
                    Paragraph("—", styles["td_muted"]),
                    Paragraph("—", styles["td_muted"]),
                    Paragraph("—", styles["td_muted"]),
                ])
                continue
            cur = int(s.get("current_qty", 0) or 0)
            mx = int(s.get("max_qty", 0) or 0)
            refill = max(0, mx - cur)
            refill_cell = (
                f'<font size="11"><b>{refill}</b></font>'
                if refill > 0
                else '<font color="#999999">0</font>'
            )
            data.append([
                Paragraph(f"<b>{name}</b>", styles["td"]),
                Paragraph(product, styles["td"]),
                Paragraph(f'<font size="11"><b>{cur}</b></font>', styles["td"]),
                Paragraph(str(mx), styles["td"]),
                Paragraph(refill_cell, styles["td"]),
            ])

        t = Table(data, colWidths=col_widths, repeatRows=1)
        ts = TableStyle([
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
        ])
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
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
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


class _AuthExpired(Exception):
    """Raised when a Vendolite call returns 401 mid-batch — surfaces all the
    way up so the user can re-login."""


def _build_machine_data(token: str, machine_id: int, display_id: str, address: str) -> dict:
    try:
        raw_slots = fetch_machine_slots(token, machine_id)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 500)
        if status == 401:
            raise _AuthExpired()
        raise
    slots = [s for s in (normalize_slot(r) for r in raw_slots) if s]
    return {
        "machine_id": machine_id,
        "machine_display_id": display_id or str(machine_id),
        "address": address or "",
        "slots": slots,
    }


def _build_many_machines_parallel(
    token: str,
    triples: List[tuple],  # [(machine_id, display_id, address), ...]
    max_workers: int = 12,
) -> List[dict]:
    """Fetch slots for many machines in parallel. Preserves input order.
    Raises _AuthExpired immediately on the first 401 (so we don't waste calls)."""
    if not triples:
        return []
    results: List[Optional[dict]] = [None] * len(triples)
    workers = min(max_workers, len(triples))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_build_machine_data, token, mid, did, addr): i
            for i, (mid, did, addr) in enumerate(triples)
        }
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except _AuthExpired:
                # Cancel anything still queued and bubble up
                for f in futures:
                    f.cancel()
                raise
            except Exception as e:
                logger.warning(f"Skipped machine in PDF batch: {e}")
                results[idx] = None
    return [r for r in results if r is not None]


def _pdf_filename(prefix: str = "refill") -> str:
    ist = pytz.timezone("Asia/Kolkata")
    return datetime.now(ist).strftime(f"{prefix}_%b%d_%H%M.pdf")


# ── PDF endpoints ────────────────────────────────────────────────────────
@app.get("/refill/pdf/machine/{machine_id}")
def download_machine_refill_pdf(
    machine_id: int,
    request: Request,
    display_id: str = "",
    address: str = "",
):
    token = extract_token(request, request.headers.get("authorization"))
    try:
        data = _build_machine_data(token, machine_id, display_id, address)
    except _AuthExpired:
        raise HTTPException(status_code=401, detail="Session expired")
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 500)
        raise HTTPException(status_code=502, detail=f"Vendolite error ({status})")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    if not data.get("slots"):
        raise HTTPException(status_code=422, detail="Machine has no slot data")

    pdf_bytes = generate_refill_pdf([data])
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_pdf_filename()}"'},
    )


@app.post("/refill/pdf/selected")
def download_selected_refill_pdf(body: SelectedMachinesRequest, request: Request):
    token = extract_token(request, request.headers.get("authorization"))
    if not body.machine_ids:
        raise HTTPException(status_code=422, detail="No machines selected")

    triples = []
    for i, mid in enumerate(body.machine_ids):
        did = body.display_ids[i] if i < len(body.display_ids) else str(mid)
        addr = body.addresses[i] if i < len(body.addresses) else ""
        triples.append((mid, did, addr))

    try:
        all_data = _build_many_machines_parallel(token, triples)
    except _AuthExpired:
        raise HTTPException(status_code=401, detail="Session expired")

    if not all_data:
        raise HTTPException(
            status_code=422,
            detail="No machine data could be fetched. Try again or check connectivity.",
        )

    pdf_bytes = generate_refill_pdf(all_data)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_pdf_filename()}"'},
    )


# ── Stock summary (kept for future use / dashboards) ────────────────────
def _machine_stock_summary(token: str, machine: dict) -> dict:
    mid = machine.get("id")
    display_id = str(machine.get("machineDisplayId") or mid)
    address = str(machine.get("addressLine1") or machine.get("address") or "")
    base = {
        "id": int(mid) if mid is not None else None,
        "display_id": display_id,
        "address": address,
        "configured": bool(address.strip()),
    }
    try:
        raw_slots = fetch_machine_slots(token, mid)
    except Exception as e:
        logger.warning(f"Stock summary skip {mid}: {e}")
        return {
            **base,
            "total_slots": 0, "active_slots": 0,
            "empty_slots": 0, "low_slots": 0, "good_slots": 0, "issue_slots": 0,
            "refill_needed": 0, "fill_percent": 0.0, "health": "unknown",
            "fetch_failed": True,
        }

    empty = low = good = issue = disabled = 0
    total_cur = total_cap = total_refill = 0
    for raw in raw_slots:
        norm = normalize_slot(raw)
        if not norm:
            continue
        st = norm["status"]
        if st == "disabled":
            disabled += 1
            continue
        if st == "empty":
            empty += 1
        elif st == "low":
            low += 1
        elif st == "good":
            good += 1
        elif st == "issue":
            issue += 1
        total_cur += int(norm.get("current_qty", 0) or 0)
        total_cap += int(norm.get("max_qty", 0) or 0)
        total_refill += int(norm.get("refill_needed", 0) or 0)

    active = empty + low + good + issue
    fill_pct = round((total_cur / total_cap * 100), 1) if total_cap > 0 else 0.0

    if active == 0 or total_cap == 0:
        health = "unknown"
    elif fill_pct < HEALTH_THRESHOLD_CRITICAL:
        health = "critical"
    elif fill_pct < HEALTH_THRESHOLD_LOW:
        health = "low"
    else:
        health = "good"

    return {
        **base,
        "total_slots": active + disabled,
        "active_slots": active,
        "empty_slots": empty, "low_slots": low, "good_slots": good,
        "issue_slots": issue, "disabled_slots": disabled,
        "refill_needed": total_refill,
        "fill_percent": fill_pct,
        "health": health,
        "fetch_failed": False,
    }


@app.get("/machines/stock/summary")
def stock_summary(request: Request):
    token = extract_token(request, request.headers.get("authorization"))
    try:
        machines = fetch_machines(token)
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 500)
        if status == 401:
            raise HTTPException(status_code=401, detail="Unauthorized")
        raise HTTPException(status_code=502, detail="Vendolite error")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    if not machines:
        return {
            "totals": {"machines": 0, "critical": 0, "low": 0, "good": 0,
                       "unconfigured": 0, "total_refill_needed": 0},
            "machines": [],
        }

    workers = min(20, len(machines))
    summaries: List[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_machine_stock_summary, token, m): m for m in machines}
        for fut in as_completed(futures):
            try:
                summaries.append(fut.result())
            except Exception as e:
                logger.warning(f"Stock summary future failed: {e}")

    health_rank = {"critical": 0, "low": 1, "good": 2, "unknown": 3}
    summaries.sort(key=lambda s: (
        0 if s.get("configured") else 1,
        health_rank.get(s.get("health"), 4),
        -int(s.get("refill_needed", 0) or 0),
        (s.get("address") or "").lower(),
        s.get("display_id", ""),
    ))
    totals = {
        "machines": len(summaries),
        "critical": sum(1 for s in summaries if s.get("configured") and s["health"] == "critical"),
        "low": sum(1 for s in summaries if s.get("configured") and s["health"] == "low"),
        "good": sum(1 for s in summaries if s.get("configured") and s["health"] == "good"),
        "unconfigured": sum(1 for s in summaries if not s.get("configured")),
        "total_refill_needed": sum(int(s.get("refill_needed", 0) or 0) for s in summaries),
    }
    return {"totals": totals, "machines": summaries}


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
def create_group(body: GroupRequest):
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/machine_groups",
        headers=supabase_headers(),
        json=body.model_dump() if hasattr(body, "model_dump") else body.dict(),
        timeout=10,
    )
    resp.raise_for_status()
    return {"data": resp.json()}


@app.put("/groups/{group_id}")
def update_group(group_id: str, body: GroupRequest):
    if not SUPABASE_URL:
        raise HTTPException(status_code=503, detail="Supabase not configured")
    resp = requests.patch(
        f"{SUPABASE_URL}/rest/v1/machine_groups?id=eq.{group_id}",
        headers=supabase_headers(),
        json=body.model_dump() if hasattr(body, "model_dump") else body.dict(),
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


# Note: /debug/raw_slots endpoint has been removed.
# It was useful during initial debugging but is a data-exposure risk in
# production. If you need to inspect raw Vendolite payloads, add a temporary
# endpoint guarded by a secret header, then remove it.
