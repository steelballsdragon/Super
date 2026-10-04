# Stock consolidation

Finds items stored in more than one location and tells you how many to move
from which location to which, so locations can be emptied.

## Which file do I use?

Pick **one** of these. They give the same result.

### Option 1: the ready-made Excel file (easiest)

`Stock_Consolidation.xlsm`

1. Right-click the downloaded file → **Properties** → tick **Unblock** → OK
   (Windows blocks macros in downloaded files).
2. Open it and click **Enable Content** if Excel asks.
3. Go to the **Raw Data** sheet, click cell **A1** and paste your whole report,
   headers included (it replaces the header row that is already there).
4. Go to the **Start** sheet and click **Build Consolidation Plan**.

`Stock_Consolidation_Sample.xlsm` is the same file already filled with the rows
from the screenshot, if you want to see a finished result first.

### Option 2: paste the code into your own Excel file

`VBA_Code.txt`

1. Open your report in Excel.
2. Press **Alt + F11** (the code window opens).
3. Click **Insert → Module**.
4. Open `VBA_Code.txt` in Notepad, press **Ctrl + A**, **Ctrl + C**, then click
   in the empty white window in Excel and press **Ctrl + V**.
5. Close the code window, press **Alt + F8**, choose
   **BuildConsolidationPlan** and click **Run**.

To keep the macro in that file, save it as **Excel Macro-Enabled Workbook (*.xlsm)**.
The settings are the `DEFAULT_` lines near the top of the code.

### Option 3: run it straight in SQL Server (optional)

`consolidation_plan.sql` does the same thing without Excel. Change the one
`INSERT ... SELECT` in section 1 to read the table or view behind the report,
then run it in SQL Server Management Studio.

## What you get

| Sheet | What's on it |
|---|---|
| Clean Data | Your report with only the 6 columns below, after removing rows. Items in 2+ locations are highlighted. |
| Consolidation Plan | The moves: item, from location, qty to move, to location, open and max capacity of the target, location types. |
| Not Consolidated | Locations of multi-location items that can't be emptied, and why. |

Columns used from the report: **Prtnum** (item), **Stoloc** (location),
**Max of Curqvl** (current qty), **Max of Fp Available** (available capacity),
**Max of Maxqvl** (max qty), **Typcod** (location type). All other columns are
ignored.

Rows are removed when current qty, max qty or available capacity is 0 or
negative, and only locations with a max qty from 9 to 23 are kept.

## How the moves are chosen

1. Only items sitting in 2 or more locations are looked at.
2. The location with the smallest quantity is emptied first.
3. Stock only goes to locations that already hold the same item.
4. A location is only planned if it can be emptied completely: one move if a
   location has room for all of it (the one already holding the most of the
   item wins). Otherwise it is spread over the locations with the most room.
5. A location that is emptied never receives stock, and a location that
   receives stock is never emptied.
6. Open capacity starts at Fp Available and is updated after every planned
   move, so no location is overfilled.

(`generator/` holds the scripts that build the Excel file. You don't need it.)
