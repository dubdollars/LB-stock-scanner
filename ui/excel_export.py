"""
Excel export formatter.

Writes a pandas DataFrame to an openpyxl workbook with:
  - 1-row + 1-column padding offset (data starts at B2 instead of A1)
  - Light-gray header row with centered text
  - Thin borders around all data cells (table-style)
  - Auto-filter enabled on the header row
  - Column widths sized appropriately per column type

Matches the user's preferred format from legacy_brands_scan_20260518_0115.xlsx.
"""

from __future__ import annotations

import io
from typing import Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


# Light gray — matches the user's reference file (theme 0 with -0.15 tint ≈ #D9D9D9)
_HEADER_FILL = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
_HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="000000")
_HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=False)

_THIN_BORDER = Border(
    top=Side(style="thin", color="000000"),
    bottom=Side(style="thin", color="000000"),
    left=Side(style="thin", color="000000"),
    right=Side(style="thin", color="000000"),
)

_DATA_FONT = Font(name="Calibri", size=11, bold=False, color="000000")
_DATA_ALIGN_NUM = Alignment(horizontal="center", vertical="center")
_DATA_ALIGN_TEXT = Alignment(horizontal="left", vertical="center")

# Column-width hints. Anything not in this dict gets auto-sized from content.
# Widths chosen to roughly match the user's reference file.
_COLUMN_WIDTH_HINTS = {
    # Ticker / short identifiers
    "Ticker": 8,
    "RS": 5,
    "RSI": 6,
    "PEG": 6,
    "RS_NH": 7,
    "VCP_Quality": 11,
    "VDU": 8,
    "Index": 8,
    "Industry_Rank": 13,
    # Pills/text
    "Signals_Display": 18,
    "Warnings_Display": 18,
    "Fundamentals": 13,
    # Price/execution
    "Price": 9,
    "Buy_Trigger": 11,
    "Stop": 9,
    "Risk_Pct": 9,
    "Pct_Above_50MA": 16,
    "Vol_vs_Avg": 11,
    "Days_To_Earnings": 16,
    "Earnings_Date": 13,
    "Market_Cap": 11,
    "Sector": 11,
    "Industry": 36,
    # Short-scan columns
    "Setup_Tier": 12,
    "Score": 7,
    "RSI2": 7,
    "Weekly_RSI": 9,
    "Pct_Above_200MA": 17,
    "ATR_Stretch": 11,
    "Run_Pct_15d": 11,
    "Short_Below": 11,
    "Cover_Stop": 11,
    "Target_20MA": 11,
    "Target_50MA": 11,
}


def _estimate_width(col_name: str, series: pd.Series) -> float:
    """Estimate a sensible column width from content if not in the hints dict."""
    if col_name in _COLUMN_WIDTH_HINTS:
        return _COLUMN_WIDTH_HINTS[col_name]
    # Auto-size: max(header_length, max_data_length) + 2, clamped between 6 and 40
    try:
        max_data = series.astype(str).str.len().max()
    except Exception:
        max_data = 8
    width = max(len(str(col_name)), int(max_data) if not pd.isna(max_data) else 8) + 2
    return float(max(6, min(40, width)))


def format_dataframe_to_excel(
    df: pd.DataFrame,
    sheet_name: str = "Candidates",
    title: Optional[str] = None,
) -> bytes:
    """Write df to a styled XLSX and return the bytes for a download_button.

    Layout:
      - Optional title in A1 (skipped if `title` is None)
      - Headers in row 2 starting at column B
      - Data rows 3+, with thin borders and centered/text alignment
      - Auto-filter on the header row
      - Column widths sized per column type
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]  # Excel limits sheet names to 31 chars

    # Header row at row 2, starting at column B
    header_row = 2
    data_start_row = 3
    start_col = 2  # B

    for col_idx, col_name in enumerate(df.columns):
        cell = ws.cell(row=header_row, column=start_col + col_idx, value=col_name)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _HEADER_ALIGN
        cell.border = _THIN_BORDER

    # Data rows
    for r_offset, (_, row) in enumerate(df.iterrows()):
        for c_offset, col_name in enumerate(df.columns):
            value = row[col_name]
            # Handle pandas NA/NaN — write blank instead of literal 'nan'
            if pd.isna(value):
                cell_value = None
            elif isinstance(value, (list, tuple)):
                # Lists like Signals/Warnings show as joined text
                cell_value = " · ".join(str(v) for v in value) if value else None
            else:
                cell_value = value
            cell = ws.cell(
                row=data_start_row + r_offset,
                column=start_col + c_offset,
                value=cell_value,
            )
            cell.font = _DATA_FONT
            cell.border = _THIN_BORDER
            # Numeric columns get centered; text gets left-aligned
            if isinstance(value, (int, float)) and not pd.isna(value):
                cell.alignment = _DATA_ALIGN_NUM
            else:
                cell.alignment = _DATA_ALIGN_TEXT

    # Column widths
    for col_idx, col_name in enumerate(df.columns):
        letter = get_column_letter(start_col + col_idx)
        ws.column_dimensions[letter].width = _estimate_width(col_name, df[col_name])

    # Padding column A — narrow
    ws.column_dimensions["A"].width = 3

    # Auto-filter on the header row
    last_col_letter = get_column_letter(start_col + len(df.columns) - 1)
    ws.auto_filter.ref = f"B{header_row}:{last_col_letter}{header_row}"

    # Freeze panes so header stays visible while scrolling data
    ws.freeze_panes = f"B{data_start_row}"

    # Optional title cell in B1 (above the header)
    if title:
        title_cell = ws.cell(row=1, column=start_col, value=title)
        title_cell.font = Font(name="Calibri", size=14, bold=True, color="000000")
        ws.merge_cells(
            start_row=1, start_column=start_col,
            end_row=1, end_column=start_col + len(df.columns) - 1,
        )

    # Write to bytes
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue()
