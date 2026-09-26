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
PRICE_TOLERANCE = 0.005

REQUIRED_FIELDS = [
    "Part Number",
    "Material Grade",
    "Quantity",
    "Client Unit",
    "Unit Price",
    "Delivery Date",
]

PART_RE = re.compile(r"\b([A-Z]{1,4}-?\d{3,6}[A-Z]?)\b", re.I)


def load_pricing_master(path: Path) -> pd.DataFrame:
    if path.exists():
        try:
            master = pd.read_csv(path, dtype=str)
            master.columns = [c.strip().lower().replace(" ", "_") for c in master.columns]
            rename_map = {}
            for col in master.columns:
                if "part" in col:
                    rename_map[col] = "part_number"
                elif "grade" in col or "material" in col:
                    rename_map[col] = "material_grade"
                elif "price" in col or "cost" in col:
                    rename_map[col] = "contracted_unit_price"
            master = master.rename(columns=rename_map)
            if "part_number" in master.columns and "contracted_unit_price" in master.columns:
                master["part_number"] = master["part_number"].str.strip()
                master["material_grade"] = master.get("material_grade", pd.Series([""] * len(master))).fillna("").str.strip()
                master["contracted_unit_price"] = pd.to_numeric(master["contracted_unit_price"], errors="coerce")
                return master
        except Exception:
            pass

    fallback_data = [
        {"part_number": "PN-1001", "material_grade": "SS304", "material_code": "MAT-SS304", "contracted_unit_price": 12.50},
        {"part_number": "PN-1002", "material_grade": "SS316", "material_code": "MAT-SS316", "contracted_unit_price": 4.75},
        {"part_number": "PN-1003", "material_grade": "A36", "material_code": "MAT-A36", "contracted_unit_price": 8.20},
        {"part_number": "PN-1004", "material_grade": "AI 6061", "material_code": "MAT-AI 6061", "contracted_unit_price": 18.90},
        {"part_number": "PN-1005", "material_grade": "C36000", "material_code": "MAT-BRZ", "contracted_unit_price": 22.40},
        {"part_number": "PN-1006", "material_grade": "TI-6AL-4V", "material_code": "MAT-TI6AL4V", "contracted_unit_price": 96.00},
        {"part_number": "PN-1007", "material_grade": "POM-C", "material_code": "MAT-POM", "contracted_unit_price": 3.15},
        {"part_number": "PN-1008", "material_grade": "NBR70", "material_code": "MAT-NBR", "contracted_unit_price": 0.85},
    ]
    return pd.DataFrame(fallback_data)


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
    if qty is None or pd.isna(qty):
        return None, "EA", "No quantity specified"
    uom = (client_unit or "").strip().upper()
    if uom in {"LBS", "LB", "POUND", "POUNDS"}:
        metric_qty = qty * 0.453592
        return round(metric_qty, 3), "KG", f"Converted {qty:g} LBS to {metric_qty:.2f} KG"
    elif uom in {"IN", "INCH", "INCHES"}:
        metric_qty = qty * 25.4
        return round(metric_qty, 2), "MM", f"Converted {qty:g} IN to {metric_qty:.1f} MM"
    elif uom in {"FT", "FOOT", "FEET"}:
        metric_qty = qty * 0.3048
        return round(metric_qty, 3), "M", f"Converted {qty:g} FT to {metric_qty:.2f} M"
    elif uom in {"OZ", "OUNCE", "OUNCES"}:
        metric_qty = qty * 0.0283495
        return round(metric_qty, 3), "KG", f"Converted {qty:g} OZ to {metric_qty:.3f} KG"
    else:
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
    if not rows:
        return None
    
    records = []
    for row in rows:
        cells = [clean_cell(c) for c in row if clean_cell(c) is not None]
        if not cells:
            continue
            
        row_str = " ".join(cells)
        pn_match = PART_RE.search(row_str)
        if not pn_match:
            continue
            
        part_num = pn_match.group(1).upper()
        
        grade = ""
        unit = "EA"
        for cell in cells:
            if "MAT-" in cell.upper() or cell.upper() in {"SS304", "SS316", "A36", "AI 6061", "C36000", "TI-6AL-4V", "POM-C", "NBR70"}:
                grade = cell.upper().replace("MAT-", "").strip()
            if cell.upper() in {"EA", "M", "PCS", "KG", "LBS", "IN", "FT", "OZ"}:
                unit = cell.upper()

        numeric_cells = []
        for idx, cell in enumerate(cells):
            if part_num in cell.upper() or "STAINLESS" in cell.upper() or "MILD" in cell.upper() or "BRASS" in cell.upper() or "BOLT" in cell.upper() or "BAR" in cell.upper():
                continue
            val = parse_money(cell)
            if val is not None and val < 100000:
                numeric_cells.append((idx, val))
                
        qty = None
        price = None
        if len(numeric_cells) >= 2:
            numeric_cells.sort(key=lambda x: x[0])
            qty = numeric_cells[0][1]
            price = numeric_cells[1][1] if len(numeric_cells) > 1 else None
        elif len(numeric_cells) == 1:
            qty = numeric_cells[0][1]

        if qty is not None:
            records.append({
                "Part Number": part_num,
                "Material Grade": grade,
                "Quantity": qty,
                "Client Unit": unit,
                "Unit Price": price,
                "Delivery Date": ""
            })

    if not records:
        return None
    return pd.DataFrame(records)


