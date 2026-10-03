"""Sample data shared by the SQL demo block and the Excel workbook.

Both deliverables are generated from these lists so they always hold the
same rows, and therefore produce the same consolidation plan.
"""

# Default settings (the SQL script and the workbook start with these)
SAME_ZONE_ONLY = False
ALLOW_SPLIT = True
EXCLUDE_ZONES = "STAGE"

# (location, zone, max_capacity)  max_capacity None = unknown
LOCATIONS = (
    [(f"AOF{i}", "CONSOF", 15) for i in range(1, 13)]
    + [(f"ZA{i}", "ZA", 20) for i in range(180, 196)]
    + [(f"ZB{i}", "ZB", 40) for i in range(10, 21)]
    + [("BLK01", "BULK", 200), ("BLK02", "BULK", 200), ("BLK03", "BULK", 200)]
    + [("ZC01", "ZC", None), ("ZC02", "ZC", None), ("ZC03", "ZC", None)]
    + [("STG1", "STAGE", 999)]
)

# (item_number, location, quantity)  one row per stock record
INVENTORY = [
    # Same as the screenshot: 5 in ZA186 fit into AOF5 (6 of 15 used)
    ("5794400", "ZA186", 5),
    ("5794400", "AOF5", 6),
    # Three locations: the two small ones go into the fullest one
    ("5794411", "AOF1", 3),
    ("5794411", "AOF2", 8),
    ("5794411", "ZA181", 9),
    # Only one location outside staging, so nothing to do
    ("5794422", "STG1", 10),
    ("5794422", "AOF3", 4),
    # Two small lots fill AOF12; ZA184 is then left with nowhere to go
    ("5794433", "AOF10", 2),
    ("5794433", "AOF11", 3),
    ("5794433", "ZA184", 9),
    ("5794433", "AOF12", 10),
    # 14 units, no single location has room: split 10 + 4
    ("6100025", "ZA183", 14),
    ("6100025", "ZB12", 30),
    ("6100025", "ZB13", 34),
    # Two stock records in the same location are added together (7 + 2)
    ("6100031", "ZB17", 7),
    ("6100031", "ZB17", 2),
    ("6100031", "ZB18", 25),
    # Both nearly full: neither can be emptied
    ("7002210", "ZA190", 18),
    ("7002210", "ZA191", 19),
    # X99 is missing from the location master; it can still be emptied
    ("7300001", "X99", 4),
    ("7300001", "AOF9", 2),
    # BLK01 holds two items, so they share its free space
    ("8800100", "BLK01", 120),
    ("8800100", "ZB15", 15),
    ("8800200", "BLK01", 60),
    ("8800200", "ZB16", 12),
    ("8800200", "BLK02", 190),
    # ZC01 has no capacity on file, so ZC01 is emptied into ZA195 instead
    ("9100001", "ZA195", 3),
    ("9100001", "ZC01", 10),
    # Neither location has a capacity on file
    ("9100002", "ZC02", 5),
    ("9100002", "ZC03", 6),
    # Everyday cases
    ("4410078", "AOF4", 9),
    ("4410078", "AOF6", 2),
    ("4410078", "ZA187", 1),
    ("4410090", "ZB10", 22),
    ("4410090", "ZB11", 6),
    ("4410090", "ZA188", 4),
    ("4410105", "AOF7", 11),
    ("4410105", "AOF8", 3),
    ("3300512", "ZA182", 7),
    ("3300512", "ZA185", 6),
    ("3300512", "BLK03", 150),
    ("3300527", "ZB14", 40),
    ("3300527", "ZB19", 1),
    ("3300527", "ZB20", 38),
    ("5000001", "ZA189", 16),
    ("5000001", "ZA192", 2),
    ("5000001", "ZA193", 5),
    ("5000001", "ZA194", 15),
]
