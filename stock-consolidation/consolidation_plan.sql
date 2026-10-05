/*==============================================================================
  STOCK CONSOLIDATION PLAN  (SQL Server 2012 or later)

  Reads the location report (Prtnum, Stoloc, Curqvl, Fp Available, Maxqvl,
  Typcod), cleans it, finds items stored in more than one location and works
  out how many units to move from which location to which, so that locations
  can be emptied.

  Result 1 - one row per move:
      item_number, from_location, quantity_to_move, to_location,
      target_open_capacity, target_max_capacity, target_location_type,
      from_location_type, target_open_after_move
  Result 2 - locations of multi-location items that could not be emptied
  Result 3 - summary (including how many report rows were removed and why)
  Result 4 - the cleaned data (when @ShowCleanData = 1)

  Cleaning:
    - only Prtnum, Stoloc, Curqvl, Fp Available, Maxqvl and Typcod are used;
    - rows with 0 or a negative current qty, max qty or available capacity are
      removed (available capacity only while @RemoveNoCapacity = 1);
    - only locations whose max qty is from @MinLocationQty (9) to
      @MaxLocationQty (23) are kept;
    - the same item and location listed twice is kept once (largest values);
    - available capacity is the smaller of Fp Available and Maxqvl - Curqvl,
      so no location is ever planned past its max.

  Planning (Stock_Consolidation.xlsm applies exactly the same rules):
    1. Only items sitting in 2 or more locations are looked at, and stock
       only goes to locations that already hold the same item.
    2. For each item every combination of its locations is tried (items in
       more than 10 locations use a quicker rule), and the plan that empties
       the MOST locations is chosen; between equal plans, the one that moves
       the FEWEST units.
    3. A location is only emptied completely. With @AllowSplit = 1 it may be
       spread over several locations; with 0 it must fit whole into one.
    4. Each emptied location (largest first) goes to the kept location it
       fits most tightly; if none can take it all, to the ones with most room.
    5. A location that is emptied never receives stock, and a location that
       receives stock is never emptied.
    6. Open capacity starts at Fp Available and goes down as moves are
       planned (and up when a location is emptied), shared by all items in
       the location.

  HOW TO USE
    1. Edit the INSERT in section 1 so it reads your own table or view.
    2. Run. (Set @UseDemoData = 1 to try it on the built-in sample first.)
==============================================================================*/
SET NOCOUNT ON;

--------------------------------------------------------------------------------
-- SETTINGS
--------------------------------------------------------------------------------
DECLARE @MinLocationQty   decimal(18,4) = 9;    -- only locations whose max qty is at least this
DECLARE @MaxLocationQty   decimal(18,4) = 23;   -- ... and at most this
DECLARE @RemoveNoCapacity bit           = 1;    -- 1 = also remove rows with 0 / negative available capacity
DECLARE @SameTypeOnly     bit           = 0;    -- 1 = only move between locations of the same type (Typcod)
DECLARE @AllowSplit       bit           = 1;    -- 1 = a location may be emptied into several locations
DECLARE @ExcludeTypes     varchar(4000) = '';   -- comma-separated location types to ignore, e.g. 'CONS'
DECLARE @UseDemoData      bit           = 0;    -- 1 = ignore your table and use the sample data below
DECLARE @ShowCleanData    bit           = 1;    -- 1 = also return the cleaned data (result 4)

--------------------------------------------------------------------------------
-- 1. INPUT
--------------------------------------------------------------------------------
IF OBJECT_ID('tempdb..#raw') IS NOT NULL DROP TABLE #raw;

CREATE TABLE #raw (
    prtnum       varchar(100)  NULL,   -- item number
    stoloc       varchar(100)  NULL,   -- location
    curqvl       decimal(18,4) NULL,   -- current quantity
    fp_available decimal(18,4) NULL,   -- available capacity (NULL = maxqvl - curqvl)
    maxqvl       decimal(18,4) NULL,   -- max quantity the location can hold
    typcod       varchar(100)  NULL    -- location type
);

