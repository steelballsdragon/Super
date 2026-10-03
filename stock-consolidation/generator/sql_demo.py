"""Writes the sample data from demo_data.py into consolidation_plan.sql."""
import re
from pathlib import Path

import demo_data

SQL_FILE = Path(__file__).resolve().parent.parent / "consolidation_plan.sql"
START = "-- DEMO DATA START"
END = "-- DEMO DATA END"


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def _values(rows, indent="        "):
    return ",\n".join(indent + "(" + ", ".join(_lit(v) for v in r) + ")" for r in rows)


def demo_block(inventory=demo_data.INVENTORY, locations=demo_data.LOCATIONS):
    return (
        "    INSERT INTO #stock (item_number, location, quantity) VALUES\n"
        + _values(inventory) + ";\n"
        + "    INSERT INTO #locations (location, zone, max_capacity) VALUES\n"
        + _values(locations) + ";\n"
    )


def inject(sql_text, block):
    pattern = re.compile(r"(" + re.escape(START) + r"[^\n]*\n).*?(\s*" + re.escape(END) + ")", re.S)
    out, n = pattern.subn(lambda m: m.group(1) + block.rstrip("\n") + m.group(2), sql_text)
    if n != 1:
        raise RuntimeError("demo data markers not found in SQL file")
    return out


def write_sql():
    SQL_FILE.write_text(inject(SQL_FILE.read_text(), demo_block()), newline="\r\n")


if __name__ == "__main__":
    write_sql()
    print("updated", SQL_FILE)
