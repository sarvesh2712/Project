"""Write a sample client purchase-order PDF with no third-party PDF libraries."""

from __future__ import annotations

from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent / "samples"
OUTPUT_PATH = OUTPUT_DIR / "sample_purchase_order.pdf"

LINE_ITEMS = [
    ("PN-1001", "SS304", "240", "12.50", "15-Oct-2026"),
    ("PN-1002", "SS316", "500", "5.10", "18-Oct-2026"),
    ("PN-1003", "", "120", "8.20", "22-Oct-2026"),
    ("PN-1004", "AL6061", "80", "18.90", "01-Nov-2026"),
    ("PN-1006", "TI-6AL-4V", "40", "110.00", "05-Nov-2026"),
    ("PN-9999", "INCONEL718", "12", "250.00", "12-Nov-2026"),
    ("PN-1010", "SA105", "16", "47.80", "20-Nov-2026"),
]


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _tj(x: float, y: float, text: str, font: str = "F1", size: int = 10) -> str:
    return f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({_esc(text)}) Tj ET"


def build_sample_po(path: Path = OUTPUT_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)

    page_w, page_h = 612, 792
    left, right = 40, 572
    header_y = 720
    col_x = [48, 140, 250, 330, 430]
    headers = ["Part Number", "Material Grade", "Quantity", "Unit Price", "Delivery Date"]
    row_h = 22
    table_top = 600
    table_bottom = table_top - row_h * (len(LINE_ITEMS) + 1)

    ops: list[str] = []
    ops.append("0.10 0.21 0.36 rg")
    ops.append(f"{left:.1f} {header_y:.1f} {right - left:.1f} 36 re f")
    ops.append("1 1 1 rg")
    ops.append(_tj(50, header_y + 14, "NORTHLINE FABRICATION LTD.", "F2", 13))
    ops.append(_tj(400, header_y + 14, "PURCHASE ORDER", "F2", 12))
    ops.append("0 0 0 rg")
    ops.append(_tj(50, 690, "412 Industrial Parkway, Cleveland, OH 44114", "F1", 9))
    ops.append(_tj(50, 676, "Vendor: ShopFloor Components Inc.", "F1", 9))
    ops.append(_tj(360, 690, "PO Number: PO-2026-0918", "F1", 9))
    ops.append(_tj(360, 676, "Date: 25-Sep-2026   Buyer: A. Chen", "F1", 9))
    ops.append(_tj(50, 640, "Client Purchase Order - Production Materials", "F2", 14))
    ops.append(
        _tj(
            50,
            622,
            "Unit prices USD. Delivery dates are on-dock. Material certs required for metals.",
            "F1",
            9,
        )
    )

    # Table fill + grid
    ops.append("0.10 0.21 0.36 rg")
    ops.append(f"{left:.1f} {table_top - row_h:.1f} {right - left:.1f} {row_h:.1f} re f")
    ops.append("0.93 0.95 0.97 rg")
    for i in range(len(LINE_ITEMS)):
        if i % 2 == 1:
            y = table_top - row_h * (i + 2)
            ops.append(f"{left:.1f} {y:.1f} {right - left:.1f} {row_h:.1f} re f")

    ops.append("0.8 0.83 0.88 RG")
    ops.append("0.6 w")
    y = table_top
    for i in range(len(LINE_ITEMS) + 2):
        yy = table_top - row_h * i
        ops.append(f"{left:.1f} {yy:.1f} m {right:.1f} {yy:.1f} l S")
    for x in [left, *col_x[1:], right]:
        ops.append(f"{x:.1f} {table_top:.1f} m {x:.1f} {table_bottom:.1f} l S")

    ops.append("1 1 1 rg")
    header_text_y = table_top - 15
    for x, label in zip(col_x, headers):
        ops.append(_tj(x, header_text_y, label, "F2", 9))

    ops.append("0.05 0.07 0.13 rg")
    for i, item in enumerate(LINE_ITEMS):
        y = table_top - row_h * (i + 1) - 15
        for x, value in zip(col_x, item):
            ops.append(_tj(x, y, value, "F1", 9))

    ops.append(_tj(50, table_bottom - 28, "Notes:", "F2", 11))
    ops.append(_tj(50, table_bottom - 44, "Ship to Dock 3. Reference PO-2026-0918 on packing slips.", "F1", 9))

    stream = "\n".join(ops) + "\n"
    stream_bytes = stream.encode("latin-1")

    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            "<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {page_w} {page_h}] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>"
        ),
        f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("latin-1")
        + stream_bytes
        + b"endstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    ]

    chunks: list[bytes] = [b"%PDF-1.4\n"]
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(sum(len(c) for c in chunks))
        if isinstance(obj, bytes):
            chunks.append(f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n")
        else:
            chunks.append(f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1"))

    xref_pos = sum(len(c) for c in chunks)
    xref = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    for off in offsets[1:]:
        xref.append(f"{off:010d} 00000 n \n")
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    )
    pdf = b"".join(chunks) + "".join(xref).encode("latin-1") + trailer.encode("latin-1")
    path.write_bytes(pdf)
    return path


if __name__ == "__main__":
    out = build_sample_po()
    print(f"Wrote {out}")
