"""Builds Stock_Consolidation.xlsm and refreshes the demo block in the SQL file.

    python3 build_workbook.py

Needs: pip install xlsxwriter
"""
import datetime
import io
from collections import Counter
from pathlib import Path

import xlsxwriter

import demo_data as d
import planner
import sql_demo
from vba_project import build_vba_project

ROOT = Path(__file__).resolve().parent.parent
XLSM = ROOT / "Stock_Consolidation.xlsm"                # empty, ready for your report
XLSM_SAMPLE = ROOT / "Stock_Consolidation_Sample.xlsm"  # with the screenshot rows and their result
PRESS = "Press 'Build Consolidation Plan' on the Start sheet."
BAS = ROOT / "VBA_Code.txt"

NAVY = "#1F3864"
GREY_TEXT = "#595959"
GRID = "#D9D9D9"
GROUP_LINE = "#8EA0BD"
BAND = "#EAF1FB"
AMBER = "#FFF2CC"
GREEN = "#006100"

SHEETS = [  # (tab name, VBA code name)
    ("Start", "shtStart"),
    ("Raw Data", "shtRaw"),
    ("Clean Data", "shtClean"),
    ("Consolidation Plan", "shtPlan"),
    ("Not Consolidated", "shtNotMoved"),
]

CLEAN_HEADERS = ["Item Number", "Location", "Current Qty", "Available Capacity", "Max Qty", "Location Type"]
CLEAN_KEYS = ["item_number", "location", "current_qty", "available_capacity", "max_qty", "location_type"]
PLAN_HEADERS = ["Item Number", "From Location", "Qty to Move", "To Location",
                "Target Open Capacity", "Target Max Capacity", "Target Location Type",
                "From Location Type", "Target Open After Move"]
PLAN_KEYS = ["item_number", "from_location", "quantity_to_move", "to_location",
             "target_open_capacity", "target_max_capacity", "target_location_type",
             "from_location_type", "target_open_after_move"]
FAIL_HEADERS = ["Item Number", "Location", "Location Type", "Quantity", "Room Elsewhere", "Reason"]
FAIL_KEYS = ["item_number", "location", "location_type", "quantity", "room_elsewhere", "reason"]

RULES = [
    "Only Prtnum, Stoloc, Max of Curqvl, Max of Fp Available, Max of Maxqvl and Typcod are used.",
    "Rows with 0 or a negative current qty or max qty are removed (and available capacity, if set above).",
    "Only locations whose max qty is within the limits above (9 to 23) are kept.",
    "Only items sitting in 2 or more locations are looked at; locations are emptied smallest quantity first.",
    "Stock only goes to locations that already hold the same item.",
    "A location is only planned if it can be emptied completely:",
    "     - one move if a location has room for all of it (the one already holding the most of the item wins);",
    "     - otherwise, if splitting is allowed, spread over the locations with the most room first.",
    "A location that is emptied never receives stock, and a location that receives stock is never emptied.",
    "Open capacity starts at Fp Available and is updated as moves are planned, so no location is overfilled.",
]


class Formats:
    def __init__(self, wb):
        self.wb, self.cache = wb, {}

    def __call__(self, **props):
        key = tuple(sorted(props.items()))
        if key not in self.cache:
            self.cache[key] = self.wb.add_format(props)
        return self.cache[key]


def yn(b):
    return "Yes" if b else "No"


def qty_text(v):
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-9 else f"{v:,.2f}"


def settings_line(built):
    return (f"Built {built}   |   Max qty {qty_text(d.MIN_LOCATION_QTY)} to {qty_text(d.MAX_LOCATION_QTY)}   |   "
            f"Same type only: {yn(d.SAME_TYPE_ONLY)}   |   Split moves: {yn(d.ALLOW_SPLIT)}   |   "
            f"Ignored types: {d.EXCLUDE_TYPES or 'none'}")


def stats_line(stats):
    return (f"Rows read: {stats['rows_read']}   |   Removed - zero or negative qty/capacity: "
            f"{stats['removed_zero_or_negative']}   |   Removed - max qty outside "
            f"{qty_text(d.MIN_LOCATION_QTY)}-{qty_text(d.MAX_LOCATION_QTY)}: "
            f"{stats['removed_max_qty_out_of_range']}   |   Removed - ignored type: {stats['removed_ignored_type']}"
            f"   |   Kept: {stats['rows_kept']}")


