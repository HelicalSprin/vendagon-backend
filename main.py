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
