import base64
import io

import pandas as pd
from datapackage import Package
from PIL import Image as PILImage
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

_SKIP_INDEX_NAMES = {"index", None}

_TABLE_STYLE = TableStyle(
    [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c6fad")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f0f4f8")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
)


def _fmt(v):
    if pd.isna(v):
        return ""
    if isinstance(v, float):
        if v == float("inf") or v == float("-inf"):
            return "-"
        return f"{v:,.2f}"
    return str(v)


def _df_to_rl_table(df, page_width, cell_style, header_cell_style):
    """Render a DataFrame as a ReportLab Table flowable."""
    # Include meaningful index (e.g. kpi names) as first column
    if df.index.name not in _SKIP_INDEX_NAMES:
        df = df.reset_index()

    headers = [Paragraph(str(c).replace("_", " ").title(), header_cell_style) for c in df.columns]

    rows = [[Paragraph(_fmt(v), cell_style) for v in row] for _, row in df.iterrows()]

    col_width = page_width / len(df.columns)
    table = Table([headers] + rows, colWidths=[col_width] * len(df.columns))
    table.setStyle(_TABLE_STYLE)
    return table


def create_wefe_pdf_report(dp_path, project_name, tables, services, units, image_list, label_map=None):
    """
    Build a WEFE results PDF with ReportLab.

    Args:
        dp_path: Path object to scenario datapackage
        project_name: str
        tables: dict of {name: DataFrame} — scalar result tables
        services: dict of {name: DataFrame} — service flow tables
        units: dict of {param_name: unit_string}
        image_list: list of base64 PNG data URLs from Plotly.toImage
        label_map: dict of {param_name: verbose_name}

    Returns:
        BytesIO buffer containing the PDF
    """
    buffer = io.BytesIO()
    margin = 0.75 * inch

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin,
    )

    page_width = A4[0] - 2 * margin
    page_height = A4[1] - 2 * margin

    base_styles = getSampleStyleSheet()

    h1 = ParagraphStyle("H1", parent=base_styles["Heading1"], fontSize=15, spaceAfter=8, spaceBefore=16)
    h2 = ParagraphStyle("H2", parent=base_styles["Heading2"], fontSize=12, spaceAfter=6, spaceBefore=10)
    cell = ParagraphStyle("Cell", parent=base_styles["Normal"], fontSize=8)
    header_cell = ParagraphStyle(
        "HeaderCell", parent=base_styles["Normal"], fontSize=9, fontName="Helvetica-Bold", textColor=colors.white
    )

    elements = []

    # --- Verbose Names ---
    p0 = Package(dp_path)

    # Dynamic label mapping to use verbose names (if available)
    if label_map is None:
        label_map = {}

    for resource_name in p0.resource_names:
        try:
            df = pd.DataFrame.from_records(p0.get_resource(resource_name).read(keyed=True))

            if "name" in df.columns and "verbose_name" in df.columns:
                label_map.update(
                    {row["name"]: row["verbose_name"] for _, row in df.iterrows() if pd.notna(row["verbose_name"])}
                )

        except Exception:
            pass

    def display_name(name):
        return label_map.get(name, name)

    # --- Title ---
    elements.append(Paragraph("WEFE Results Report", base_styles["Title"]))
    elements.append(Paragraph(f"Project: {project_name}", h2))
    elements.append(Spacer(1, 0.2 * inch))

    # --- Scalar Results ---
    elements.append(Paragraph("Scalar Results", h1))

    # Capacities
    if "capacities" in tables:
        elements.append(Paragraph("Capacities", h2))
        df = tables["capacities"]
        if "Component name" in df.columns:
            df["Component name"] = df["Component name"].apply(display_name)
        elements.append(_df_to_rl_table(df, page_width, cell, header_cell))
        elements.append(Spacer(1, 0.15 * inch))

    # KPIs — augment with units from parameters_units
    if "kpis" in tables:
        elements.append(Paragraph("KPIs", h2))
        kpis_df = tables["kpis"].reset_index()  # → columns: [kpi, value]
        if "kpi" in kpis_df.columns:
            kpis_df["kpi"] = kpis_df["kpi"].apply(display_name)
        kpis_df["unit"] = kpis_df["kpi"].map(units).fillna("")
        elements.append(_df_to_rl_table(kpis_df, page_width, cell, header_cell))
        elements.append(Spacer(1, 0.15 * inch))

    # Any additional result tables
    for name, df in tables.items():
        if name in ("capacities", "kpis"):
            continue
        elements.append(Paragraph(name.replace("-", " ").replace("_", " ").title(), h2))
        elements.append(_df_to_rl_table(df, page_width, cell, header_cell))
        elements.append(Spacer(1, 0.15 * inch))

    # --- Services ---
    if services:
        elements.append(Paragraph("Services", h1))
        for name, df in services.items():
            elements.append(Paragraph(name, h2))
            if "asset" in df.columns:
                df["asset"] = df["asset"].apply(display_name)
            elements.append(_df_to_rl_table(df, page_width, cell, header_cell))
            elements.append(Spacer(1, 0.15 * inch))

    # --- Dynamic Results (Plotly graphs as PNGs) ---
    if image_list:
        elements.append(PageBreak())
        elements.append(Paragraph("Dynamic Results", h1))
        elements.append(Spacer(1, 0.1 * inch))

        for png_data_url in image_list:
            img_bytes = base64.b64decode(png_data_url.replace("data:image/png;base64,", ""))
            img_io = io.BytesIO(img_bytes)
            pil_img = PILImage.open(img_io)
            w_px, h_px = pil_img.size

            # Scale to fit page width; Plotly renders at 96 DPI → convert to pts (72 DPI)
            w_pt = w_px * 72 / 96
            h_pt = h_px * 72 / 96

            if w_pt > page_width:
                scale = page_width / w_pt
                w_pt *= scale
                h_pt *= scale

            if h_pt > page_height:
                scale = page_height / h_pt
                w_pt *= scale
                h_pt *= scale

            img_io.seek(0)
            rl_img = Image(img_io, width=w_pt, height=h_pt)
            rl_img.hAlign = "CENTER"

            elements.append(KeepTogether([rl_img, Spacer(1, 0.2 * inch)]))

    doc.build(elements)
    buffer.seek(0)
    return buffer
