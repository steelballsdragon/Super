"""Builds Stock_Consolidation.xlsm and refreshes the demo block in the SQL file.

    python3 build_workbook.py

Needs: pip install xlsxwriter
"""
import datetime
import io
from pathlib import Path

import xlsxwriter

import demo_data as d
import planner
import sql_demo
from vba_project import build_vba_project

ROOT = Path(__file__).resolve().parent.parent
XLSM = ROOT / "Stock_Consolidation.xlsm"
BAS = ROOT / "modConsolidation.bas"

NAVY = "#1F3864"
GREY_TEXT = "#595959"
GRID = "#D9D9D9"
GROUP_LINE = "#8EA0BD"
BAND = "#EAF1FB"
GREEN = "#006100"

SHEETS = [  # (tab name, VBA code name)
    ("Start", "shtStart"),
    ("Inventory", "shtInventory"),
    ("Locations", "shtLocations"),
    ("Consolidation Plan", "shtPlan"),
    ("Not Consolidated", "shtNotMoved"),
]

PLAN_HEADERS = ["Item Number", "From Location", "Qty to Move", "To Location",
                "Target Open Capacity", "Target Max Capacity", "Target Zone",
                "From Zone", "Target Open After Move"]
PLAN_KEYS = ["item_number", "from_location", "quantity_to_move", "to_location",
             "target_open_capacity", "target_max_capacity", "target_zone",
             "from_zone", "target_open_after_move"]
FAIL_HEADERS = ["Item Number", "Location", "Zone", "Quantity", "Room Elsewhere", "Reason"]
FAIL_KEYS = ["item_number", "location", "zone", "quantity", "room_elsewhere", "reason"]

RULES = [
    "Only items sitting in 2 or more locations are looked at.",
    "Locations are emptied smallest quantity first.",
    "Stock only goes to locations that already hold the same item.",
    "A location is only planned if it can be emptied completely:",
    "     - one move if a location has room for all of it (the one already holding the most of the item wins);",
    "     - otherwise, if splitting is allowed, spread over the locations with the most room first.",
    "A location that is emptied never receives stock, and a location that receives stock is never emptied.",
    "Open capacity = max capacity - everything in the location (all items), updated as moves are planned.",
    "Locations in ignored zones, and stock with zero or negative quantity, are left out.",
]


class Formats:
    def __init__(self, wb):
        self.wb, self.cache = wb, {}

    def __call__(self, **props):
        key = tuple(sorted(props.items()))
        if key not in self.cache:
            self.cache[key] = self.wb.add_format(props)
        return self.cache[key]


def settings_line(built):
    ex = d.EXCLUDE_ZONES or "none"
    yn = lambda b: "Yes" if b else "No"
    return (f"Built {built}   |   Same zone only: {yn(d.SAME_ZONE_ONLY)}   |   "
            f"Split moves: {yn(d.ALLOW_SPLIT)}   |   Excluded zones: {ex}")


def write_result_sheet(ws, fmt, title, subtitle, headers, keys, rows, text_cols, qty_cols,
                       widths, empty_msg, left_cols=(0,), bold_qty_col=None):
    """Mirrors the formatting the VBA macro applies (StartSheet/WriteHeaders/FormatTable)."""
    ws.write("A1", title, fmt(bold=True, font_size=16, font_color=NAVY))
    ws.write("A2", subtitle, fmt(font_size=9, font_color=GREY_TEXT))
    ws.set_row(3, 30)
    head = fmt(bold=True, font_color="#FFFFFF", bg_color=NAVY, align="center",
               valign="vcenter", text_wrap=True)
    for c, h in enumerate(headers):
        ws.write(3, c, h, head)
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)

    if not rows:
        ws.write(4, 0, empty_msg, fmt(italic=True, font_color=GREY_TEXT))
    whole = all(abs(r[keys[c]] - int(r[keys[c]])) < 1e-9 for r in rows for c in qty_cols)
    num_fmt = "#,##0" if whole else "#,##0.00"
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
                p["num_format"] = num_fmt
            if c == 0 and first:
                p["bold"] = True
            if c == bold_qty_col:
                p.update(bold=True, font_color=GREEN)
            v = r[k]
            if v is None:
                ws.write_blank(4 + i, c, None, fmt(**p))
            else:
                ws.write(4 + i, c, v, fmt(**p))
    if rows:
        ws.autofilter(3, 0, 3 + len(rows), len(headers) - 1)
    ws.freeze_panes(4, 0)
    ws.set_landscape()
    ws.fit_to_pages(1, 0)
    ws.repeat_rows(3)
    ws.set_margins(left=0.4, right=0.4, top=0.5, bottom=0.5)


