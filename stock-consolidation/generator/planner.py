"""Python model of the cleaning and consolidation rules.

consolidation_plan.sql and the VBA module implement these same rules. The
build uses this model to pre-fill the workbook's result sheets for the sample
data, and the tests compare the SQL and VBA output with it.
"""

EPS = 1e-6

REASON_TYPE = "No other location of this item with the same location type"
REASON_ROOM = "Not enough room in the item's other locations"
REASON_SINGLE = "No single location of this item has room for all of it"


def _text(v):
    return "" if v is None else str(v).strip(" ")


def clean(raw, max_location_qty=23, remove_no_capacity=True, exclude_types="", min_location_qty=9):
    """Filter and tidy the raw report.

    raw: iterable of (item, location, current_qty, available_capacity, max_qty, location_type)
    Returns (rows, stats); rows are dicts sorted by item, then location.
    """
    excluded = {t.strip(" ").upper() for t in exclude_types.split(",") if t.strip(" ")}
    stats = {"rows_read": 0, "removed_zero_or_negative": 0, "removed_max_qty_out_of_range": 0,
             "removed_ignored_type": 0, "rows_kept": 0}
    merged = {}
    for item, loc, qty, avail, mx, typ in raw:
        item, loc, typ = _text(item), _text(loc), _text(typ)
        if not item or not loc:
            continue
        stats["rows_read"] += 1
        if avail is None and qty is not None and mx is not None:
            avail = mx - qty
        if (qty is None or qty <= 0 or mx is None or mx <= 0
                or (remove_no_capacity and (avail is None or avail <= 0))):
            stats["removed_zero_or_negative"] += 1
            continue
        if mx > max_location_qty or mx < min_location_qty:
            stats["removed_max_qty_out_of_range"] += 1
            continue
        if typ.upper() in excluded:
            stats["removed_ignored_type"] += 1
            continue
        if avail is None:
            avail = 0.0
        key = (item.upper(), loc.upper())
        r = merged.get(key)
        if r is None:
            merged[key] = {"item": item, "loc": loc, "qty": float(qty), "avail": float(avail),
                           "max": float(mx), "type": typ, "item_key": key[0], "loc_key": key[1]}
        else:  # the same item and location twice: keep the largest values
            r["item"] = min(r["item"], item)
            r["loc"] = min(r["loc"], loc)
            r["qty"] = max(r["qty"], float(qty))
            r["avail"] = max(r["avail"], float(avail))
            r["max"] = max(r["max"], float(mx))
            r["type"] = max(r["type"], typ)
    rows = sorted(merged.values(), key=lambda r: (r["item_key"], r["loc_key"]))
    stats["rows_kept"] = len(rows)
    return rows, stats


def plan(rows, same_type_only=False, allow_split=True):
    """Return (moves, not_moved, summary) for cleaned rows."""
    # Location level: capacity is shared by every item in the location
    locs = {}
    for r in rows:
        l = locs.setdefault(r["loc_key"], {"open": r["avail"], "max": r["max"], "type": r["type"]})
        l["open"] = max(l["open"], r["avail"])
        l["max"] = max(l["max"], r["max"])
        l["type"] = max(l["type"], r["type"])

    work = [dict(r, cur=r["qty"], state=0, l=locs[r["loc_key"]]) for r in rows]
    groups = {}
    for r in sorted(work, key=lambda r: (r["item_key"], r["qty"], r["loc_key"])):
        groups.setdefault(r["item_key"], []).append(r)
    groups = [g for g in groups.values() if len(g) >= 2]

    moves, failures = [], []

    def add_move(s, t, q):
        before = t["l"]["open"]
        moves.append({
            "item_number": s["item"], "from_location": s["loc"], "quantity_to_move": q,
            "to_location": t["loc"], "target_open_capacity": before,
            "target_max_capacity": t["l"]["max"], "target_location_type": t["l"]["type"],
            "from_location_type": s["l"]["type"], "target_open_after_move": before - q,
        })
        t["l"]["open"] -= q
        t["cur"] += q
        t["state"] = 2

    for g in groups:
        for s in g:
            if s["state"] != 0:
                continue
            need = s["qty"]
            others = [t for t in g if t is not s and t["state"] != 1
                      and (not same_type_only or t["l"]["type"].upper() == s["l"]["type"].upper())]
            cand = [t for t in others if t["l"]["open"] > EPS]
            sum_open = sum(t["l"]["open"] for t in cand)
            max_open = max((t["l"]["open"] for t in cand), default=0.0)

            fits = [t for t in cand if t["l"]["open"] >= need - EPS]
            if fits:
                best = sorted(fits, key=lambda t: (-t["cur"], t["l"]["open"], t["loc_key"]))[0]
                add_move(s, best, need)
            elif allow_split and cand and sum_open >= need - EPS:
                remaining = need
                for t in sorted(cand, key=lambda t: (-t["l"]["open"], -t["cur"], t["loc_key"])):
                    if remaining <= EPS:
                        break
                    take = min(t["l"]["open"], remaining)
                    add_move(s, t, take)
                    remaining -= take
            else:
                if not others:
                    reason = REASON_TYPE
                elif allow_split:
                    reason = REASON_ROOM
                else:
                    reason = REASON_SINGLE
                failures.append((s, sum_open if allow_split else max_open, reason))
                continue
            s["state"] = 1
            s["cur"] = 0.0
            s["l"]["open"] += need

    not_moved = [{
        "item_number": s["item"], "location": s["loc"], "location_type": s["l"]["type"],
        "quantity": s["qty"], "room_elsewhere": room, "reason": reason,
    } for s, room, reason in failures if s["state"] == 0]

    summary = {
        "items_in_multiple_locations": len(groups),
        "locations_emptied": sum(1 for r in work if r["state"] == 1),
        "moves": len(moves),
        "units_to_move": sum(m["quantity_to_move"] for m in moves),
        "locations_not_emptied": len(not_moved),
    }
    return moves, not_moved, summary


def clean_rows_out(rows):
    """Cleaned rows in the column order of the Clean Data sheet / SQL result."""
    return [{"item_number": r["item"], "location": r["loc"], "current_qty": r["qty"],
             "available_capacity": r["avail"], "max_qty": r["max"], "location_type": r["type"]}
            for r in rows]


def run(raw, max_location_qty=23, remove_no_capacity=True, same_type_only=False,
        allow_split=True, exclude_types="", min_location_qty=9):
    rows, stats = clean(raw, max_location_qty, remove_no_capacity, exclude_types, min_location_qty)
    moves, not_moved, summary = plan(rows, same_type_only, allow_split)
    return clean_rows_out(rows), stats, moves, not_moved, summary


if __name__ == "__main__":
    import demo_data as d
    cl, st, mv, nm, sm = run(d.RAW, d.MAX_LOCATION_QTY, d.REMOVE_NO_CAPACITY, d.SAME_TYPE_ONLY,
                             d.ALLOW_SPLIT, d.EXCLUDE_TYPES, d.MIN_LOCATION_QTY)
    print(st)
    for m in mv:
        print(m)
    print()
    for n in nm:
        print(n)
    print(sm)