def num_format(rows, keys, cols):
    whole = all(abs(r[keys[c]] - round(r[keys[c]])) < 1e-6 for r in rows for c in cols)
    return "#,##0" if whole else "#,##0.00"


def sheet_top(ws, fmt, title, subtitle, headers, widths):
    """Mirrors the VBA StartSheet / WriteHeaders."""
    ws.write("A1", title, fmt(bold=True, font_size=16, font_color=NAVY))
    ws.write("A2", subtitle, fmt(font_size=9, font_color=GREY_TEXT))
    ws.set_row(3, 30)
    head = fmt(bold=True, font_color="#FFFFFF", bg_color=NAVY, align="center",
               valign="vcenter", text_wrap=True)
    for c, h in enumerate(headers):
        ws.write(3, c, h, head)
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)


def sheet_bottom(ws, n_rows, n_cols):
    """Mirrors the VBA FinishSheet, plus print settings."""
    if n_rows:
        ws.autofilter(3, 0, 3 + n_rows, n_cols - 1)
    ws.freeze_panes(4, 0)
    ws.set_landscape()
    ws.fit_to_pages(1, 0)
    ws.repeat_rows(3)
    ws.set_margins(left=0.4, right=0.4, top=0.5, bottom=0.5)


def write_result_sheet(ws, fmt, title, subtitle, headers, keys, rows, text_cols, qty_cols,
                       widths, empty_msg, left_cols=(0,), bold_qty_col=None):
    """Mirrors the formatting the VBA macro applies (FormatTable / BandGroup)."""
    sheet_top(ws, fmt, title, subtitle, headers, widths)
    if not rows and empty_msg:
        ws.write(4, 0, empty_msg, fmt(italic=True, font_color=GREY_TEXT))
    nf = num_format(rows, keys, qty_cols)
    shade, prev = False, None
    for i, r in enumerate(rows):
        item = r[keys[0]].upper()
        first = item != prev
        if first and prev is not None:
            shade = not shade
        prev = item
        for c, k in enumerate(keys):
            p = dict(font_size=10, valign="vcenter", border=1, border_color=GRID,
                     align="left" if c in left_cols else "center")
            if first:
                p.update(top=1, top_color=GROUP_LINE)
            if shade:
                p["bg_color"] = BAND
            if c in text_cols:
                p["num_format"] = "@"
            if c in qty_cols:
                p["num_format"] = nf
            if c == 0 and first:
                p["bold"] = True
            if c == bold_qty_col:
                p.update(bold=True, font_color=GREEN)
            ws.write(4 + i, c, r[k], fmt(**p))
    sheet_bottom(ws, len(rows), len(headers))


def build():
    sql_demo.write_sql()
    build_one(XLSM, sample=False)
    return build_one(XLSM_SAMPLE, sample=True)


