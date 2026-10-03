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
    - locations whose max qty is over @MaxLocationQty (23) are removed;
    - the same item and location listed twice is kept once (largest values).

  Planning (Stock_Consolidation.xlsm applies exactly the same rules):
    1. Only items sitting in 2 or more locations are looked at.
    2. Locations are emptied smallest quantity first.
    3. Stock only goes to locations that already hold the same item.
    4. A location is only planned if it can be emptied completely:
         - one move if any location has room for all of it (the one holding
           the most of the item wins, then the one with least spare room);
         - otherwise, if @AllowSplit = 1, it is spread over the locations
           with the most room first.
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
DECLARE @MaxLocationQty   decimal(18,4) = 23;   -- only locations whose max qty is at most this
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
        CASE WHEN x.curqvl IS NULL OR x.curqvl <= 0
               OR x.maxqvl IS NULL OR x.maxqvl <= 0
               OR (@RemoveNoCapacity = 1 AND (x.avail IS NULL OR x.avail <= 0)) THEN 1
             WHEN x.maxqvl > @MaxLocationQty                                    THEN 2
             WHEN EXISTS (SELECT 1 FROM @ExcludedType AS e
                          WHERE e.type_key = UPPER(x.typ) COLLATE Latin1_General_BIN2) THEN 3
        END AS dropped
INTO    #tagged
FROM   (SELECT LTRIM(RTRIM(prtnum))               AS item,
               LTRIM(RTRIM(stoloc))               AS loc,
               LTRIM(RTRIM(ISNULL(typcod, '')))   AS typ,
               curqvl,
               maxqvl,
               ISNULL(fp_available, maxqvl - curqvl) AS avail
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
    src_order     int NULL                         -- order in which locations are tried
);

-- Kept rows; the same item and location twice keeps the largest values
INSERT INTO #work (item_key, loc_key, item_number, location, current_qty, available, max_qty,
                   location_type, cur_qty)
SELECT  item_key, loc_key, MIN(item_number), MIN(location), MAX(curqvl), MAX(ISNULL(avail, 0)),
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
SELECT  loc_key, MAX(location_type), UPPER(MAX(location_type)), MAX(max_qty), MAX(available)
FROM    #work
GROUP BY loc_key;

