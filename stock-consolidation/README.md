# Stock consolidation

Finds items stored in more than one location and plans how many units to move
from which location to which, so that locations can be emptied.

| File | What it is |
|---|---|
| `consolidation_plan.sql` | SQL Server script. Returns the move list (same columns as before, plus `from_zone` and `target_open_after_move`), the locations that can't be emptied, and a summary. |
| `Stock_Consolidation.xlsm` | Excel workbook with a VBA macro that applies the same rules and builds formatted result sheets. |
| `modConsolidation.bas` | The VBA code on its own (already inside the workbook). |
| `generator/` | Python scripts that generate the workbook and the SQL demo data (`python3 generator/build_workbook.py`, needs `pip install xlsxwriter`). |

## SQL

1. In section 1 of the script, change the two `INSERT ... SELECT` queries to read
   your on-hand stock table and your location master (warehouse/status filters
   are there as comments).
2. Run it. Set `@UseDemoData = 1` to try it on the built-in sample first.

Settings at the top: `@SameZoneOnly`, `@AllowSplit`, `@ExcludeZones`
(comma-separated, default `STAGE`).

## Excel

1. Right-click the downloaded file → **Properties** → tick **Unblock** → OK.
   Windows blocks macros in files that come from the internet.
2. Open it and click **Enable Content**.
3. Paste your stock into **Inventory** (`item_number, location, quantity`) and
   the location master into **Locations** (`location, zone, max_capacity`), with
   headers in row 1. The macro finds columns by header name, so extra columns
   are fine. The easy way to get these: run the SQL with `@ShowInputs = 1` and
   copy the first two result grids with headers. Keep columns A:B formatted as
   Text so codes like `00123` keep their leading zeros.
4. Press **Build Consolidation Plan** on the Start sheet.

The workbook opens showing the plan for the sample data. The SQL script with
`@UseDemoData = 1` gives exactly the same rows.

If Excel ever reports a problem with the VBA project, delete it and add
`modConsolidation.bas` instead (Alt+F11 → File → Import File), then right-click
each button → Assign Macro.

## Rules (identical in SQL and VBA)

1. Only items sitting in 2 or more locations are looked at.
2. Locations are emptied smallest quantity first.
3. Stock only goes to locations that already hold the same item.
4. A location is only planned if it can be emptied completely: one move if a
   location has room for all of it (the one already holding the most of the
   item wins, then the one with the least spare room). Otherwise, if splitting
   is allowed, the stock is spread over the locations with the most room first.
5. A location that is emptied never receives stock, and a location that
   receives stock is never emptied.
6. Open capacity = max capacity − everything in the location (all items). It
   goes down as moves are planned, so two moves never overfill the same location.
7. Locations in ignored zones, and stock with a zero or negative quantity, are
   left out. Locations missing from the location master can be emptied, but
   never receive stock (their capacity is unknown).