def build_one(path, sample):
    """sample=True: Raw Data holds the screenshot rows and the result sheets show their plan.
    sample=False: Raw Data has only its header row and the result sheets are empty."""
    clean, stats, moves, not_moved, summary = planner.run(
        d.RAW if sample else [], d.MAX_LOCATION_QTY, d.REMOVE_NO_CAPACITY, d.SAME_TYPE_ONLY, d.ALLOW_SPLIT,
        d.EXCLUDE_TYPES, d.MIN_LOCATION_QTY)
    built = datetime.datetime.now().strftime("%d-%b-%Y %H:%M")

    vba = build_vba_project([("modConsolidation", BAS.read_text(encoding="cp1252"))],
                            "ThisWorkbook", [code for _, code in SHEETS])

    wb = xlsxwriter.Workbook(str(path))
    wb.set_vba_name("ThisWorkbook")
    wb.add_vba_project(io.BytesIO(vba), is_stream=True)
    wb.set_properties({"title": "Stock Consolidation", "subject": "Location consolidation planner"})
    fmt = Formats(wb)
    ws = {}
    for tab, code in SHEETS:
        ws[tab] = wb.add_worksheet(tab)
        ws[tab].set_vba_name(code)

    # ---- Start ---------------------------------------------------------------
    s = ws["Start"]
    s.hide_gridlines(2)
    s.set_column("A:A", 2)
    s.set_column("B:B", 50)
    s.set_column("C:C", 16)
    s.set_column("D:D", 3)
    s.set_column("E:E", 30)
    s.write("B2", "Stock Consolidation", fmt(bold=True, font_size=20, font_color=NAVY))
    s.write("B3", "Cleans the location report, finds items stored in several locations and plans "
                  "the moves that empty locations.", fmt(font_color=GREY_TEXT))

    h2 = fmt(bold=True, font_size=12, font_color=NAVY, bottom=1, bottom_color=NAVY)
    s.write("B5", "How to use", h2)
    s.write("C5", "", h2)
    steps = [
        "1.  Paste the location report into the Raw Data sheet (all columns, headers in row 1).",
        "2.  Check the settings below.",
        "3.  Press Build Consolidation Plan.",
        "     Clean Data = the report with only the needed columns, after removing rows.",
        "     Consolidation Plan = the moves.   Not Consolidated = locations that have to stay.",
    ]
    for i, t in enumerate(steps):
        s.write(5 + i, 1, t, fmt(font_color="#262626"))

    s.write("B12", "Settings", h2)
    s.write("C12", "", h2)
    label = fmt(font_color="#262626", valign="vcenter")
    inp = fmt(bg_color=AMBER, border=1, border_color="#BF9000", align="center", valign="vcenter", bold=True)
    settings = [
        ("MinLocationQty", "Only locations with max qty (Maxqvl) from", d.MIN_LOCATION_QTY, None),
        ("MaxLocationQty", "... up to", d.MAX_LOCATION_QTY, None),
        ("RemoveNoCapacity", "Remove rows with 0 / negative available capacity", yn(d.REMOVE_NO_CAPACITY), "yn"),
        ("SameTypeOnly", "Only move within the same location type (Typcod)", yn(d.SAME_TYPE_ONLY), "yn"),
        ("AllowSplit", "Allow emptying a location into several locations", yn(d.ALLOW_SPLIT), "yn"),
        ("ExcludeTypes", "Location types to ignore (comma separated)", d.EXCLUDE_TYPES, None),
    ]
    for i, (name, text, value, kind) in enumerate(settings):
        row = 12 + i
        s.set_row(row, 20)
        s.write(row, 1, text, label)
        if isinstance(value, (int, float)):
            s.write_number(row, 2, value, inp)
            s.data_validation(row, 2, row, 2, {"validate": "decimal", "criteria": ">", "value": 0})
        else:
            s.write_string(row, 2, value, inp)
        wb.define_name(name, f"=Start!$C${row + 1}")
        if kind == "yn":
            s.data_validation(row, 2, row, 2, {"validate": "list", "source": ["Yes", "No"]})

    s.write("B20", "Status", h2)
    s.write("C20", "", h2)
    status = ("Showing the result for the sample rows. Paste your own report into Raw Data and press "
              "Build Consolidation Plan." if sample else
              "Paste your report into the Raw Data sheet (headers in row 1) and press Build Consolidation Plan.")
    s.write("B21", status, fmt(italic=True, font_color=GREY_TEXT))
    wb.define_name("LastRun", "=Start!$B$21")

    s.write("B23", "How it works", h2)
    s.write("C23", "", h2)
    n_rule = 0
    for i, rule in enumerate(RULES):
        if not rule.startswith(" "):
            n_rule += 1
            rule = f"{n_rule}.  {rule}"
        s.write(23 + i, 1, rule, fmt(font_color=GREY_TEXT, font_size=9))

    s.insert_button("E5", {"macro": "BuildConsolidationPlan", "caption": "Build Consolidation Plan",
                           "width": 210, "height": 46})
    s.insert_button("E9", {"macro": "ClearResults", "caption": "Clear Results", "width": 210, "height": 28})
    s.activate()
    s.set_landscape()
    s.fit_to_pages(1, 1)

    # ---- Raw Data (the report exactly as exported) ---------------------------
    raw = ws["Raw Data"]
    raw.freeze_panes(1, 0)
    text = fmt(num_format="@")
    raw.set_column("A:B", 11, text)
    raw.set_column("C:E", 12)
    raw.set_column("F:I", 11)
    raw.set_column("J:M", 12)
    raw_head = fmt(bold=True, font_color="#FFFFFF", bg_color=NAVY, border=1, border_color=NAVY,
                   text_wrap=True, valign="vcenter")
    raw.set_row(0, 30)
    raw.write_row(0, 0, d.RAW_HEADERS, raw_head)
    for i, row in enumerate(d.RAW_ROWS if sample else [], start=1):
        for c, v in enumerate(row):
            if isinstance(v, str):
                raw.write_string(i, c, v, text if c < 2 else None)
            else:
                raw.write_number(i, c, v)

    # ---- Clean Data ----------------------------------------------------------
    cl = ws["Clean Data"]
    sheet_top(cl, fmt, "Clean Data", stats_line(stats) if sample else PRESS, CLEAN_HEADERS,
              [14, 12, 12, 13, 10, 13, 3, 22])
    multi = {k for k, n in Counter(r["item_number"].upper() for r in clean).items() if n >= 2}
    nf = num_format(clean, CLEAN_KEYS, [2, 3, 4])
    for i, r in enumerate(clean):
        hot = r["item_number"].upper() in multi
        for c, k in enumerate(CLEAN_KEYS):
            p = dict(font_size=10, valign="vcenter", border=1, border_color=GRID,
                     align="left" if c == 0 else "center")
            p["num_format"] = "@" if c in (0, 1, 5) else nf
            if hot:
                p["bg_color"] = AMBER
                if c == 0:
                    p["bold"] = True
            cl.write(4 + i, c, r[k], fmt(**p))
    if sample:
        cl.write("H4", "Item in 2+ locations", fmt(bg_color=AMBER, bold=True, align="center", valign="vcenter",
                                                   border=1, border_color=GRID))
    sheet_bottom(cl, len(clean), len(CLEAN_HEADERS))

    # ---- Results ------------------------------------------------------------
    p = ws["Consolidation Plan"]
    write_result_sheet(p, fmt, "Consolidation Plan", settings_line(built) if sample else PRESS,
                       PLAN_HEADERS, PLAN_KEYS, moves, text_cols={0, 1, 3, 6, 7}, qty_cols={2, 4, 5, 8},
                       widths=[14, 15, 12, 14, 13, 13, 13, 13, 14, 3, 22, 10],
                       empty_msg="No moves found - nothing can be consolidated with this data and these settings."
                       if sample else None,
                       bold_qty_col=2)
    if not sample:
        write_result_sheet(ws["Not Consolidated"], fmt, "Not Consolidated", PRESS, FAIL_HEADERS, FAIL_KEYS, [],
                           text_cols={0, 1, 2, 5}, qty_cols={3, 4}, widths=[14, 14, 13, 12, 14, 56],
                           left_cols=(0, 5), empty_msg=None)
        wb.close()
        return clean, stats, moves, not_moved, summary

    sh = fmt(bold=True, font_color="#FFFFFF", bg_color=NAVY, border=1, border_color="#C8CED8", valign="vcenter")
    p.write("K4", "Summary", sh)
    p.write_blank("L4", None, sh)
    lab = fmt(font_color="#404040", border=1, border_color="#C8CED8")
    val = fmt(bold=True, num_format="#,##0", align="right", border=1, border_color="#C8CED8")
    for i, (text_, key) in enumerate([("Items in 2+ locations", "items_in_multiple_locations"),
                                      ("Locations emptied", "locations_emptied"),
                                      ("Moves", "moves"),
                                      ("Units to move", "units_to_move"),
                                      ("Locations that stay", "locations_not_emptied")]):
        p.write(4 + i, 10, text_, lab)
        p.write_number(4 + i, 11, summary[key], val)

    n = ws["Not Consolidated"]
    write_result_sheet(n, fmt, "Not Consolidated",
                       "Locations of multi-location items that cannot be emptied with the space available.   "
                       + settings_line(built),
                       FAIL_HEADERS, FAIL_KEYS, not_moved, text_cols={0, 1, 2, 5}, qty_cols={3, 4},
                       widths=[14, 14, 13, 12, 14, 56], left_cols=(0, 5),
                       empty_msg="Every multi-location item can be consolidated.")

    wb.close()
    return clean, stats, moves, not_moved, summary


if __name__ == "__main__":
    cl, st, mv, nm, sm = build()
    print(f"built {XLSM.name} (empty) and {XLSM_SAMPLE.name}: {st['rows_kept']} clean rows, {len(mv)} moves, "
          f"{len(nm)} not consolidated, summary {sm}")