def build():
    sql_demo.write_sql()

    moves, not_moved, summary = planner.plan(d.INVENTORY, d.LOCATIONS, d.SAME_ZONE_ONLY,
                                             d.ALLOW_SPLIT, d.EXCLUDE_ZONES)
    built = datetime.datetime.now().strftime("%d-%b-%Y %H:%M")

    vba = build_vba_project([("modConsolidation", BAS.read_text(encoding="cp1252"))],
                            "ThisWorkbook", [code for _, code in SHEETS])

    wb = xlsxwriter.Workbook(str(XLSM))
    wb.set_vba_name("ThisWorkbook")
    wb.add_vba_project(io.BytesIO(vba), is_stream=True)
    wb.set_properties({"title": "Stock Consolidation", "subject": "Bin consolidation planner"})
    fmt = Formats(wb)
    ws = {}
    for tab, code in SHEETS:
        ws[tab] = wb.add_worksheet(tab)
        ws[tab].set_vba_name(code)

    # ---- Start ---------------------------------------------------------------
    s = ws["Start"]
    s.hide_gridlines(2)
    s.set_column("A:A", 2)
    s.set_column("B:B", 46)
    s.set_column("C:C", 18)
    s.set_column("D:D", 3)
    s.set_column("E:E", 30)
    s.write("B2", "Stock Consolidation", fmt(bold=True, font_size=20, font_color=NAVY))
    s.write("B3", "Finds items stored in several locations and plans the moves that empty locations.",
            fmt(font_color=GREY_TEXT))

    h2 = fmt(bold=True, font_size=12, font_color=NAVY, bottom=1, bottom_color=NAVY)
    s.write("B5", "How to use", h2)
    s.write("C5", "", h2)
    steps = [
        "1.  Paste on-hand stock into the Inventory sheet (item_number, location, quantity).",
        "2.  Paste the location master into the Locations sheet (location, zone, max_capacity).",
        "     Tip: run consolidation_plan.sql with @ShowInputs = 1 and copy both grids with headers.",
        "3.  Check the settings below.",
        "4.  Press Build Consolidation Plan. Results go to 'Consolidation Plan' and 'Not Consolidated'.",
    ]
    for i, t in enumerate(steps):
        s.write(5 + i, 1, t, fmt(font_color="#262626"))

    s.write("B12", "Settings", h2)
    s.write("C12", "", h2)
    label = fmt(font_color="#262626", valign="vcenter")
    inp = fmt(bg_color="#FFF2CC", border=1, border_color="#BF9000", align="center",
              valign="vcenter", bold=True)
    settings = [
        ("SameZoneOnly", "Only move within the same zone", "Yes" if d.SAME_ZONE_ONLY else "No", True),
        ("AllowSplit", "Allow splitting a location over several targets", "Yes" if d.ALLOW_SPLIT else "No", True),
        ("ExcludeZones", "Zones to ignore (comma separated)", d.EXCLUDE_ZONES, False),
    ]
    for i, (name, text, value, yes_no) in enumerate(settings):
        row = 12 + i
        s.set_row(row, 20)
        s.write(row, 1, text, label)
        s.write_string(row, 2, value, inp)
        wb.define_name(name, f"=Start!$C${row + 1}")
        if yes_no:
            s.data_validation(row, 2, row, 2, {"validate": "list", "source": ["Yes", "No"]})

    s.write("B17", "Status", h2)
    s.write("C17", "", h2)
    s.write("B18", "Showing the plan for the sample data. Paste your own data and press Build Consolidation Plan.",
            fmt(italic=True, font_color=GREY_TEXT))
    wb.define_name("LastRun", "=Start!$B$18")

    s.write("B20", "How the plan is worked out", h2)
    s.write("C20", "", h2)
    n_rule = 0
    for i, rule in enumerate(RULES):
        if not rule.startswith(" "):
            n_rule += 1
            rule = f"{n_rule}.  {rule}"
        s.write(20 + i, 1, rule, fmt(font_color=GREY_TEXT, font_size=9))

    s.insert_button("E5", {"macro": "BuildConsolidationPlan", "caption": "Build Consolidation Plan",
                           "width": 210, "height": 46})
    s.insert_button("E9", {"macro": "ClearResults", "caption": "Clear Results",
                           "width": 210, "height": 28})
    s.activate()
    s.set_landscape()
    s.fit_to_pages(1, 1)

    # ---- Inputs --------------------------------------------------------------
    in_head = fmt(bold=True, font_color="#FFFFFF", bg_color=NAVY, border=1, border_color=NAVY)
    text = fmt(num_format="@")

    inv = ws["Inventory"]
    inv.freeze_panes(1, 0)
    inv.set_column("A:B", 16, text)
    inv.set_column("C:C", 12)
    inv.write_row(0, 0, ["item_number", "location", "quantity"], in_head)
    for i, (item, loc, qty) in enumerate(d.INVENTORY, start=1):
        inv.write_string(i, 0, item, text)
        inv.write_string(i, 1, loc, text)
        inv.write_number(i, 2, qty)
    inv.write("E1", "Paste your stock here (headers in row 1). Extra columns are ignored.",
              fmt(italic=True, font_color=GREY_TEXT))

    loc_ws = ws["Locations"]
    loc_ws.freeze_panes(1, 0)
    loc_ws.set_column("A:B", 16, text)
    loc_ws.set_column("C:C", 14)
    loc_ws.write_row(0, 0, ["location", "zone", "max_capacity"], in_head)
    for i, (loc, zone, cap) in enumerate(d.LOCATIONS, start=1):
        loc_ws.write_string(i, 0, loc, text)
        loc_ws.write_string(i, 1, zone, text)
        if cap is not None:
            loc_ws.write_number(i, 2, cap)
    loc_ws.write("E1", "Paste your location master here. max_capacity = most units the location holds.",
                 fmt(italic=True, font_color=GREY_TEXT))

    # ---- Results (what the macro produces for the sample data) ---------------
    p = ws["Consolidation Plan"]
    write_result_sheet(p, fmt, "Consolidation Plan", settings_line(built), PLAN_HEADERS, PLAN_KEYS,
                       moves, text_cols={0, 1, 3, 6, 7}, qty_cols={2, 4, 5, 8},
                       widths=[14, 15, 12, 14, 13, 13, 12, 12, 14, 3, 22, 10],
                       empty_msg="No moves found - nothing can be consolidated with this data and these settings.",
                       bold_qty_col=2)
    sh = fmt(bold=True, font_color="#FFFFFF", bg_color=NAVY, border=1, border_color="#C8CED8",
             valign="vcenter")
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
                       widths=[14, 14, 12, 12, 14, 52], left_cols=(0, 5),
                       empty_msg="Every multi-location item can be consolidated.")

    wb.close()
    return moves, not_moved, summary


if __name__ == "__main__":
    mv, nm, sm = build()
    print(f"built {XLSM.name}: {len(mv)} moves, {len(nm)} not consolidated, summary {sm}")