IF @UseDemoData = 0
BEGIN
    /* >>>>>>>>>>>>>>>>>> CHANGE THIS QUERY TO MATCH YOUR TABLE OR VIEW <<<<<<<<<<<<<<<<<< */
    INSERT INTO #raw (prtnum, stoloc, curqvl, fp_available, maxqvl, typcod)
    SELECT  r.prtnum,
            r.stoloc,
            r.curqvl,
            r.fp_available,
            r.maxqvl,
            r.typcod
    FROM    dbo.location_report AS r
 -- WHERE   r.wh_id = 'WH1'                 -- one warehouse at a time
    ;
    /* >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>><<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<< */
END
ELSE
BEGIN
    -- DEMO DATA START (generated from generator/demo_data.py - same rows as the workbook)
    INSERT INTO #raw (prtnum, stoloc, curqvl, fp_available, maxqvl, typcod) VALUES
        ('0100520', 'EB141', 9, 2, 11, 'KCP'),
        ('0105202', 'Q097', 5, 6, 11, 'KCP'),
        ('0108006', 'EA111', 4, 7, 11, 'KCP'),
        ('0151003', 'EC140', 6, 5, 11, 'KCP'),
        ('0170002', 'F129', 4, 5, 9, 'KCP'),
        ('0170103', 'F103', 10, 1, 11, 'KCP'),
        ('0180401', 'EA132', 3, 6, 9, 'KCP'),
        ('0180401', 'EA141', 7, 2, 9, 'KCP'),
        ('0180703', 'EC137', 10, 1, 11, 'KCP'),
        ('0200000', 'F108', 6, 5, 11, 'KCP'),
        ('0204601', 'EB140', 9, 2, 11, 'KCP'),
        ('0314830', 'G143', 4, 7, 11, 'KCP'),
        ('0390419', 'T105', 3, 6, 9, 'CONS'),
        ('0390421', 'Y183', 6, 3, 9, 'CONS'),
        ('0390604', 'R128', 4, 7, 11, 'CONS'),
        ('0390605', 'ZA166', 3, 8, 11, 'CONS'),
        ('0400703', 'EB127', 9, 2, 11, 'KCP'),
        ('0400703', 'G134', 1, 8, 9, 'KCP');    -- DEMO DATA END
END;

--------------------------------------------------------------------------------
-- 2. CLEAN
--------------------------------------------------------------------------------
IF OBJECT_ID('tempdb..#tagged')    IS NOT NULL DROP TABLE #tagged;
IF OBJECT_ID('tempdb..#locstate')  IS NOT NULL DROP TABLE #locstate;
IF OBJECT_ID('tempdb..#work')      IS NOT NULL DROP TABLE #work;
IF OBJECT_ID('tempdb..#moves')     IS NOT NULL DROP TABLE #moves;
IF OBJECT_ID('tempdb..#not_moved') IS NOT NULL DROP TABLE #not_moved;

-- Ignored location types
DECLARE @ExcludedType TABLE (type_key varchar(100) COLLATE Latin1_General_BIN2 PRIMARY KEY);
DECLARE @rest varchar(4001) = ISNULL(@ExcludeTypes, '') + ',', @comma int, @t varchar(4000);
SET @comma = CHARINDEX(',', @rest);
WHILE @comma > 0
BEGIN
    SET @t = UPPER(LTRIM(RTRIM(LEFT(@rest, @comma - 1))));
    IF @t <> '' AND NOT EXISTS (SELECT 1 FROM @ExcludedType WHERE type_key = @t)
        INSERT INTO @ExcludedType (type_key) VALUES (@t);
    SET @rest  = SUBSTRING(@rest, @comma + 1, 4001);
    SET @comma = CHARINDEX(',', @rest);
END;

