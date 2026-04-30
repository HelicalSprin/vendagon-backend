import os
import io
import base64
import logging
import requests
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from functools import lru_cache
import json

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Vendolite API",
    description="Backend for Vendolite Stock Management Mobile App",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Config ────────────────────────────────────────────────────────────────────
VENDOLITE_BASE_URL = os.getenv("VENDOLITE_BASE_URL", "https://ecloud.vendolite.com/api")

# ── Models ────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    token: str
    message: str

class StatusSummary(BaseModel):
    total_machines: int
    cloud_connected: int
    cloud_disconnected: int
    connection_rate: float
    operation_online: int
    operation_terminated: int
    operation_down: int
    operation_rate: float
    healthy_count: int
    health_score: float
    health_rating: str

class MachineProblem(BaseModel):
    machine_id: str
    cloud_status: str
    operation_status: str
    address: str

class StockMachine(BaseModel):
    machine_name: str
    stock_percentage: float
    level: str  # critical / warning / moderate / good

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_auth_header(token: str) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "authorization": f"Bearer {token}",
        "user-agent": "VendoliteApp/1.0"
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
        "currentPage": 0
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

def clean_stock_value(val) -> float:
    if pd.isna(val):
        return 0.0
    if isinstance(val, str):
        val = val.replace("%", "").strip()
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0

def stock_level_label(pct: float) -> str:
    if pct < 25:
        return "critical"
    elif pct < 50:
        return "warning"
    elif pct < 75:
        return "moderate"
    return "good"

def health_rating(score: float) -> str:
    if score >= 95:
        return "OUTSTANDING"
    elif score >= 90:
        return "EXCELLENT"
    elif score >= 80:
        return "GOOD"
    elif score >= 70:
        return "FAIR"
    return "POOR"

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "Vendolite API"}


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest):
    """Authenticate with Vendolite and return a bearer token."""
    url = f"{VENDOLITE_BASE_URL}/company/login"
    try:
        resp = requests.post(
            url,
            json={"username": body.username, "password": body.password},
            headers={"content-type": "application/json"},
            timeout=30
        )
        resp.raise_for_status()
        result = resp.json()
        if "token" not in result:
            raise HTTPException(status_code=401, detail="Invalid credentials")
        return TokenResponse(token=result["token"], message="Login successful")
    except requests.HTTPError as e:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except requests.RequestException as e:
        logger.error(f"Login request failed: {e}")
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")


@app.get("/machines/status", response_model=StatusSummary)
def get_machine_status(token: str):
    """Return cloud & operation status summary for all machines."""
    try:
        machines = fetch_machines(token)
    except requests.HTTPError as e:
        raise HTTPException(status_code=401, detail="Unauthorized — invalid token")
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    if not machines:
        raise HTTPException(status_code=404, detail="No machine data found")

    df = pd.json_normalize(machines)
    total = len(df)

    cloud_conn = int((df["cloudStatus"] == "Connected").sum())
    cloud_disc = int((df["cloudStatus"] == "Disconnected").sum())
    op_online  = int((df["operationStatus"] == "Online").sum())
    op_term    = int((df["operationStatus"] == "Terminated").sum())
    op_down    = int((df["operationStatus"] == "Down").sum())
    healthy    = int(((df["cloudStatus"] == "Connected") & (df["operationStatus"] == "Online")).sum())
    score      = round(healthy / total * 100, 1) if total > 0 else 0.0

    return StatusSummary(
        total_machines=total,
        cloud_connected=cloud_conn,
        cloud_disconnected=cloud_disc,
        connection_rate=round(cloud_conn / total * 100, 1) if total > 0 else 0.0,
        operation_online=op_online,
        operation_terminated=op_term,
        operation_down=op_down,
        operation_rate=round(op_online / total * 100, 1) if total > 0 else 0.0,
        healthy_count=healthy,
        health_score=score,
        health_rating=health_rating(score)
    )


@app.get("/machines/problems", response_model=list[MachineProblem])
def get_problem_machines(token: str):
    """Return machines that are Disconnected or Terminated/Down."""
    try:
        machines = fetch_machines(token)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized — invalid token")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    df = pd.json_normalize(machines)
    problems = df[
        (df["cloudStatus"] == "Disconnected") |
        (df["operationStatus"] == "Terminated") |
        (df["operationStatus"] == "Down")
    ]

    return [
        MachineProblem(
            machine_id=str(row.get("machineDisplayId", "N/A")),
            cloud_status=str(row.get("cloudStatus", "N/A")),
            operation_status=str(row.get("operationStatus", "N/A")),
            address=str(row.get("addressLine1") or row.get("address") or "N/A")
        )
        for _, row in problems.iterrows()
    ]