def parse_text_lines(text: str) -> pd.DataFrame:
    records = []
    for line in text.splitlines():
        pn_match = PART_RE.search(line)
        if pn_match:
            part_num = pn_match.group(1).upper()
            unit_match = re.search(r"\b(EA|M|PCS|KG|LBS|IN|FT|OZ)\b", line, re.I)
            unit = unit_match.group(1).upper() if unit_match else "EA"
            
            clean_line = line.replace(part_num, "")
            numbers = [parse_money(m.group()) for m in re.finditer(r"\b\d+(?:\.\d+)?\b", clean_line)]
            valid_nums = [n for n in numbers if n is not None and n < 100000]
            
            qty = valid_nums[0] if len(valid_nums) > 0 else 1.0
            price = valid_nums[1] if len(valid_nums) > 1 else 0.0
            
            records.append({
                "Part Number": part_num,
                "Material Grade": "",
                "Quantity": qty,
                "Client Unit": unit,
                "Unit Price": price,
                "Delivery Date": "",
            })
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
        items = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["Part Number"])
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
        
        metric_qty, metric_uom, unit_note = translate_units(raw_qty, client_uom)

        found = pn in lookup.index
        master_row = lookup.loc[pn] if found else None
        contracted = None if master_row is None else float(master_row["contracted_unit_price"])
        master_grade = "" if master_row is None else str(master_row["material_grade"]).strip()
        master_code = ""
        if master_row is not None and "material_code" in master_row.index:
            master_code = str(master_row["material_code"]).strip()

        issues: list[str] = []
        if not found:
            issues.append("Part not in pricing master")
        else:
            if po_grade and master_grade and po_grade.upper() != master_grade.upper():
                issues.append(f"Grade mismatch (PO {po_grade} vs master {master_grade})")
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
        .format({c: (lambda v: "" if pd.isna(v) else f"{v:,.2f}") for c in money_cols}, na_rep="—")
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
            foreign units are automatically translated to metric standards for shop-floor operators, 
            and prices are cross-checked against the pricing master.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    master = load_pricing_master(MASTER_PATH)

    with st.sidebar:
        st.subheader("Pricing master")
        st.dataframe(master, use_container_width=True, hide_index=True, height=320)
        sample_path = APP_DIR / "samples" / "sample_purchase_order.pdf"
        if sample_path.exists():
            st.download_button(
                "Download sample PO PDF",
                data=sample_path.read_bytes(),
                file_name="sample_purchase_order.pdf",
                mime="application/pdf",
            )

    uploaded = st.file_uploader("Client purchase order (PDF)", type=["pdf"])
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
        return

    st.caption(f"Checking **{source_name}**")
    with st.spinner("Extracting purchase-order text and normalizing units..."):
        raw_text, items = extract_pdf_bytes(pdf_bytes)

    col_a, col_b = st.columns([1.15, 0.85])
    with col_a:
        st.subheader("Extracted line items")
        if items.empty:
            st.warning("No structured line items were found.")
        else:
            display_items = items.copy()
            display_items["Quantity"] = display_items["Quantity"].map(lambda v: "" if pd.isna(v) else f"{v:g}")
            display_items["Unit Price"] = display_items["Unit Price"].map(lambda v: "" if pd.isna(v) else f"{v:,.2f}")
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
    main()