-- Every report row with the reason it is dropped (NULL = kept).
-- Keys are trimmed and upper-cased, and compared/sorted byte by byte, so the
-- result is the same whatever the server collation (and the same as Excel).
SELECT  UPPER(x.item) COLLATE Latin1_General_BIN2 AS item_key,
        UPPER(x.loc)  COLLATE Latin1_General_BIN2 AS loc_key,
        x.item COLLATE Latin1_General_BIN2         AS item_number,
        x.loc  COLLATE Latin1_General_BIN2         AS location,
        x.curqvl,
        x.avail,
        x.maxqvl,
        x.typ COLLATE Latin1_General_BIN2          AS location_type,
        x.capped,
        CASE WHEN x.curqvl IS NULL OR x.curqvl <= 0
               OR x.maxqvl IS NULL OR x.maxqvl <= 0
               OR (@RemoveNoCapacity = 1 AND (x.avail IS NULL OR x.avail <= 0)) THEN 1
             WHEN x.maxqvl > @MaxLocationQty OR x.maxqvl < @MinLocationQty      THEN 2
             WHEN EXISTS (SELECT 1 FROM @ExcludedType AS e
                          WHERE e.type_key = UPPER(x.typ) COLLATE Latin1_General_BIN2) THEN 3
        END AS dropped
INTO    #tagged
FROM   (SELECT LTRIM(RTRIM(prtnum))               AS item,
               LTRIM(RTRIM(stoloc))               AS loc,
               LTRIM(RTRIM(ISNULL(typcod, '')))   AS typ,
               curqvl,
               maxqvl,
               -- never more room than max - current, whatever Fp Available says
               CASE WHEN fp_available IS NULL OR fp_available > maxqvl - curqvl
                    THEN maxqvl - curqvl ELSE fp_available END             AS avail,
               CASE WHEN fp_available > maxqvl - curqvl THEN 1 ELSE 0 END  AS capped
        FROM   #raw) AS x
WHERE   ISNULL(x.item, '') <> '' AND ISNULL(x.loc, '') <> '';

CREATE TABLE #work (
    row_id        int IDENTITY(1,1) PRIMARY KEY,
    item_key      varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
    loc_key       varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
    item_number   varchar(100) NOT NULL,
    location      varchar(100) NOT NULL,
    current_qty   decimal(18,4) NOT NULL,
    available     decimal(18,4) NOT NULL,
    max_qty       decimal(18,4) NOT NULL,
    location_type varchar(100) NOT NULL,
    cur_qty       decimal(18,4) NOT NULL,          -- quantity as moves are planned
    state         tinyint NOT NULL DEFAULT 0,      -- 0 untouched, 1 emptied, 2 received stock
    item_seq      int NULL,                        -- items in 2+ locations, in order
    sub_key       varchar(100) COLLATE Latin1_General_BIN2 NULL,   -- location type when @SameTypeOnly = 1
    grp           int NULL,                        -- item (and type) group, in order of work
    idx           int NULL                         -- position in the group: smallest qty first
);

-- Kept rows; the same item and location twice keeps the largest values
INSERT INTO #work (item_key, loc_key, item_number, location, current_qty, available, max_qty,
                   location_type, cur_qty)
SELECT  item_key, loc_key, MIN(item_number), MIN(location), MAX(curqvl),
        CASE WHEN MAX(ISNULL(avail, 0)) > MAX(maxqvl) - MAX(curqvl)      -- never more room than max - current
             THEN MAX(maxqvl) - MAX(curqvl) ELSE MAX(ISNULL(avail, 0)) END,
        MAX(maxqvl), MAX(location_type), MAX(curqvl)
FROM    #tagged
WHERE   dropped IS NULL
GROUP BY item_key, loc_key
ORDER BY item_key, loc_key;

-- Location level: capacity is shared by every item in the location
CREATE TABLE #locstate (
    loc_key       varchar(100) COLLATE Latin1_General_BIN2 NOT NULL PRIMARY KEY,
    location_type varchar(100) NOT NULL,
    type_key      varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
    max_qty       decimal(18,4) NOT NULL,
    open_cap      decimal(18,4) NOT NULL
);
INSERT INTO #locstate (loc_key, location_type, type_key, max_qty, open_cap)
SELECT  loc_key, MAX(location_type), UPPER(MAX(location_type)), MAX(max_qty), MIN(available)
FROM    #work
GROUP BY loc_key;