@app.get("/machines/stock", response_model=list[StockMachine])
def get_stock_data(token: str):
    """Return stock percentage for all machines."""
    try:
        machines = fetch_machines(token)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized — invalid token")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    df = pd.json_normalize(machines)

    # Find stock column
    stock_col = next(
        (c for c in df.columns if "stock" in c.lower() and "percent" in c.lower()), None
    )
    if not stock_col:
        raise HTTPException(status_code=404, detail="Stock percentage column not found")

    df[stock_col] = df[stock_col].apply(clean_stock_value)

    # Build name
    addr_col = next((c for c in ["addressLine1", "address", "branchName"] if c in df.columns), None)
    id_col   = next((c for c in ["machineDisplayId", "displayId", "id"] if c in df.columns), None)

    results = []
    for _, row in df.iterrows():
        pct = row[stock_col]
        if pct <= 0:
            continue
        name_parts = []
        if addr_col and pd.notna(row[addr_col]):
            name_parts.append(str(row[addr_col])[:30])
        if id_col and pd.notna(row[id_col]):
            name_parts.append(f"({row[id_col]})")
        name = " ".join(name_parts) or "Unknown Machine"
        results.append(StockMachine(
            machine_name=name,
            stock_percentage=round(pct, 1),
            level=stock_level_label(pct)
        ))

    results.sort(key=lambda x: x.stock_percentage)
    return results


@app.get("/machines/stock/chart")
def get_stock_chart(token: str):
    """Return a PNG bar chart of stock levels as base64."""
    stock_data = get_stock_data(token)

    if not stock_data:
        raise HTTPException(status_code=404, detail="No stock data available")

    color_map = {"critical": "red", "warning": "orange", "moderate": "yellow", "good": "green"}
    names = [m.machine_name for m in stock_data]
    values = [m.stock_percentage for m in stock_data]
    colors = [color_map[m.level] for m in stock_data]

    chart_height = max(8, len(stock_data) * 0.4)
    fig, ax = plt.subplots(figsize=(16, chart_height))

    bars = ax.barh(range(len(stock_data)), values, color=colors, alpha=0.8,
                   edgecolor="black", linewidth=0.5)
    ax.set_yticks(range(len(stock_data)))
    ax.set_yticklabels(names, fontsize=9)
    ax.set_xlabel("Stock Percentage (%)", fontweight="bold")
    ax.set_title("Machine Stock Levels", fontsize=14, fontweight="bold")
    ax.grid(True, axis="x", alpha=0.3, linestyle="--")

    for bar, pct in zip(bars, values):
        ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                f"{pct:.1f}%", va="center", fontweight="bold", fontsize=8)

    legend_elements = [
        Patch(facecolor="green",  alpha=0.8, label="Good (>75%)"),
        Patch(facecolor="yellow", alpha=0.8, label="Moderate (50–75%)"),
        Patch(facecolor="orange", alpha=0.8, label="Warning (25–50%)"),
        Patch(facecolor="red",    alpha=0.8, label="Critical (<25%)"),
    ]
    ax.legend(handles=legend_elements, loc="lower right", framealpha=0.9)
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)

    return StreamingResponse(buf, media_type="image/png")


@app.get("/machines/report/export")
def export_report(token: str):
    """Export full machine report as CSV."""
    try:
        machines = fetch_machines(token)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    df = pd.json_normalize(machines)

    # Keep useful columns only
    keep_cols = [c for c in [
        "machineDisplayId", "cloudStatus", "operationStatus",
        "addressLine1", "address", "city"
    ] if c in df.columns]

    # Add stock if available
    stock_col = next(
        (c for c in df.columns if "stock" in c.lower() and "percent" in c.lower()), None
    )
    if stock_col:
        keep_cols.append(stock_col)

    export_df = df[keep_cols].copy()

    buf = io.StringIO()
    export_df.to_csv(buf, index=False)
    buf.seek(0)

    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=vendolite_report.csv"}
    )


# ── Sales ─────────────────────────────────────────────────────────────────────

class SalesRequest(BaseModel):
    start_date: int   # Unix ms
    end_date: int     # Unix ms
    machine_id: Optional[str] = None
    page: int = 0
    limit: int = 100

class ProductSale(BaseModel):
    product_name: str
    product_id: str
    qty: int
    amount: float
    slot: str

class TransactionItem(BaseModel):
    trx_id: int
    machine_id: str
    machine_display_id: str
    amount: float
    status: str
    transaction_time: int
    payment_method: str
    products: list[ProductSale]

class MachineSalesSummary(BaseModel):
    machine_display_id: str
    machine_name: str
    total_revenue: float
    total_transactions: int
    successful_transactions: int
    failed_transactions: int
    total_products_sold: int

class SalesSummary(BaseModel):
    total_revenue: float
    total_transactions: int
    successful_transactions: int
    failed_transactions: int
    total_refunds: float
    total_products_sold: int
    by_machine: list[MachineSalesSummary]
    transactions: list[TransactionItem]

