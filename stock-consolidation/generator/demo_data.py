"""Sample data: the rows of the raw location report shown in the screenshot.

The SQL demo block and the workbook's Raw Data sheet are both generated from
these rows, so they produce the same consolidation plan.
"""

# Default settings (the SQL script and the workbook start with these)
MIN_LOCATION_QTY = 9         # only locations whose max qty is at least this
MAX_LOCATION_QTY = 23        # ... and at most this
REMOVE_NO_CAPACITY = True    # drop rows whose available capacity is 0 or negative
SAME_TYPE_ONLY = False       # only move between locations of the same type
ALLOW_SPLIT = True           # a location may be emptied into several locations
EXCLUDE_TYPES = ""           # comma-separated location types to ignore

RAW_HEADERS = ["Prtnum", "Stoloc", "Max of Curqvl", "Max of Fp Available", "Max of Maxqvl",
               "Sto Zone Cod", "Typcod", "Prt Velzon", "Default Ftpcod", "Sum of Comqty",
               "Sum of Pndqvl", "Max of Lochgt", "Max of Loclen"]

RAW_ROWS = [
    ("0100520", "EB141", 9, 2, 11, "KCPB", "KCP", "A", "05X12", 0, 0, 336, 245),
    ("0105202", "Q097", 5, 6, 11, "KCPA", "KCP", "C", "07X06", 0, 0, 260, 245),
    ("0108006", "EA111", 4, 7, 11, "KCPA", "KCP", "B", "05X06", 0, 0, 336, 245),
    ("0151003", "EC140", 6, 5, 11, "KCPB", "KCP", "C", "06X09", 0, 0, 336, 245),
    ("0170002", "F129", 4, 5, 9, "KCPB", "KCP", "B", "06X11", 0, 0, 260, 242),
    ("0170103", "F103", 10, 1, 11, "KCPA", "KCP", "C", "06X06", 0, 0, 260, 242),
    ("0180401", "EA132", 3, 6, 9, "KCPB", "KCP", "A", "08X10", 0, 0, 336, 245),
    ("0180401", "EA141", 7, 2, 9, "KCPB", "KCP", "A", "08X10", 0, 0, 336, 245),
    ("0180703", "EC137", 10, 1, 11, "KCPB", "KCP", "C", "08X10", 0, 0, 336, 245),
    ("0200000", "F108", 6, 5, 11, "KCPA", "KCP", "A", "05X12", 0, 0, 260, 242),
    ("0204601", "EB140", 9, 2, 11, "KCPB", "KCP", "C", "14X08", 0, 0, 336, 245),
    ("0314830", "G143", 4, 7, 11, "KCPC", "KCP", "C", "10X14", 0, 0, 260, 242),
    ("0390419", "T105", 3, 6, 9, "CONSA", "CONS", "C", "18X18", 0, 0, 260, 242),
    ("0390421", "Y183", 6, 3, 9, "CONSOF", "CONS", "C", "18X18", 0, 0, 260, 240),
    ("0390604", "R128", 4, 7, 11, "CONSB", "CONS", "C", "15X08", 0, 0, 260, 242),
    ("0390605", "ZA166", 3, 8, 11, "CONSOF", "CONS", "C", "15X08", 0, 0, 260, 242),
    ("0400703", "EB127", 9, 2, 11, "KCPA", "KCP", "A", "06X08", 0, 0, 336, 245),
    ("0400703", "G134", 1, 8, 9, "KCPC", "KCP", "A", "06X08", 0, 0, 260, 242),
]


def raw_fields(row):
    """(item, location, current qty, available capacity, max qty, location type) of a raw row."""
    return row[0], row[1], row[2], row[3], row[4], row[6]


RAW = [raw_fields(r) for r in RAW_ROWS]