-- Order of work: items in 2+ locations; within an item (and location type when
-- @SameTypeOnly = 1) the locations are numbered smallest quantity first
UPDATE w
SET    sub_key = CASE WHEN @SameTypeOnly = 1 THEN l.type_key ELSE '' END
FROM   #work AS w
JOIN   #locstate AS l ON l.loc_key = w.loc_key;

UPDATE w
SET    item_seq = o.item_seq, grp = o.grp, idx = o.idx
FROM   #work AS w
JOIN  (SELECT row_id,
              DENSE_RANK() OVER (ORDER BY item_key)                                   AS item_seq,
              DENSE_RANK() OVER (ORDER BY item_key, sub_key)                          AS grp,
              ROW_NUMBER() OVER (PARTITION BY item_key, sub_key
                                 ORDER BY current_qty, loc_key) - 1                   AS idx
       FROM   #work
       WHERE  item_key IN (SELECT item_key FROM #work GROUP BY item_key HAVING COUNT(*) >= 2)) AS o
       ON o.row_id = w.row_id;

CREATE INDEX ix_work_item ON #work (item_key) INCLUDE (state, loc_key, cur_qty);
CREATE INDEX ix_work_grp  ON #work (grp) INCLUDE (idx, loc_key, current_qty, cur_qty, state);

--------------------------------------------------------------------------------
-- 3. PLAN THE MOVES
--------------------------------------------------------------------------------
CREATE TABLE #moves (
    move_seq               int IDENTITY(1,1) PRIMARY KEY,
    item_number            varchar(100),
    from_location          varchar(100),
    quantity_to_move       decimal(18,4),
    to_location            varchar(100),
    target_open_capacity   decimal(18,4),
    target_max_capacity    decimal(18,4),
    target_location_type   varchar(100),
    from_location_type     varchar(100),
    target_open_after_move decimal(18,4)
);

CREATE TABLE #not_moved (
    seq            int IDENTITY(1,1) PRIMARY KEY,
    row_id         int NOT NULL,
    room_elsewhere decimal(18,4) NOT NULL,
    reason         varchar(100) NOT NULL
);

-- Every combination of up to 10 locations, as a bit mask (bit i = location idx i)
DECLARE @MaxSearch int = 10;
IF OBJECT_ID('tempdb..#masks') IS NOT NULL DROP TABLE #masks;
CREATE TABLE #masks (m int PRIMARY KEY);
WITH n AS (SELECT 1 AS m UNION ALL SELECT m + 1 FROM n WHERE m < 1022)
INSERT INTO #masks (m) SELECT m FROM n OPTION (MAXRECURSION 1100);

DECLARE @grp    TABLE (idx int PRIMARY KEY, row_id int NOT NULL,
                       loc_key varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
                       qty decimal(18,4) NOT NULL, cur decimal(18,4) NOT NULL,
                       room decimal(18,4) NOT NULL, w decimal(18,4) NOT NULL);
DECLARE @chosen TABLE (idx int PRIMARY KEY);
DECLARE @cands  TABLE (ord int PRIMARY KEY, m int NOT NULL);
DECLARE @keep   TABLE (idx int PRIMARY KEY, row_id int NOT NULL,
                       loc_key varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
                       room decimal(18,4) NOT NULL, cur decimal(18,4) NOT NULL);
DECLARE @alloc  TABLE (seq int PRIMARY KEY, idx int NOT NULL, take decimal(18,4) NOT NULL);