def fetch_machine_names(token: str) -> dict:
    """Returns a dict of machineId -> addressLine1 for name lookup."""
    try:
        machines = fetch_machines(token)
        result = {}
        for m in machines:
            mid = str(m.get("id", ""))
            addr = m.get("addressLine1") or m.get("address") or m.get("branchName") or ""
            display = m.get("machineDisplayId", "")
            result[mid] = {"name": addr, "display": display}
        return result
    except Exception:
        return {}

def fetch_transactions(token: str, start_date: int, end_date: int, machine_id: Optional[str] = None, page: int = 0, limit: int = 100) -> list:
    url = f"{VENDOLITE_BASE_URL}/transactions/getListV3"
    search_options = []
    if machine_id:
        search_options = [{"name": "Machine Id", "autoComplete": True, "searchTexts": [machine_id]}]

    payload = {
        "currentPage": page,
        "limit": limit,
        "startdate": start_date,
        "enddate": end_date,
        "sortSelected": "transactionTime",
        "sortDirection": "DESC",
        "searchOptions": search_options
    }
    resp = requests.post(url, json=payload, headers=get_auth_header(token), timeout=30)
    resp.raise_for_status()
    result = resp.json()
    return result.get("data", [])

def fetch_transaction_cart(token: str, trx_id: int) -> list:
    url = f"{VENDOLITE_BASE_URL}/transactions/getTransactionCart"
    resp = requests.post(url, json={"id": trx_id}, headers=get_auth_header(token), timeout=15)
    resp.raise_for_status()
    result = resp.json()
    return result.get("data", [])

def parse_payment_method(trx: dict) -> str:
    paid_info = trx.get("paidInfo", [])
    if paid_info:
        return paid_info[0].get("payment_type.name", "Unknown")
    return "Unknown"

@app.post("/sales/summary", response_model=SalesSummary)
def get_sales_summary(body: SalesRequest, token: str):
    """Get sales summary for a date range, optionally filtered by machine."""
    try:
        transactions = fetch_transactions(
            token, body.start_date, body.end_date,
            body.machine_id, body.page, body.limit
        )
    except requests.HTTPError as e:
        raise HTTPException(status_code=401, detail="Unauthorized — invalid or expired token")
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    # Fetch machine names for lookup
    machine_name_map = fetch_machine_names(token)
    total_revenue = 0.0
    total_refunds = 0.0
    success_count = 0
    fail_count = 0
    total_products = 0
    machine_map = {}
    parsed_transactions = []

    for trx in transactions:
        status = trx.get("status", "")
        amount = (trx.get("amountT") or 0) / 100
        refund = (trx.get("refundAmount") or 0) / 100
        machine_display = trx.get("machine.machineDisplayId", "Unknown")
        machine_id_int = trx.get("machineId", 0)

        if status == "SUCCESS":
            total_revenue += amount
            success_count += 1
        else:
            fail_count += 1
        total_refunds += refund

        # Fetch cart items for this transaction
        products = []
        try:
            cart_items = fetch_transaction_cart(token, trx["id"])
            for item in cart_items:
                if item.get("status") == "SUCCESS":
                    products.append(ProductSale(
                        product_name=item.get("productName", "Unknown"),
                        product_id=item.get("displayProductId", ""),
                        qty=item.get("qty", 1),
                        amount=(item.get("amount", 0)) / 100,
                        slot=item.get("slotName", "")
                    ))
                    total_products += item.get("qty", 1)
        except Exception:
            pass  # Skip cart fetch errors

        parsed_transactions.append(TransactionItem(
            trx_id=trx["id"],
            machine_id=str(machine_id_int),
            machine_display_id=machine_display,
            amount=amount,
            status=status,
            transaction_time=trx.get("transactionTime", 0),
            payment_method=parse_payment_method(trx),
            products=products
        ))

        # Per machine summary
        machine_name_str = machine_name_map.get(str(machine_id_int), {}).get("name", "")
        if machine_display not in machine_map:
            machine_map[machine_display] = {
                "revenue": 0.0, "total": 0, "success": 0, "fail": 0, "products": 0,
                "name": machine_name_str
            }
        m = machine_map[machine_display]
        m["total"] += 1
        if status == "SUCCESS":
            m["revenue"] += amount
            m["success"] += 1
        else:
            m["fail"] += 1
        m["products"] += len(products)

    by_machine = sorted([
        MachineSalesSummary(
            machine_display_id=k,
            machine_name=v.get("name", ""),
            total_revenue=v["revenue"],
            total_transactions=v["total"],
            successful_transactions=v["success"],
            failed_transactions=v["fail"],
            total_products_sold=v["products"]
        )
        for k, v in machine_map.items()
    ], key=lambda x: x.total_revenue, reverse=True)

    return SalesSummary(
        total_revenue=round(total_revenue, 2),
        total_transactions=len(transactions),
        successful_transactions=success_count,
        failed_transactions=fail_count,
        total_refunds=round(total_refunds, 2),
        total_products_sold=total_products,
        by_machine=by_machine,
        transactions=parsed_transactions
    )


