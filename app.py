<<<<<<< HEAD
"""ShopFloor PO Checker — extract PDF purchase orders, translate foreign units, and compare to contracted prices."""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import streamlit as st
from dateutil import parser as date_parser

APP_DIR = Path(__file__).resolve().parent
MASTER_PATH = APP_DIR / "pricing_master.csv"
PRICE_TOLERANCE = 0.005  # $0.005 rounding allowance

REQUIRED_FIELDS = [
    "Part Number",
    "Material Grade",
    "Quantity",
    "Client Unit",
    "Unit Price",
    "Delivery Date",
]

HEADER_ALIASES = {
    "part number": "Part Number",
    "part no": "Part Number",
    "part no.": "Part Number",
    "part #": "Part Number",
    "pn": "Part Number",
    "item": "Part Number",
    "sku": "Part Number",
    "material grade": "Material Grade",
    "grade": "Material Grade",
    "material": "Material Grade",
    "matl grade": "Material Grade",
    "spec": "Material Grade",
    "quantity": "Quantity",
    "qty": "Quantity",
    "qty.": "Quantity",
    "unit": "Client Unit",
    "uom": "Client Unit",
    "unit price": "Unit Price",
    "price": "Unit Price",
    "unit cost": "Unit Price",
    "u/p": "Unit Price",
    "delivery date": "Delivery Date",
    "need by": "Delivery Date",
    "due date": "Delivery Date",
    "ship date": "Delivery Date",
    "eta": "Delivery Date",
}

PART_RE = re.compile(r"\b([A-Z]{1,4}-?\d{3,6}[A-Z]?)\b", re.I)


def load_pricing_master(path: Path) -> pd.DataFrame:
    master = pd.read_csv(path, dtype=str)
    master.columns = [c.strip().lower() for c in master.columns]
    required = {"part_number", "material_grade", "contracted_unit_price"}
    missing = required - set(master.columns)
    if missing:
        raise ValueError(f"pricing_master.csv is missing columns: {sorted(missing)}")
    master["part_number"] = master["part_number"].str.strip()
    master["material_grade"] = master["material_grade"].fillna("").str.strip()
    master["contracted_unit_price"] = pd.to_numeric(
        master["contracted_unit_price"], errors="coerce"
    )
    return master


def normalize_header(value: str) -> str | None:
    key = re.sub(r"\s+", " ", (value or "").strip().lower())
    key = key.replace("_", " ")
    return HEADER_ALIASES.get(key)


