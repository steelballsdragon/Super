"""Python model of the consolidation rules.

consolidation_plan.sql and the VBA module implement these same rules. The
build uses this model to pre-fill the workbook's result sheets with the plan
for the sample data, and the tests compare the SQL and VBA output with it.
"""

EPS = 1e-6

REASON_ZONE = "No other location of this item in the same zone"
REASON_NO_CAP = "Other locations of this item have no max capacity set"
REASON_ROOM = "Not enough room in the item's other locations"
REASON_SINGLE = "No single location of this item has room for all of it"


def _clean(v):
    return "" if v is None else str(v).strip(" ")


def plan(inventory, locations, same_zone_only=False, allow_split=True, exclude_zones=""):
    """Return (moves, not_moved, summary).

    inventory: iterable of (item_number, location, quantity)
    locations: iterable of (location, zone, max_capacity or None)
    """
    excluded = {z.strip().upper() for z in exclude_zones.split(",") if z.strip()}

    # Location master; duplicates keep the largest zone text and capacity
    locs = {}
    for loc, zone, cap in locations:
        loc, zone = _clean(loc), _clean(zone)
        if not loc:
            continue
        key = loc.upper()
        cur = locs.setdefault(key, {"zone": zone, "cap": None, "total": 0.0})
        if zone > cur["zone"]:
            cur["zone"] = zone
        if cap is not None and (cur["cap"] is None or cap > cur["cap"]):
            cur["cap"] = float(cap)

    # On-hand stock summed per item per location
    rows = {}
    for item, loc, qty in inventory:
        item, loc = _clean(item), _clean(loc)
        if not item or not loc or qty is None:
            continue
        key = (item.upper(), loc.upper())
        r = rows.setdefault(key, {"item": item, "loc": loc, "qty": 0.0})
        r["item"] = min(r["item"], item)
        r["loc"] = min(r["loc"], loc)
        r["qty"] += float(qty)

    work = []
    for (ik, lk), r in rows.items():
        if r["qty"] <= EPS:
            continue
        m = locs.get(lk)
        if m:
            m["total"] += r["qty"]
        work.append({
            "item": r["item"], "item_key": ik, "loc": r["loc"], "loc_key": lk,
            "qty": r["qty"], "cur": r["qty"], "master": m,
            "eligible": not (m and m["zone"].upper() in excluded),
            "state": 0,  # 0 untouched, 1 emptied, 2 received stock
        })

    def open_cap(r):
        return r["master"]["cap"] - r["master"]["total"]

    def zone_key(r):
        return r["master"]["zone"].upper() if r["master"] else None

    eligible = sorted((r for r in work if r["eligible"]),
                      key=lambda r: (r["item_key"], r["qty"], r["loc_key"]))
    groups = {}
    for r in eligible:
        groups.setdefault(r["item_key"], []).append(r)
    groups = [g for g in groups.values() if len(g) >= 2]

    moves, failures = [], []

    def add_move(s, t, q):
        before = open_cap(t)
        moves.append({
            "item_number": s["item"], "from_location": s["loc"], "quantity_to_move": q,
            "to_location": t["loc"], "target_open_capacity": before,
            "target_max_capacity": t["master"]["cap"], "target_zone": t["master"]["zone"],
            "from_zone": s["master"]["zone"] if s["master"] else None,
            "target_open_after_move": before - q,
        })
        t["master"]["total"] += q
        t["cur"] += q
        t["state"] = 2

    for g in groups:
        for s in g:
            if s["state"] != 0:
                continue
            need = s["qty"]
            others = [t for t in g if t is not s and t["state"] != 1
                      and (not same_zone_only or (zone_key(s) is not None and zone_key(t) == zone_key(s)))]
            with_cap = [t for t in others if t["master"] and t["master"]["cap"] is not None]
            cand = [t for t in with_cap if open_cap(t) > EPS]
            sum_open = sum(open_cap(t) for t in cand)
            max_open = max((open_cap(t) for t in cand), default=0.0)

            fits = [t for t in cand if open_cap(t) >= need - EPS]
            if fits:
                best = sorted(fits, key=lambda t: (-t["cur"], open_cap(t), t["loc_key"]))[0]
                add_move(s, best, need)
            elif allow_split and cand and sum_open >= need - EPS:
                remaining = need
                for t in sorted(cand, key=lambda t: (-open_cap(t), -t["cur"], t["loc_key"])):
                    if remaining <= EPS:
                        break
                    take = min(open_cap(t), remaining)
                    add_move(s, t, take)
                    remaining -= take
            else:
                if not others:
                    reason = REASON_ZONE
                elif not with_cap:
                    reason = REASON_NO_CAP
                elif allow_split:
                    reason = REASON_ROOM
                else:
                    reason = REASON_SINGLE
                failures.append((s, sum_open if allow_split else max_open, reason))
                continue
            s["state"] = 1
            s["cur"] = 0.0
            if s["master"]:
                s["master"]["total"] -= need

    not_moved = [{
        "item_number": s["item"], "location": s["loc"],
        "zone": s["master"]["zone"] if s["master"] else None,
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


if __name__ == "__main__":
    import demo_data as d
    mv, nm, sm = plan(d.INVENTORY, d.LOCATIONS, d.SAME_ZONE_ONLY, d.ALLOW_SPLIT, d.EXCLUDE_ZONES)
    for m in mv:
        print(m)
    print()
    for n in nm:
        print(n)
    print(sm)