@app.get("/sales/top-products")
def get_top_products(token: str, start_date: int, end_date: int, limit: int = 20):
    """Get top selling products across all machines for a date range."""
    try:
        transactions = fetch_transactions(token, start_date, end_date, limit=100)
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    product_map = {}
    for trx in transactions:
        if trx.get("status") != "SUCCESS":
            continue
        try:
            cart_items = fetch_transaction_cart(token, trx["id"])
            for item in cart_items:
                if item.get("status") == "SUCCESS":
                    name = item.get("productName", "Unknown")
                    pid = item.get("displayProductId", "")
                    qty = item.get("qty", 1)
                    amt = (item.get("amount", 0)) / 100
                    if name not in product_map:
                        product_map[name] = {"product_id": pid, "qty": 0, "revenue": 0.0}
                    product_map[name]["qty"] += qty
                    product_map[name]["revenue"] += amt
        except Exception:
            pass

    top = sorted(
        [{"product_name": k, **v} for k, v in product_map.items()],
        key=lambda x: x["qty"],
        reverse=True
    )[:limit]

    return {"data": top}


# ── Refill / Slot Grid ────────────────────────────────────────────────────────

class SlotInfo(BaseModel):
    slot_id: int
    slot_name: str
    row_number: int
    column_number: int
    product_name: str
    product_id: str
    current_qty: int
    max_qty: int
    enabled: bool
    issue_found: bool
    refill_needed: int  # how many to add to reach full
    price: float
    status: str  # "good", "low", "empty", "disabled", "issue"

class MachineSlotData(BaseModel):
    machine_id: int
    machine_display_id: str
    slots: list[SlotInfo]
    total_slots: int
    empty_slots: int
    low_slots: int
    good_slots: int

def fetch_machine_slots(token: str, machine_id: int) -> list:
    url = f"{VENDOLITE_BASE_URL}/machineSlot/getAllSlots"
    resp = requests.post(url, json={"machineId": machine_id},
                         headers=get_auth_header(token), timeout=30)
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
    if qty / max_qty < 0.5:
        return "low"
    return "good"

@app.get("/machines/slots/{machine_id}", response_model=MachineSlotData)
def get_machine_slots(machine_id: int, token: str, display_id: str = ""):
    try:
        raw_slots = fetch_machine_slots(token, machine_id)
    except requests.HTTPError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")

    slots = []
    for s in raw_slots:
        # Skip spacer slots (slotWidth == 0 and enable == 0)
        if s.get("slotWidth", 0) == 0 and s.get("enable", 0) == 0:
            continue

        stock_list = s.get("stock", [])
        current_qty = sum(st.get("qty", 0) for st in stock_list)
        max_qty = s.get("stockLimit", 1) or 1
        enabled = bool(s.get("enable", 0))
        issue = bool(s.get("slotIssueFound", 0))
        product_name = s.get("client_level_product.name", "Unknown")
        product_display_id = s.get("client_level_product.displayProductId", "")
        price = (s.get("client_level_product.cost") or 0) / 100
        status = parse_slot_status(current_qty, max_qty, enabled, issue)
        refill_needed = max(0, max_qty - current_qty) if enabled else 0

        slots.append(SlotInfo(
            slot_id=s["id"],
            slot_name=s["slotName"],
            row_number=s["rowNumber"],
            column_number=s["coloumnNumber"],
            product_name=product_name,
            product_id=product_display_id,
            current_qty=current_qty,
            max_qty=max_qty,
            enabled=enabled,
            issue_found=issue,
            refill_needed=refill_needed,
            price=price,
            status=status
        ))

    empty = sum(1 for s in slots if s.status == "empty")
    low = sum(1 for s in slots if s.status == "low")
    good = sum(1 for s in slots if s.status == "good")

    return MachineSlotData(
        machine_id=machine_id,
        machine_display_id=display_id or str(machine_id),
        slots=slots,
        total_slots=len(slots),
        empty_slots=empty,
        low_slots=low,
        good_slots=good
    )


@app.get("/machines/list")
def get_machine_list(token: str):
    """Returns list of machines with their IDs for refill screen."""
    try:
        machines = fetch_machines(token)
        result = []
        for m in machines:
            result.append({
                "id": m.get("id"),
                "display_id": m.get("machineDisplayId", ""),
                "address": m.get("addressLine1") or m.get("address") or "",
                "operation_status": m.get("operationStatus", ""),
                "cloud_status": m.get("cloudStatus", ""),
            })
        return {"data": result}
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Vendolite API unreachable")