DECLARE @grp_no int = 1, @groups int = ISNULL((SELECT MAX(grp) FROM #work), 0),
        @k int, @budget decimal(18,4), @best int, @c int, @nc int, @ok bit,
        @e int, @e_row int, @need decimal(18,4), @mask_try int, @tgt int, @item_seq int;

WHILE @grp_no <= @groups
BEGIN
    DELETE FROM @grp;
    INSERT INTO @grp (idx, row_id, loc_key, qty, cur, room, w)
    SELECT w.idx, w.row_id, w.loc_key, w.current_qty, w.cur_qty,
           CASE WHEN l.open_cap > 0 THEN l.open_cap ELSE 0 END,
           w.current_qty + CASE WHEN l.open_cap > 0 THEN l.open_cap ELSE 0 END
    FROM   #work AS w
    JOIN   #locstate AS l ON l.loc_key = w.loc_key
    WHERE  w.grp = @grp_no;
    SET @k = (SELECT COUNT(*) FROM @grp);

    IF @k >= 2
    BEGIN
        -- Emptying a location needs its qty moved and loses its room, so it "costs"
        -- qty + room; together they must fit in the room of all the item's locations.
        SET @budget = (SELECT SUM(room) FROM @grp);
        DELETE FROM @chosen;

        IF @k > @MaxSearch
            -- too many to try every combination: cheapest first while they fit
            INSERT INTO @chosen (idx)
            SELECT idx
            FROM  (SELECT idx,
                          ROW_NUMBER() OVER (ORDER BY w, qty, idx)                        AS rn,
                          SUM(w) OVER (ORDER BY w, qty, idx ROWS UNBOUNDED PRECEDING)     AS cum
                   FROM   @grp) AS x
            WHERE  rn <= @k - 1 AND cum <= @budget;
        ELSE
        BEGIN
            -- combinations that fit, best first: most locations, fewest units, lowest mask
            DELETE FROM @cands;
            INSERT INTO @cands (ord, m)
            SELECT ROW_NUMBER() OVER (ORDER BY e.cnt DESC, e.units ASC, mk.m ASC), mk.m
            FROM   #masks AS mk
            CROSS APPLY (SELECT COUNT(*) AS cnt, SUM(g.qty) AS units, SUM(g.w) AS sw
                         FROM   @grp AS g
                         WHERE  (mk.m & POWER(2, g.idx)) <> 0) AS e
            WHERE  mk.m <= POWER(2, @k) - 2
              AND  e.sw <= @budget;

            SET @best = NULL;
            IF @AllowSplit = 1
                SELECT TOP (1) @best = m FROM @cands ORDER BY ord;
            ELSE
            BEGIN
                -- each emptied location must fit whole into one kept location
                SET @c = 1;
                SET @nc = (SELECT COUNT(*) FROM @cands);
                WHILE @best IS NULL AND @c <= @nc
                BEGIN
                    SELECT @mask_try = m FROM @cands WHERE ord = @c;
                    DELETE FROM @keep;
                    INSERT INTO @keep (idx, row_id, loc_key, room, cur)
                    SELECT idx, row_id, loc_key, room, cur FROM @grp WHERE (@mask_try & POWER(2, idx)) = 0;
                    SET @ok = 1;
                    SET @e = (SELECT TOP (1) idx FROM @grp WHERE (@mask_try & POWER(2, idx)) <> 0
                              ORDER BY qty DESC, idx ASC);
                    WHILE @e IS NOT NULL AND @ok = 1
                    BEGIN
                        SET @need = (SELECT qty FROM @grp WHERE idx = @e);
                        SET @tgt = NULL;
                        SELECT TOP (1) @tgt = idx FROM @keep WHERE room >= @need
                        ORDER BY room ASC, cur DESC, loc_key ASC;
                        IF @tgt IS NULL
                            SET @ok = 0;
                        ELSE
                            UPDATE @keep SET room = room - @need, cur = cur + @need WHERE idx = @tgt;
                        SET @e = (SELECT TOP (1) idx FROM @grp
                                  WHERE (@mask_try & POWER(2, idx)) <> 0
                                    AND (qty < @need OR (qty = @need AND idx > @e))
                                  ORDER BY qty DESC, idx ASC);
                    END;
                    SET @best = CASE WHEN @ok = 1 THEN @mask_try END;
                    SET @c += 1;
                END;
            END;

            IF @best IS NOT NULL
                INSERT INTO @chosen (idx) SELECT idx FROM @grp WHERE (@best & POWER(2, idx)) <> 0;
        END;

        -- Move the chosen locations, largest first
        DELETE FROM @keep;
        INSERT INTO @keep (idx, row_id, loc_key, room, cur)
        SELECT idx, row_id, loc_key, room, cur FROM @grp WHERE idx NOT IN (SELECT idx FROM @chosen);

        SET @e = (SELECT TOP (1) g.idx FROM @grp AS g JOIN @chosen AS c ON c.idx = g.idx
                  ORDER BY g.qty DESC, g.idx ASC);
        WHILE @e IS NOT NULL
        BEGIN
            SELECT @need = qty, @e_row = row_id FROM @grp WHERE idx = @e;
            DELETE FROM @alloc;

            SET @mask_try = NULL;
            SELECT TOP (1) @mask_try = idx FROM @keep WHERE room >= @need ORDER BY room ASC, cur DESC, loc_key ASC;
            IF @mask_try IS NOT NULL
                INSERT INTO @alloc (seq, idx, take) VALUES (1, @mask_try, @need);     -- one move: tightest fit
            ELSE IF @AllowSplit = 1
                INSERT INTO @alloc (seq, idx, take)                             -- spread: most room first
                SELECT seq, idx, CASE WHEN prev_room + room <= @need THEN room ELSE @need - prev_room END
                FROM  (SELECT idx, room,
                              ROW_NUMBER() OVER (ORDER BY room DESC, cur DESC, loc_key ASC) AS seq,
                              ISNULL(SUM(room) OVER (ORDER BY room DESC, cur DESC, loc_key ASC
                                                     ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS prev_room
                       FROM   @keep
                       WHERE  room > 0) AS x
                WHERE  prev_room < @need;
            ELSE
                -- cannot be placed whole: it stays, and its room can take smaller ones
                INSERT INTO @keep (idx, row_id, loc_key, room, cur)
                SELECT idx, row_id, loc_key, room, cur FROM @grp WHERE idx = @e;

            IF EXISTS (SELECT 1 FROM @alloc)
            BEGIN
                INSERT INTO #moves (item_number, from_location, quantity_to_move, to_location,
                                    target_open_capacity, target_max_capacity, target_location_type,
                                    from_location_type, target_open_after_move)
                SELECT s.item_number, s.location, a.take, t.location,
                       l.open_cap, l.max_qty, l.location_type,
                       sl.location_type, l.open_cap - a.take
                FROM   @alloc AS a
                JOIN   @keep     AS k  ON k.idx = a.idx
                JOIN   #work     AS t  ON t.row_id = k.row_id
                JOIN   #locstate AS l  ON l.loc_key = t.loc_key
                JOIN   #work     AS s  ON s.row_id = @e_row
                JOIN   #locstate AS sl ON sl.loc_key = s.loc_key
                ORDER BY a.seq;

                UPDATE l SET open_cap = l.open_cap - a.take
                FROM   #locstate AS l JOIN @keep AS k ON k.loc_key = l.loc_key JOIN @alloc AS a ON a.idx = k.idx;

                UPDATE t SET cur_qty = t.cur_qty + a.take, state = 2
                FROM   #work AS t JOIN @keep AS k ON k.row_id = t.row_id JOIN @alloc AS a ON a.idx = k.idx;

                UPDATE k SET room = k.room - a.take, cur = k.cur + a.take
                FROM   @keep AS k JOIN @alloc AS a ON a.idx = k.idx;

                UPDATE #work     SET cur_qty = 0, state = 1 WHERE row_id = @e_row;
                UPDATE #locstate SET open_cap = open_cap + @need
                WHERE  loc_key = (SELECT loc_key FROM #work WHERE row_id = @e_row);
            END;

            SET @e = (SELECT TOP (1) g.idx FROM @grp AS g JOIN @chosen AS c ON c.idx = g.idx
                      WHERE g.qty < @need OR (g.qty = @need AND g.idx > @e)
                      ORDER BY g.qty DESC, g.idx ASC);
        END;
    END;

    -- After the item's last group: list its locations that stay without receiving stock
    SET @item_seq = (SELECT MAX(item_seq) FROM #work WHERE grp = @grp_no);
    IF NOT EXISTS (SELECT 1 FROM #work WHERE grp = @grp_no + 1 AND item_seq = @item_seq)
       AND (SELECT COUNT(*) FROM #work WHERE item_seq = @item_seq AND state <> 1) >= 2
        INSERT INTO #not_moved (row_id, room_elsewhere, reason)
        SELECT r.row_id,
               CASE WHEN m.cnt = 0 THEN 0 WHEN @AllowSplit = 1 THEN m.sum_room ELSE m.max_room END,
               CASE WHEN m.cnt = 0    THEN 'No other location of this item with the same location type'
                    WHEN @AllowSplit = 1 THEN 'Not enough room in the item''s other locations'
                    ELSE                      'No single location of this item has room for all of it'
               END
        FROM   #work AS r
        CROSS APPLY (SELECT COUNT(*) AS cnt,
                            ISNULL(SUM(CASE WHEN l.open_cap > 0 THEN l.open_cap ELSE 0 END), 0) AS sum_room,
                            ISNULL(MAX(CASE WHEN l.open_cap > 0 THEN l.open_cap ELSE 0 END), 0) AS max_room
                     FROM   #work AS x
                     JOIN   #locstate AS l ON l.loc_key = x.loc_key
                     WHERE  x.item_seq = r.item_seq AND x.sub_key = r.sub_key
                       AND  x.row_id <> r.row_id AND x.state <> 1) AS m
        WHERE  r.item_seq = @item_seq AND r.state = 0
        ORDER BY r.current_qty, r.loc_key;

    SET @grp_no += 1;
END;

--------------------------------------------------------------------------------
-- 4. RESULTS
--------------------------------------------------------------------------------
-- 1: Moves
SELECT  item_number,
        from_location,
        quantity_to_move,
        to_location,
        target_open_capacity,
        target_max_capacity,
        target_location_type,
        from_location_type,
        target_open_after_move
FROM    #moves
ORDER BY move_seq;

-- 2: Locations that stay (could not be emptied)
SELECT  w.item_number,
        w.location,
        l.location_type,
        w.current_qty AS quantity,
        n.room_elsewhere,
        n.reason
FROM    #not_moved AS n
JOIN    #work      AS w ON w.row_id  = n.row_id
JOIN    #locstate  AS l ON l.loc_key = w.loc_key
WHERE   w.state = 0
ORDER BY n.seq;

-- 3: Summary
SELECT  (SELECT COUNT(*) FROM #tagged)                                                AS rows_read,
        (SELECT COUNT(*) FROM #tagged WHERE dropped = 1)                              AS removed_zero_or_negative,
        (SELECT COUNT(*) FROM #tagged WHERE dropped = 2)                              AS removed_max_qty_out_of_range,
        (SELECT COUNT(*) FROM #tagged WHERE dropped = 3)                              AS removed_ignored_type,
        (SELECT COUNT(*) FROM #work)                                                  AS rows_kept,
        (SELECT COUNT(*) FROM #tagged WHERE dropped IS NULL AND capped = 1)           AS capacity_capped,
        (SELECT COUNT(DISTINCT item_key) FROM #work WHERE grp IS NOT NULL)            AS items_in_multiple_locations,
        (SELECT COUNT(*) FROM #work WHERE state = 1)                                  AS locations_emptied,
        (SELECT COUNT(*) FROM #moves)                                                 AS moves,
        (SELECT ISNULL(SUM(quantity_to_move), 0) FROM #moves)                         AS units_to_move,
        (SELECT COUNT(*) FROM #not_moved AS n JOIN #work AS w ON w.row_id = n.row_id
         WHERE w.state = 0)                                                           AS locations_not_emptied;

-- 4: Cleaned data
IF @ShowCleanData = 1
    SELECT  item_number,
            location,
            current_qty,
            available     AS available_capacity,
            max_qty,
            location_type
    FROM    #work
    ORDER BY item_key, loc_key;
