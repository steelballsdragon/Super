# Stock consolidation

Cleans the location report, finds items stored in more than one location and
plans how many units to move from which location to which, so that locations
can be emptied.

| File | What it is |
|---|---|
| `consolidation_plan.sql` | SQL Server script: cleans the report and returns the moves, the locations that can't be emptied, a summary and the cleaned data. |
| `Stock_Consolidation.xlsm` | Excel workbook: paste the report, press a button, get the same result as formatted sheets. |
| `modConsolidation.bas` | The VBA code on its own (already inside the workbook). |
| `generator/` | Python scripts that generate the workbook and the SQL demo data (`python3 generator/build_workbook.py`, needs `pip install xlsxwriter`). |

## Columns used from the report

| Report column | Meaning |
|---|---|
| Prtnum | Item number |
| Stoloc | Location |
| Max of Curqvl | Current quantity |
| Max of Fp Available | Available capacity |
| Max of Maxqvl | Max quantity the location can hold |
| Typcod | Location type |

All other columns are ignored. Rows are removed when the current qty, max qty
or available capacity is 0 or negative, and locations whose max qty is over 23
are removed (both are settings).

## SQL

1. In section 1 of the script, change the `INSERT ... SELECT` so it reads the
   table or view behind the report.
2. Run it. Set `@UseDemoData = 1` to try it on the sample rows first.

Settings at the top: `@MaxLocationQty` (23), `@RemoveNoCapacity`,
`@SameTypeOnly`, `@AllowSplit`, `@ExcludeTypes`, `@ShowCleanData`.

## Excel

1. Right-click the downloaded file → **Properties** → tick **Unblock** → OK.
   Windows blocks macros in files that come from the internet.
2. Open it and click **Enable Content**.
3. Paste the whole report into **Raw Data** with its headers in row 1. Columns
   are found by header name, so the order and extra columns don't matter. Keep
   columns A:B formatted as Text so item numbers keep their leading zeros.
4. Check the settings on the Start sheet and press **Build Consolidation Plan**.

You get **Clean Data** (only the six columns, after removing rows; items in 2+
locations highlighted), **Consolidation Plan** (the moves plus a summary) and
**Not Consolidated** (locations that have to stay, with the reason).

The workbook opens showing the result for the sample rows from the screenshot.
The SQL script with `@UseDemoData = 1` gives exactly the same rows.

If Excel ever reports a problem with the VBA project, delete it and add
`modConsolidation.bas` instead (Alt+F11 → File → Import File), then right-click
each button → Assign Macro.

## Planning rules (identical in SQL and VBA)

1. Only items sitting in 2 or more locations are looked at.
2. Locations are emptied smallest quantity first.
3. Stock only goes to locations that already hold the same item.
4. A location is only planned if it can be emptied completely: one move if a
   location has room for all of it (the one already holding the most of the
   item wins, then the one with the least spare room). Otherwise, if splitting
   is allowed, the stock is spread over the locations with the most room first.
5. A location that is emptied never receives stock, and a location that
   receives stock is never emptied.
6. Open capacity starts at Fp Available. It goes down as moves are planned
   (and up when a location is emptied), so no location is ever overfilled.
