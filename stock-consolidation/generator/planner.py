"""Python model of the cleaning and consolidation rules.

consolidation_plan.sql and the VBA module implement these same rules. The
build uses this model to pre-fill the workbook's result sheets for the sample
data, and the tests compare the SQL and VBA output with it.
"""

EPS = 1e-6
MAX_SEARCH = 10   # items in more locations than this use the quicker "cheapest first" rule

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
             "removed_ignored_type": 0, "rows_kept": 0, "capacity_capped": 0}
    merged = {}
    for item, loc, qty, avail, mx, typ in raw:
        item, loc, typ = _text(item), _text(loc), _text(typ)
        if not item or not loc:
            continue
        stats["rows_read"] += 1
        capped = False
        if qty is not None and mx is not None:
            if avail is None:
                avail = mx - qty
            elif avail > mx - qty:              # never more room than max - current
                avail = mx - qty
                capped = True
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
        if capped:
            stats["capacity_capped"] += 1
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
    for r in merged.values():                   # combined duplicates: still never more room than max - current
        r["avail"] = min(r["avail"], r["max"] - r["qty"])
    rows = sorted(merged.values(), key=lambda r: (r["item_key"], r["loc_key"]))
    stats["rows_kept"] = len(rows)
    return rows, stats


def plan(rows, same_type_only=False, allow_split=True):
    """Return (moves, not_moved, summary) for cleaned rows."""
    # Location level: capacity is shared by every item in the location
    locs = {}
    for r in rows:
        l = locs.setdefault(r["loc_key"], {"open": r["avail"], "max": r["max"], "type": r["type"]})
        l["open"] = min(l["open"], r["avail"])     # shared location: the smallest room wins
        l["max"] = max(l["max"], r["max"])
        l["type"] = max(l["type"], r["type"])

    work = [dict(r, cur=r["qty"], state=0, l=locs[r["loc_key"]]) for r in rows]
    items = {}
    for r in sorted(work, key=lambda r: (r["item_key"], r["qty"], r["loc_key"])):
        items.setdefault(r["item_key"], []).append(r)
    items = [g for g in items.values() if len(g) >= 2]

    moves, not_moved = [], []

    def free(r):
        return max(r["l"]["open"], 0.0)

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

    def allocate(empty, keep, split):
        """Plan where each location in `empty` (largest first) goes, without changing anything.
        Returns (steps, stays): steps = [(location, [(target, qty), ...])], stays = could not be placed."""
        keep = list(keep)
        room = {id(t): free(t) for t in keep}
        cur = {id(t): t["cur"] for t in keep}
        steps, stays = [], []
        for e in empty:
            need = e["qty"]
            fits = [t for t in keep if room[id(t)] >= need - EPS]
            if fits:                                    # one move: the tightest fit
                t = min(fits, key=lambda t: (room[id(t)], -cur[id(t)], t["loc_key"]))
                parts = [(t, need)]
            elif split:                                 # spread: most room first
                parts, rem = [], need
                for t in sorted(keep, key=lambda t: (-room[id(t)], -cur[id(t)], t["loc_key"])):
                    if rem <= EPS:
                        break
                    take = min(room[id(t)], rem)
                    if take > EPS:
                        parts.append((t, take))
                        rem -= take
            else:                                       # stays; its room can take smaller ones
                stays.append(e)
                keep.append(e)
                room[id(e)], cur[id(e)] = free(e), e["cur"]
                continue
            for t, take in parts:
                room[id(t)] -= take
                cur[id(t)] += take
            steps.append((e, parts))
        return steps, stays

    def largest_first(sub, idx):
        return [sub[i] for i in sorted(idx, key=lambda i: (-sub[i]["qty"], i))]

    def choose(sub):
        """Which locations of `sub` to empty: the most locations, then the fewest units moved."""
        k = len(sub)
        fr = [free(r) for r in sub]
        w = [r["qty"] + fr[i] for i, r in enumerate(sub)]   # emptying i needs its qty moved and loses its room
        budget = sum(fr)
        if k > MAX_SEARCH:                                  # too many to try all: cheapest first
            chosen, used = [], 0.0
            for i in sorted(range(k), key=lambda i: (w[i], sub[i]["qty"], i)):
                if len(chosen) < k - 1 and used + w[i] <= budget + EPS:
                    chosen.append(i)
                    used += w[i]
                else:
                    break
            return chosen
        best = None
        for m in range(1, (1 << k) - 1):
            idx = [i for i in range(k) if m >> i & 1]
            if sum(w[i] for i in idx) > budget + EPS:       # not enough room for these
                continue
            cnt, units = len(idx), sum(sub[i]["qty"] for i in idx)
            if best is not None and (cnt < best[0] or (cnt == best[0] and units >= best[1] - EPS)):
                continue
            if not allow_split:
                _, stays = allocate(largest_first(sub, idx), [sub[i] for i in range(k) if i not in idx], False)
                if stays:
                    continue
            best = (cnt, units, idx)
        return best[2] if best else []

    for g in items:
        subs = {}
        for r in g:
            subs.setdefault(r["l"]["type"].upper() if same_type_only else "", []).append(r)
        for key in sorted(subs):
            sub = subs[key]
            if len(sub) < 2:
                continue
            idx = choose(sub)
            steps, _ = allocate(largest_first(sub, idx), [sub[i] for i in range(len(sub)) if i not in idx],
                                allow_split)
            for e, parts in steps:
                for t, take in parts:
                    add_move(e, t, take)
                e["state"] = 1
                e["cur"] = 0.0
                e["l"]["open"] += e["qty"]
        remaining = [r for r in g if r["state"] != 1]
        if len(remaining) < 2:
            continue
        for r in g:                                       # locations that stay without receiving stock
            if r["state"] != 0:
                continue
            key = r["l"]["type"].upper() if same_type_only else ""
            mates = [x for x in subs[key] if x is not r and x["state"] != 1]
            if not mates:
                reason, room = REASON_TYPE, 0.0
            elif allow_split:
                reason, room = REASON_ROOM, sum(free(x) for x in mates)
            else:
                reason, room = REASON_SINGLE, max(free(x) for x in mates)
            not_moved.append({
                "item_number": r["item"], "location": r["loc"], "location_type": r["l"]["type"],
                "quantity": r["qty"], "room_elsewhere": room, "reason": reason,
            })

    summary = {
        "items_in_multiple_locations": len(items),
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