-- Order of work: by item, then smallest quantity first
UPDATE w
SET    src_order = o.rn
FROM   #work AS w
JOIN  (SELECT row_id, ROW_NUMBER() OVER (ORDER BY item_key, current_qty, loc_key) AS rn
       FROM   #work
       WHERE  item_key IN (SELECT item_key FROM #work GROUP BY item_key HAVING COUNT(*) >= 2)) AS o
       ON o.row_id = w.row_id;

CREATE INDEX ix_work_item  ON #work (item_key) INCLUDE (state, loc_key, cur_qty);
CREATE INDEX ix_work_order ON #work (src_order);

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

DECLARE @cand  TABLE (row_id int PRIMARY KEY,
                      loc_key varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
                      cur_qty decimal(18,4) NOT NULL,
                      open_cap decimal(18,4) NOT NULL);
DECLARE @alloc TABLE (seq int PRIMARY KEY, row_id int NOT NULL, take decimal(18,4) NOT NULL);

DECLARE @i int = 1,
        @n int = ISNULL((SELECT MAX(src_order) FROM #work), 0),
        @src int, @item varchar(100), @src_loc varchar(100), @src_type varchar(100),
        @need decimal(18,4), @state tinyint,
        @n_other int, @sum_open decimal(18,4), @max_open decimal(18,4), @best int;

WHILE @i <= @n
BEGIN
    SELECT @src = w.row_id, @item = w.item_key, @src_loc = w.loc_key, @src_type = l.type_key,
           @need = w.current_qty, @state = w.state
    FROM   #work AS w
    JOIN   #locstate AS l ON l.loc_key = w.loc_key
    WHERE  w.src_order = @i;
    SET @i += 1;

    IF @state <> 0 CONTINUE;          -- has received stock, so it stays

    -- Other locations of the item that may receive stock
    DELETE FROM @cand;
    INSERT INTO @cand (row_id, loc_key, cur_qty, open_cap)
    SELECT t.row_id, t.loc_key, t.cur_qty, l.open_cap
    FROM   #work AS t
    JOIN   #locstate AS l ON l.loc_key = t.loc_key
    WHERE  t.item_key = @item AND t.row_id <> @src AND t.state <> 1
      AND (@SameTypeOnly = 0 OR l.type_key = @src_type);

    SET @n_other = (SELECT COUNT(*) FROM @cand);
    DELETE FROM @cand WHERE open_cap <= 0;

    SELECT @sum_open = ISNULL(SUM(open_cap), 0),
           @max_open = ISNULL(MAX(open_cap), 0)
    FROM   @cand;

    -- One move if a single location can take it all
    SET @best = NULL;
    SELECT TOP (1) @best = row_id
    FROM   @cand
    WHERE  open_cap >= @need
    ORDER BY cur_qty DESC, open_cap ASC, loc_key ASC;

    DELETE FROM @alloc;
    IF @best IS NOT NULL
        INSERT INTO @alloc (seq, row_id, take) VALUES (1, @best, @need);
    ELSE IF @AllowSplit = 1 AND @sum_open >= @need
        INSERT INTO @alloc (seq, row_id, take)
        SELECT seq, row_id,
               CASE WHEN prev_open + open_cap <= @need THEN open_cap ELSE @need - prev_open END
        FROM  (SELECT row_id, open_cap,
                      ROW_NUMBER() OVER (ORDER BY open_cap DESC, cur_qty DESC, loc_key ASC) AS seq,
                      ISNULL(SUM(open_cap) OVER (ORDER BY open_cap DESC, cur_qty DESC, loc_key ASC
                                                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS prev_open
               FROM   @cand) AS c
        WHERE  prev_open < @need;
    ELSE
    BEGIN
        INSERT INTO #not_moved (row_id, room_elsewhere, reason)
        VALUES (@src,
                CASE WHEN @AllowSplit = 1 THEN @sum_open ELSE @max_open END,
                CASE WHEN @n_other = 0    THEN 'No other location of this item with the same location type'
                     WHEN @AllowSplit = 1 THEN 'Not enough room in the item''s other locations'
                     ELSE                      'No single location of this item has room for all of it'
                END);
        CONTINUE;
    END;

    INSERT INTO #moves (item_number, from_location, quantity_to_move, to_location,
                        target_open_capacity, target_max_capacity, target_location_type,
                        from_location_type, target_open_after_move)
    SELECT s.item_number, s.location, a.take, t.location,
           l.open_cap, l.max_qty, l.location_type,
           s.location_type, l.open_cap - a.take
    FROM   @alloc AS a
    JOIN   #work     AS t ON t.row_id  = a.row_id
    JOIN   #locstate AS l ON l.loc_key = t.loc_key
    CROSS JOIN (SELECT w.item_number, w.location, ls.location_type
                FROM   #work AS w JOIN #locstate AS ls ON ls.loc_key = w.loc_key
                WHERE  w.row_id = @src) AS s
    ORDER BY a.seq;

    UPDATE l
    SET    open_cap = l.open_cap - a.take
    FROM   #locstate AS l
    JOIN   #work AS t ON t.loc_key = l.loc_key
    JOIN   @alloc AS a ON a.row_id = t.row_id;

    UPDATE t
    SET    cur_qty = t.cur_qty + a.take, state = 2
    FROM   #work AS t
    JOIN   @alloc AS a ON a.row_id = t.row_id;

    UPDATE #work     SET cur_qty = 0, state = 1        WHERE row_id  = @src;
    UPDATE #locstate SET open_cap = open_cap + @need   WHERE loc_key = @src_loc;
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
        (SELECT COUNT(*) FROM #tagged WHERE dropped = 2)                              AS removed_over_max_qty,
        (SELECT COUNT(*) FROM #tagged WHERE dropped = 3)                              AS removed_ignored_type,
        (SELECT COUNT(*) FROM #work)                                                  AS rows_kept,
        (SELECT COUNT(DISTINCT item_key) FROM #work WHERE src_order IS NOT NULL)      AS items_in_multiple_locations,
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