def parse_money(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[,$]", "", text)
    text = re.sub(r"(usd|eur|gbp|inr)", "", text, flags=re.I).strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_qty(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group())


def parse_date(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return date_parser.parse(text, fuzzy=True, dayfirst=False).date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return text


def clean_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def translate_units(qty: float | None, client_unit: str) -> tuple[float | None, str, str]:
    """Converts foreign imperial/customary units to Indian shop-floor metric units.
    Returns: (metric_qty, metric_uom, translation_note)
    """
    if qty is None or pd.isna(qty):
        return None, "EA", "No quantity specified"
    
    uom = (client_unit or "").strip().upper()
    
    # Unit translation mapping rules for shop floor
    if uom in {"LBS", "LB", "POUND", "POUNDS"}:
        # 1 lb = 0.453592 kg
        metric_qty = qty * 0.453592
        return round(metric_qty, 3), "KG", f"Converted {qty:g} LBS to {metric_qty:.2f} KG"
    elif uom in {"IN", "INCH", "INCHES"}:
        # 1 inch = 25.4 mm
        metric_qty = qty * 25.4
        return round(metric_qty, 2), "MM", f"Converted {qty:g} IN to {metric_qty:.1f} MM"
    elif uom in {"FT", "FOOT", "FEET"}:
        # 1 foot = 0.3048 meters
        metric_qty = qty * 0.3048
        return round(metric_qty, 3), "M", f"Converted {qty:g} FT to {metric_qty:.2f} M"
    elif uom in {"OZ", "OUNCE", "OUNCES"}:
        # 1 oz = 0.0283495 kg
        metric_qty = qty * 0.0283495
        return round(metric_qty, 3), "KG", f"Converted {qty:g} OZ to {metric_qty:.3f} KG"
    else:
        # Default standard units (EA, PCS, M, KG) pass through directly
        standard_uom = uom if uom else "EA"
        return qty, standard_uom, f"Standard metric/count unit ({standard_uom})"


def extract_tables(pdf: pdfplumber.PDF) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            rows = [[clean_cell(c) for c in row] for row in table if any(clean_cell(c) for c in row)]
            if rows:
                tables.append(rows)
    return tables


def table_to_line_items(rows: list[list[str]]) -> pd.DataFrame | None:
    if len(rows) < 2:
        return None
    header_map: dict[int, str] = {}
    header_idx = None
    for i, row in enumerate(rows[:5]):
        mapped = {idx: normalize_header(cell) for idx, cell in enumerate(row)}
        hits = [v for v in mapped.values() if v]
        if "Part Number" in hits and len(hits) >= 2:
            header_map = {idx: name for idx, name in mapped.items() if name}
            header_idx = i
            break
    if header_idx is None:
        return None

    records = []
    for row in rows[header_idx + 1 :]:
        record = {field: "" for field in REQUIRED_FIELDS}
        for idx, field in header_map.items():
            if idx < len(row):
                record[field] = row[idx]
        if not record["Part Number"]:
            continue
        records.append(record)
    if not records:
        return None
    return pd.DataFrame(records)


LINE_RE = re.compile(
    r"(?P<part>PN-\d+)\s+"
    r"(?P<code>MAT-[A-Z0-9-]+)\s+"
    r"(?P<desc>.*?)\s+"
    r"(?P<qty>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<unit>EA|M|PCS|KG|LBS|IN|FT|OZ)?\s*"
    r"(?:USD|EUR|GBP|INR)?\s*"
    r"(?P<price>[\d,]+(?:\.\d+)?)",
    re.I,
)


def parse_text_lines(text: str) -> pd.DataFrame:
    records = []
    for line in text.splitlines():
        match = LINE_RE.search(line)
        if match:
            code = match.group("code")
            grade = code.replace("MAT-", "") if code else ""
            unit = match.group("unit") or "EA"
            records.append(
                {
                    "Part Number": match.group("part").upper(),
                    "Material Grade": grade,
                    "Quantity": match.group("qty"),
                    "Client Unit": unit,
                    "Unit Price": match.group("price"),
                    "Delivery Date": "",
                }
            )
    return pd.DataFrame(records) if records else pd.DataFrame(columns=REQUIRED_FIELDS)


def extract_pdf_bytes(file_bytes: bytes) -> tuple[str, pd.DataFrame]:
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
        tables = extract_tables(pdf)

    frames: list[pd.DataFrame] = []
    for table in tables:
        parsed = table_to_line_items(table)
        if parsed is not None and not parsed.empty:
            frames.append(parsed)

    if frames:
        items = pd.concat(frames, ignore_index=True)
    else:
        items = parse_text_lines(text)

    if items.empty:
        return text, items

    items["Part Number"] = items["Part Number"].map(clean_cell).str.upper()
    items["Material Grade"] = items["Material Grade"].map(clean_cell)
    items["Quantity"] = items["Quantity"].map(parse_qty)
    items["Client Unit"] = items["Client Unit"].map(clean_cell).str.upper()
    items["Unit Price"] = items["Unit Price"].map(parse_money)
    items["Delivery Date"] = items["Delivery Date"].map(parse_date)
    items = items[items["Part Number"].astype(str).str.len() > 0].reset_index(drop=True)
    items.insert(0, "Line", items.index + 1)
    return text, items


def compare_to_master(items: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    lookup = master.drop_duplicates(subset=["part_number"]).set_index("part_number")
    rows = []
    for _, item in items.iterrows():
        pn = str(item["Part Number"]).strip()
        po_grade = str(item.get("Material Grade") or "").strip()
        po_price = item.get("Unit Price")
        raw_qty = item.get("Quantity")
        client_uom = str(item.get("Client Unit") or "EA")
        
        # Apply automatic unit translation for shop floor workers
        metric_qty, metric_uom, unit_note = translate_units(raw_qty, client_uom)

        found = pn in lookup.index
        master_row = lookup.loc[pn] if found else None
        contracted = None if master_row is None else master_row["contracted_unit_price"]
        master_grade = "" if master_row is None else str(master_row["material_grade"]).strip()
        master_code = ""
        if master_row is not None and "material_code" in master_row.index:
            master_code = str(master_row["material_code"]).strip()

        issues: list[str] = []
        if not found:
            issues.append("Part not in pricing master")
        else:
            if po_grade and master_grade and po_grade.upper() != master_grade.upper():
                issues.append(
                    f"Grade mismatch (PO {po_grade} vs master {master_grade})"
                )
            if pd.isna(po_price):
                issues.append("Missing unit price on PO")
            elif pd.isna(contracted):
                issues.append("No contracted price on file")
            elif abs(float(po_price) - float(contracted)) > PRICE_TOLERANCE:
                issues.append("Price mismatch vs contracted rate")

        match_ok = found and not issues
        variance = None
        if found and pd.notna(po_price) and pd.notna(contracted):
            variance = float(po_price) - float(contracted)

        note_full = "; ".join(issues) if issues else "Matches contracted catalog"
        if unit_note and "Converted" in unit_note:
            note_full = f"{unit_note} | {note_full}"

        rows.append(
            {
                "Line": item.get("Line"),
                "Part Number": pn,
                "PO Grade": po_grade if po_grade else "—",
                "Master Grade": master_grade if master_grade else "—",
                "Material Code": master_code if master_code else "—",
                "Client Qty/Unit": f"{raw_qty:g} {client_uom}" if pd.notna(raw_qty) else "—",
                "Shop-Floor Metric": f"{metric_qty:g} {metric_uom}" if pd.notna(metric_qty) else "—",
                "PO Unit Price": po_price,
                "Contracted Price": contracted,
                "Variance": variance,
                "Delivery Date": item.get("Delivery Date") or "—",
                "Status": "MATCH" if match_ok else "WARNING",
                "Notes": note_full,
            }
        )
    return pd.DataFrame(rows)


def style_comparison(df: pd.DataFrame):
    money_cols = ["PO Unit Price", "Contracted Price", "Variance"]

    def color_row(row: pd.Series) -> list[str]:
        if row["Status"] == "MATCH":
            fill = "background-color: #c6f6d5; color: #14532d; font-weight: 600;"
        else:
            fill = "background-color: #fed7d7; color: #9b2c2c; font-weight: 700;"
        return [fill] * len(row)

    styler = (
        df.style.apply(color_row, axis=1)
        .format(
            {
                **{c: (lambda v: "" if pd.isna(v) else f"{v:,.2f}") for c in money_cols},
            },
            na_rep="—",
        )
        .hide(axis="index")
    )
    return styler


def render_css() -> None:
    st.markdown(
        """
        <style>
            .block-container { padding-top: 1.4rem; max-width: 1200px; }
            .hero {
                background: linear-gradient(135deg, #1a365d 0%, #2b6cb0 100%);
                color: #fff;
                padding: 1.4rem 1.6rem;
                border-radius: 14px;
                margin-bottom: 1.2rem;
            }
            .hero h1 { font-size: 1.7rem; margin: 0 0 0.35rem 0; }
            .hero p { margin: 0; opacity: 0.92; }
            .metric-label { font-size: 0.8rem; color: #4a5568; }
            div[data-testid="stDataFrame"] table { font-size: 0.92rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="ShopFloor PO Checker", layout="wide")
    render_css()

    st.markdown(
        """
        <div class="hero">
            <h1>ShopFloor Purchase Order Checker & Unit Normalizer</h1>
            <p>Upload a foreign client PDF purchase order. Line items are extracted locally, 
            foreign units (LBS, IN, FT) are automatically translated to metric standards for shop-floor operators, 
            and prices are cross-checked against pricing_master.csv.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not MASTER_PATH.exists():
        st.error("pricing_master.csv was not found next to app.py.")
        st.stop()

    master = load_pricing_master(MASTER_PATH)

    with st.sidebar:
        st.subheader("Pricing master")
        st.caption(f"Loaded from `{MASTER_PATH.name}`")
        st.dataframe(master, use_container_width=True, hide_index=True, height=320)
        st.download_button(
            "Download pricing master CSV",
            data=MASTER_PATH.read_bytes(),
            file_name="pricing_master.csv",
            mime="text/csv",
        )
        sample_path = APP_DIR / "samples" / "sample_purchase_order.pdf"
        if sample_path.exists():
            st.download_button(
                "Download sample PO PDF",
                data=sample_path.read_bytes(),
                file_name="sample_purchase_order.pdf",
                mime="application/pdf",
            )
    uploaded = st.file_uploader(
        "Client purchase order (PDF)",
        type=["pdf"],
        help="Text-based PDFs work best. Scanned images without OCR will not extract line items.",
    )
    sample_path = APP_DIR / "samples" / "sample_purchase_order.pdf"
    if sample_path.exists() and st.button("Run sample purchase order"):
        st.session_state["sample_pdf_bytes"] = sample_path.read_bytes()
        st.session_state["sample_pdf_name"] = sample_path.name

    pdf_bytes = None
    source_name = None
    if uploaded is not None:
        pdf_bytes = uploaded.getvalue()
        source_name = uploaded.name
        st.session_state.pop("sample_pdf_bytes", None)
    elif st.session_state.get("sample_pdf_bytes"):
        pdf_bytes = st.session_state["sample_pdf_bytes"]
        source_name = st.session_state.get("sample_pdf_name", "sample_purchase_order.pdf")

    if pdf_bytes is None:
        st.info("Upload a PDF to extract Part Number, Grade, Quantity, Foreign Units, and Unit Price.")
        with st.expander("Expected PO layout"):
            st.markdown(
                "- A table with headers for **Part Number**, **Quantity**, **Unit/UOM**, and **Unit Price**.\n"
                "- Foreign units like **LBS**, **IN**, or **FT** are automatically converted to **KG**, **MM**, or **M**.\n"
                "- Matching lines are highlighted in green. Warnings or price variances are highlighted in red."
            )
        return

    st.caption(f"Checking **{source_name}**")
    with st.spinner("Extracting purchase-order text and normalizing units..."):
        raw_text, items = extract_pdf_bytes(pdf_bytes)

    col_a, col_b = st.columns([1.15, 0.85])
    with col_a:
        st.subheader("Extracted line items")
        if items.empty:
            st.warning("No structured line items were found. Review the raw text and use a table-style PO if possible.")
        else:
            display_items = items.copy()
            display_items["Quantity"] = display_items["Quantity"].map(
                lambda v: "" if pd.isna(v) else f"{v:g}"
            )
            display_items["Unit Price"] = display_items["Unit Price"].map(
                lambda v: "" if pd.isna(v) else f"{v:,.2f}"
            )
            st.dataframe(display_items, use_container_width=True, hide_index=True)
    with col_b:
        st.subheader("Raw PDF text")
        st.text_area("Extracted text", raw_text or "(no extractable text)", height=220, label_visibility="collapsed")

    if items.empty:
        return

    comparison = compare_to_master(items, master)
    matches = int((comparison["Status"] == "MATCH").sum())
    warnings = int((comparison["Status"] == "WARNING").sum())
    price_issues = int(comparison["Notes"].str.contains("Price mismatch", na=False).sum())
    unit_conversions = int(comparison["Notes"].str.contains("Converted", na=False).sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Lines extracted", len(comparison))
    m2.metric("Matching lines", matches)
    m3.metric("Price mismatches", price_issues)
    m4.metric("Unit conversions", unit_conversions)

    st.subheader("Contracted price and unit translation comparison")
    st.caption(
        "Green = valid line items matching catalog rates. "
        "Red = price variances, unknown parts, or errors requiring review."
    )
    st.dataframe(style_comparison(comparison), use_container_width=True, hide_index=True)

    if warnings:
        st.error(f"{warnings} line(s) need review before the order is accepted.")
    else:
        st.success("All extracted lines match the contracted catalog and units are normalized.")

    csv_bytes = comparison.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download comparison CSV",
        data=csv_bytes,
        file_name=f"po_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
=======
"""ShopFloor PO Checker — extract PDF purchase orders and compare to contracted prices."""

from __future__ import annotations

import io
import re
from datetime import datetime
from pathlib import Path

import pandas as pd
import pdfplumber
import streamlit as st
from dateutil import parser as date_parser

APP_DIR = Path(__file__).resolve().parent
MASTER_PATH = APP_DIR / "pricing_master.csv"
PRICE_TOLERANCE = 0.005  # $0.005 rounding allowance

REQUIRED_FIELDS = [
    "Part Number",
    "Material Grade",
    "Quantity",
    "Unit Price",
    "Delivery Date",
]

HEADER_ALIASES = {
    "part number": "Part Number",
    "part no": "Part Number",
    "part no.": "Part Number",
    "part #": "Part Number",
    "pn": "Part Number",
    "item": "Part Number",
    "sku": "Part Number",
    "material grade": "Material Grade",
    "grade": "Material Grade",
    "material": "Material Grade",
    "matl grade": "Material Grade",
    "spec": "Material Grade",
    "quantity": "Quantity",
    "qty": "Quantity",
    "qty.": "Quantity",
    "unit price": "Unit Price",
    "price": "Unit Price",
    "unit cost": "Unit Price",
    "u/p": "Unit Price",
    "delivery date": "Delivery Date",
    "need by": "Delivery Date",
    "due date": "Delivery Date",
    "ship date": "Delivery Date",
    "eta": "Delivery Date",
}

PART_RE = re.compile(r"\b([A-Z]{1,4}-?\d{3,6}[A-Z]?)\b", re.I)


def load_pricing_master(path: Path) -> pd.DataFrame:
    master = pd.read_csv(path, dtype=str)
    master.columns = [c.strip().lower() for c in master.columns]
    required = {"part_number", "material_grade", "contracted_unit_price"}
    missing = required - set(master.columns)
    if missing:
        raise ValueError(f"pricing_master.csv is missing columns: {sorted(missing)}")
    master["part_number"] = master["part_number"].str.strip()
    master["material_grade"] = master["material_grade"].fillna("").str.strip()
    master["contracted_unit_price"] = pd.to_numeric(
        master["contracted_unit_price"], errors="coerce"
    )
    return master


def normalize_header(value: str) -> str | None:
    key = re.sub(r"\s+", " ", (value or "").strip().lower())
    key = key.replace("_", " ")
    return HEADER_ALIASES.get(key)


def parse_money(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"[,$]", "", text)
    text = re.sub(r"(usd|eur|gbp|inr)", "", text, flags=re.I).strip()
    try:
        return float(text)
    except ValueError:
        return None


def parse_qty(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group())


def parse_date(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return date_parser.parse(text, fuzzy=True, dayfirst=False).date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return text


def clean_cell(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def extract_tables(pdf: pdfplumber.PDF) -> list[list[list[str]]]:
    tables: list[list[list[str]]] = []
    for page in pdf.pages:
        for table in page.extract_tables() or []:
            rows = [[clean_cell(c) for c in row] for row in table if any(clean_cell(c) for c in row)]
            if rows:
                tables.append(rows)
    return tables


def table_to_line_items(rows: list[list[str]]) -> pd.DataFrame | None:
    if len(rows) < 2:
        return None
    header_map: dict[int, str] = {}
    header_idx = None
    for i, row in enumerate(rows[:5]):
        mapped = {idx: normalize_header(cell) for idx, cell in enumerate(row)}
        hits = [v for v in mapped.values() if v]
        if "Part Number" in hits and len(hits) >= 2:
            header_map = {idx: name for idx, name in mapped.items() if name}
            header_idx = i
            break
    if header_idx is None:
        return None

    records = []
    for row in rows[header_idx + 1 :]:
        record = {field: "" for field in REQUIRED_FIELDS}
        for idx, field in header_map.items():
            if idx < len(row):
                record[field] = row[idx]
        if not record["Part Number"]:
            continue
        records.append(record)
    if not records:
        return None
    return pd.DataFrame(records)


LINE_RE = re.compile(
    r"(?P<part>[A-Z]{1,4}-?\d{3,6}[A-Z]?)"
    r"(?:[\s|,;]+(?P<grade>[A-Z0-9][A-Z0-9.\-]{1,16}))?"
    r"[\s|,;]+(?P<qty>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s*(?:EA|M|PCS|KG))?"
    r"[\s|,;]+(?:[$])?(?P<price>\d[\d,]*(?:\.\d+)?)",
    re.I,
)


def parse_text_lines(text: str) -> pd.DataFrame:
    records = []
    for match in LINE_RE.finditer(text):
        grade = (match.group("grade") or "").strip()
        if grade.lower() in {"ea", "each", "pcs", "pc", "m", "kg", "qty"}:
            grade = ""
        records.append(
            {
                "Part Number": match.group("part").upper(),
                "Material Grade": grade,
                "Quantity": match.group("qty"),
                "Unit Price": match.group("price"),
                "Delivery Date": "",
            }
        )
    return pd.DataFrame(records) if records else pd.DataFrame(columns=REQUIRED_FIELDS)


def extract_pdf_bytes(file_bytes: bytes) -> tuple[str, pd.DataFrame]:
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        text = "\n".join((page.extract_text() or "") for page in pdf.pages)
        tables = extract_tables(pdf)

    frames: list[pd.DataFrame] = []
    for table in tables:
        parsed = table_to_line_items(table)
        if parsed is not None and not parsed.empty:
            frames.append(parsed)

    if frames:
        items = pd.concat(frames, ignore_index=True)
    else:
        items = parse_text_lines(text)

    if items.empty:
        return text, items

    items["Part Number"] = items["Part Number"].map(clean_cell).str.upper()
    items["Material Grade"] = items["Material Grade"].map(clean_cell)
    items["Quantity"] = items["Quantity"].map(parse_qty)
    items["Unit Price"] = items["Unit Price"].map(parse_money)
    items["Delivery Date"] = items["Delivery Date"].map(parse_date)
    items = items[items["Part Number"].astype(str).str.len() > 0].reset_index(drop=True)
    items.insert(0, "Line", items.index + 1)
    return text, items


def compare_to_master(items: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    lookup = master.drop_duplicates(subset=["part_number"]).set_index("part_number")
    rows = []
    for _, item in items.iterrows():
        pn = str(item["Part Number"]).strip()
        po_grade = str(item.get("Material Grade") or "").strip()
        po_price = item.get("Unit Price")
        found = pn in lookup.index
        master_row = lookup.loc[pn] if found else None
        contracted = None if master_row is None else master_row["contracted_unit_price"]
        master_grade = "" if master_row is None else str(master_row["material_grade"]).strip()
        master_code = ""
        if master_row is not None and "material_code" in master_row.index:
            master_code = str(master_row["material_code"]).strip()

        issues: list[str] = []
        if not found:
            issues.append("Part not in pricing master")
        else:
            if po_grade and master_grade and po_grade.upper() != master_grade.upper():
                issues.append(
                    f"Grade mismatch (PO {po_grade} vs master {master_grade})"
                )
            if pd.isna(po_price):
                issues.append("Missing unit price on PO")
            elif pd.isna(contracted):
                issues.append("No contracted price on file")
            elif abs(float(po_price) - float(contracted)) > PRICE_TOLERANCE:
                issues.append("Price mismatch vs contracted rate")

        match_ok = found and not issues
        variance = None
        if found and pd.notna(po_price) and pd.notna(contracted):
            variance = float(po_price) - float(contracted)

        rows.append(
            {
                "Line": item.get("Line"),
                "Part Number": pn,
                "PO Grade": po_grade if po_grade else "—",
                "Master Grade": master_grade if master_grade else "—",
                "Material Code": master_code if master_code else "—",
                "Qty": item.get("Quantity"),
                "PO Unit Price": po_price,
                "Contracted Price": contracted,
                "Variance": variance,
                "Delivery Date": item.get("Delivery Date") or "—",
                "Status": "MATCH" if match_ok else "WARNING",
                "Notes": "; ".join(issues) if issues else "Matches contracted catalog",
            }
        )
    return pd.DataFrame(rows)


def style_comparison(df: pd.DataFrame):
    money_cols = ["PO Unit Price", "Contracted Price", "Variance"]

    def color_row(row: pd.Series) -> list[str]:
        if row["Status"] == "MATCH":
            fill = "background-color: #c6f6d5; color: #14532d; font-weight: 600;"
        else:
            fill = "background-color: #fed7d7; color: #9b2c2c; font-weight: 700;"
        return [fill] * len(row)

    styler = (
        df.style.apply(color_row, axis=1)
        .format(
            {
                "Qty": lambda v: "" if pd.isna(v) else f"{v:g}",
                **{c: (lambda v: "" if pd.isna(v) else f"{v:,.2f}") for c in money_cols},
            },
            na_rep="—",
        )
        .hide(axis="index")
    )
    return styler


def render_css() -> None:
    st.markdown(
        """
        <style>
            .block-container { padding-top: 1.4rem; max-width: 1200px; }
            .hero {
                background: linear-gradient(135deg, #1a365d 0%, #2b6cb0 100%);
                color: #fff;
                padding: 1.4rem 1.6rem;
                border-radius: 14px;
                margin-bottom: 1.2rem;
            }
            .hero h1 { font-size: 1.7rem; margin: 0 0 0.35rem 0; }
            .hero p { margin: 0; opacity: 0.92; }
            .metric-label { font-size: 0.8rem; color: #4a5568; }
            div[data-testid="stDataFrame"] table { font-size: 0.92rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="ShopFloor PO Checker", page_icon="🏭", layout="wide")
    render_css()

    st.markdown(
        """
        <div class="hero">
            <h1>ShopFloor Purchase Order Checker</h1>
            <p>Upload a client PDF purchase order. Line items are extracted locally,
            then cross-checked against contracted prices in pricing_master.csv.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not MASTER_PATH.exists():
        st.error("pricing_master.csv was not found next to app.py.")
        st.stop()

    master = load_pricing_master(MASTER_PATH)

    with st.sidebar:
        st.subheader("Pricing master")
        st.caption(f"Loaded from `{MASTER_PATH.name}`")
        st.dataframe(master, use_container_width=True, hide_index=True, height=320)
        st.download_button(
            "Download pricing master CSV",
            data=MASTER_PATH.read_bytes(),
            file_name="pricing_master.csv",
            mime="text/csv",
        )
        sample_path = APP_DIR / "samples" / "sample_purchase_order.pdf"
        if sample_path.exists():
            st.download_button(
                "Download sample PO PDF",
                data=sample_path.read_bytes(),
                file_name="sample_purchase_order.pdf",
                mime="application/pdf",
            )
    uploaded = st.file_uploader(
        "Client purchase order (PDF)",
        type=["pdf"],
        help="Text-based PDFs work best. Scanned images without OCR will not extract line items.",
    )
    sample_path = APP_DIR / "samples" / "sample_purchase_order.pdf"
    if sample_path.exists() and st.button("Run sample purchase order"):
        st.session_state["sample_pdf_bytes"] = sample_path.read_bytes()
        st.session_state["sample_pdf_name"] = sample_path.name

    pdf_bytes = None
    source_name = None
    if uploaded is not None:
        pdf_bytes = uploaded.getvalue()
        source_name = uploaded.name
        st.session_state.pop("sample_pdf_bytes", None)
    elif st.session_state.get("sample_pdf_bytes"):
        pdf_bytes = st.session_state["sample_pdf_bytes"]
        source_name = st.session_state.get("sample_pdf_name", "sample_purchase_order.pdf")

    if pdf_bytes is None:
        st.info("Upload a PDF to extract Part Number, Material Grade, Quantity, Unit Price, and Delivery Date.")
        with st.expander("Expected PO layout"):
            st.markdown(
                "- A table with headers similar to **Part Number**, **Material Grade**, **Quantity**, **Unit Price**, **Delivery Date**.\n"
                "- Or line text such as `PN-1001 SS304 240 12.50`.\n"
                "- Matching lines are shown in **green**. Price mismatches, unknown parts, or missing grades are shown in **bold red**."
            )
        return

    st.caption(f"Checking **{source_name}**")
    with st.spinner("Extracting purchase-order text…"):
        raw_text, items = extract_pdf_bytes(pdf_bytes)

    col_a, col_b = st.columns([1.15, 0.85])
    with col_a:
        st.subheader("Extracted line items")
        if items.empty:
            st.warning("No structured line items were found. Review the raw text and use a table-style PO if possible.")
        else:
            display_items = items.copy()
            display_items["Quantity"] = display_items["Quantity"].map(
                lambda v: "" if pd.isna(v) else f"{v:g}"
            )
            display_items["Unit Price"] = display_items["Unit Price"].map(
                lambda v: "" if pd.isna(v) else f"{v:,.2f}"
            )
            st.dataframe(display_items, use_container_width=True, hide_index=True)
    with col_b:
        st.subheader("Raw PDF text")
        st.text_area("Extracted text", raw_text or "(no extractable text)", height=220, label_visibility="collapsed")

    if items.empty:
        return

    comparison = compare_to_master(items, master)
    matches = int((comparison["Status"] == "MATCH").sum())
    warnings = int((comparison["Status"] == "WARNING").sum())
    missing_grade = int(comparison["Notes"].str.contains("Missing material grade", na=False).sum())
    price_issues = int(comparison["Notes"].str.contains("Price mismatch", na=False).sum())

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Lines extracted", len(comparison))
    m2.metric("Matching lines", matches)
    m3.metric("Price mismatches", price_issues)
    m4.metric("Missing grades", missing_grade)

    st.subheader("Contracted price comparison")
    st.caption(
        "Green = part, grade, and unit price match the pricing master. "
        "Bold red = price mismatch, missing material grade, or unknown material/part."
    )
    st.dataframe(style_comparison(comparison), use_container_width=True, hide_index=True)

    if warnings:
        st.error(f"{warnings} line(s) need review before the order is accepted.")
    else:
        st.success("All extracted lines match the contracted catalog.")

    csv_bytes = comparison.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download comparison CSV",
        data=csv_bytes,
        file_name=f"po_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
>>>>>>> aeaaf5d06474d01a05a3e68c599a27c293b6a05f
    main()