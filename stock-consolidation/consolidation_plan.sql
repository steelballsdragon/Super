/*==============================================================================
  STOCK CONSOLIDATION PLAN  (SQL Server 2012 or later)

  Finds items stored in more than one location and works out how many units
  to move from which location to which, so that locations can be emptied.

  Result 1 - one row per move:
      item_number, from_location, quantity_to_move, to_location,
      target_open_capacity, target_max_capacity, target_zone,
      from_zone, target_open_after_move
  Result 2 - locations of multi-location items that could not be emptied
  Result 3 - summary

  The rules (Stock_Consolidation.xlsm applies exactly the same ones):
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
    6. Open capacity = max_capacity - everything in the location (all items),
       and goes down as moves are planned.

  HOW TO USE
    1. Edit the two INSERTs in section 1 so they read your own tables.
    2. Run. (Set @UseDemoData = 1 to try it on the built-in sample first.)
    3. To feed the Excel workbook instead, set @ShowInputs = 1, copy the two
       extra result grids (with headers) into the Inventory and Locations
       sheets, and press "Build Consolidation Plan".
==============================================================================*/
SET NOCOUNT ON;

--------------------------------------------------------------------------------
-- SETTINGS
--------------------------------------------------------------------------------
DECLARE @SameZoneOnly bit           = 0;        -- 1 = only move within the source location's zone
DECLARE @AllowSplit   bit           = 1;        -- 1 = a location may be emptied into several locations
DECLARE @ExcludeZones varchar(4000) = 'STAGE';  -- comma-separated zones to ignore completely
DECLARE @UseDemoData  bit           = 0;        -- 1 = ignore your tables and use the sample data below
DECLARE @ShowInputs   bit           = 0;        -- 1 = also return the input data (to paste into Excel)

--------------------------------------------------------------------------------
-- 1. INPUT
--------------------------------------------------------------------------------
IF OBJECT_ID('tempdb..#stock')     IS NOT NULL DROP TABLE #stock;
IF OBJECT_ID('tempdb..#locations') IS NOT NULL DROP TABLE #locations;

CREATE TABLE #stock (            -- on-hand stock: one or more rows per item per location
    item_number varchar(100)  NULL,
    location    varchar(100)  NULL,
    quantity    decimal(18,4) NULL
);
CREATE TABLE #locations (        -- location master
    location     varchar(100)  NULL,
    zone         varchar(100)  NULL,
    max_capacity decimal(18,4) NULL   -- most units the location can hold; NULL = unknown
);

IF @UseDemoData = 0
BEGIN
    /* >>>>>>>>>>>>>>>> CHANGE THESE TWO QUERIES TO MATCH YOUR TABLES <<<<<<<<<<<<<<<< */
    INSERT INTO #stock (item_number, location, quantity)
    SELECT  inv.item_number,
            inv.location,
            inv.quantity
    FROM    dbo.inventory_on_hand AS inv
 -- WHERE   inv.warehouse = 'WH1'          -- one warehouse at a time
 --   AND   inv.status    = 'AVAILABLE'    -- only stock that can be moved
    ;

    INSERT INTO #locations (location, zone, max_capacity)
    SELECT  loc.location,
            loc.zone,
            loc.max_capacity
    FROM    dbo.location_master AS loc
 -- WHERE   loc.warehouse = 'WH1'
    ;
    /* >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>><<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<< */
END
ELSE
BEGIN
    -- DEMO DATA START (generated from generator/demo_data.py - same rows as the workbook)
    INSERT INTO #stock (item_number, location, quantity) VALUES
        ('5794400', 'ZA186', 5),
        ('5794400', 'AOF5', 6),
        ('5794411', 'AOF1', 3),
        ('5794411', 'AOF2', 8),
        ('5794411', 'ZA181', 9),
        ('5794422', 'STG1', 10),
        ('5794422', 'AOF3', 4),
        ('5794433', 'AOF10', 2),
        ('5794433', 'AOF11', 3),
        ('5794433', 'ZA184', 9),
        ('5794433', 'AOF12', 10),
        ('6100025', 'ZA183', 14),
        ('6100025', 'ZB12', 30),
        ('6100025', 'ZB13', 34),
        ('6100031', 'ZB17', 7),
        ('6100031', 'ZB17', 2),
        ('6100031', 'ZB18', 25),
        ('7002210', 'ZA190', 18),
        ('7002210', 'ZA191', 19),
        ('7300001', 'X99', 4),
        ('7300001', 'AOF9', 2),
        ('8800100', 'BLK01', 120),
        ('8800100', 'ZB15', 15),
        ('8800200', 'BLK01', 60),
        ('8800200', 'ZB16', 12),
        ('8800200', 'BLK02', 190),
        ('9100001', 'ZA195', 3),
        ('9100001', 'ZC01', 10),
        ('9100002', 'ZC02', 5),
        ('9100002', 'ZC03', 6),
        ('4410078', 'AOF4', 9),
        ('4410078', 'AOF6', 2),
        ('4410078', 'ZA187', 1),
        ('4410090', 'ZB10', 22),
        ('4410090', 'ZB11', 6),
        ('4410090', 'ZA188', 4),
        ('4410105', 'AOF7', 11),
        ('4410105', 'AOF8', 3),
        ('3300512', 'ZA182', 7),
        ('3300512', 'ZA185', 6),
        ('3300512', 'BLK03', 150),
        ('3300527', 'ZB14', 40),
        ('3300527', 'ZB19', 1),
        ('3300527', 'ZB20', 38),
        ('5000001', 'ZA189', 16),
        ('5000001', 'ZA192', 2),
        ('5000001', 'ZA193', 5),
        ('5000001', 'ZA194', 15);
    INSERT INTO #locations (location, zone, max_capacity) VALUES
        ('AOF1', 'CONSOF', 15),
        ('AOF2', 'CONSOF', 15),
        ('AOF3', 'CONSOF', 15),
        ('AOF4', 'CONSOF', 15),
        ('AOF5', 'CONSOF', 15),
        ('AOF6', 'CONSOF', 15),
        ('AOF7', 'CONSOF', 15),
        ('AOF8', 'CONSOF', 15),
        ('AOF9', 'CONSOF', 15),
        ('AOF10', 'CONSOF', 15),
        ('AOF11', 'CONSOF', 15),
        ('AOF12', 'CONSOF', 15),
        ('ZA180', 'ZA', 20),
        ('ZA181', 'ZA', 20),
        ('ZA182', 'ZA', 20),
        ('ZA183', 'ZA', 20),
        ('ZA184', 'ZA', 20),
        ('ZA185', 'ZA', 20),
        ('ZA186', 'ZA', 20),
        ('ZA187', 'ZA', 20),
        ('ZA188', 'ZA', 20),
        ('ZA189', 'ZA', 20),
        ('ZA190', 'ZA', 20),
        ('ZA191', 'ZA', 20),
        ('ZA192', 'ZA', 20),
        ('ZA193', 'ZA', 20),
        ('ZA194', 'ZA', 20),
        ('ZA195', 'ZA', 20),
        ('ZB10', 'ZB', 40),
        ('ZB11', 'ZB', 40),
        ('ZB12', 'ZB', 40),
        ('ZB13', 'ZB', 40),
        ('ZB14', 'ZB', 40),
        ('ZB15', 'ZB', 40),
        ('ZB16', 'ZB', 40),
        ('ZB17', 'ZB', 40),
        ('ZB18', 'ZB', 40),
        ('ZB19', 'ZB', 40),
        ('ZB20', 'ZB', 40),
        ('BLK01', 'BULK', 200),
        ('BLK02', 'BULK', 200),
        ('BLK03', 'BULK', 200),
        ('ZC01', 'ZC', NULL),
        ('ZC02', 'ZC', NULL),
        ('ZC03', 'ZC', NULL),
        ('STG1', 'STAGE', 999);    -- DEMO DATA END
END;

IF @ShowInputs = 1
BEGIN
    SELECT item_number, location, quantity FROM #stock;
    SELECT location, zone, max_capacity    FROM #locations;
END;

--------------------------------------------------------------------------------
-- 2. PREPARE
--------------------------------------------------------------------------------
IF OBJECT_ID('tempdb..#locstate')   IS NOT NULL DROP TABLE #locstate;
IF OBJECT_ID('tempdb..#work')       IS NOT NULL DROP TABLE #work;
IF OBJECT_ID('tempdb..#moves')      IS NOT NULL DROP TABLE #moves;
IF OBJECT_ID('tempdb..#not_moved')  IS NOT NULL DROP TABLE #not_moved;

-- Keys are trimmed and upper-cased, and compared/sorted byte by byte, so the
-- order of work is the same whatever the server collation (and the same as Excel).
CREATE TABLE #locstate (
    loc_key      varchar(100) COLLATE Latin1_General_BIN2 NOT NULL PRIMARY KEY,
    zone         varchar(100) NOT NULL,
    zone_key     varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
    max_capacity decimal(18,4) NULL,
    total_qty    decimal(18,4) NOT NULL          -- everything in the location, all items
);

CREATE TABLE #work (
    row_id      int IDENTITY(1,1) PRIMARY KEY,
    item_key    varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
    loc_key     varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
    item_number varchar(100) NOT NULL,
    location    varchar(100) NOT NULL,
    start_qty   decimal(18,4) NOT NULL,
    cur_qty     decimal(18,4) NOT NULL,
    zone        varchar(100) NULL,                -- NULL = not in the location master
    zone_key    varchar(100) COLLATE Latin1_General_BIN2 NULL,
    eligible    bit NOT NULL,                     -- 0 = in an excluded zone
    state       tinyint NOT NULL DEFAULT 0,       -- 0 untouched, 1 emptied, 2 received stock
    src_order   int NULL                          -- order in which locations are tried
);

CREATE TABLE #moves (
    move_seq               int IDENTITY(1,1) PRIMARY KEY,
    item_number            varchar(100),
    from_location          varchar(100),
    quantity_to_move       decimal(18,4),
    to_location            varchar(100),
    target_open_capacity   decimal(18,4),
    target_max_capacity    decimal(18,4),
    target_zone            varchar(100),
    from_zone              varchar(100),
    target_open_after_move decimal(18,4)
);

CREATE TABLE #not_moved (
    seq            int IDENTITY(1,1) PRIMARY KEY,
    row_id         int NOT NULL,
    room_elsewhere decimal(18,4) NOT NULL,
    reason         varchar(100) NOT NULL
);

-- Excluded zones
DECLARE @ExcludedZone TABLE (zone_key varchar(100) COLLATE Latin1_General_BIN2 PRIMARY KEY);
DECLARE @rest varchar(4001) = ISNULL(@ExcludeZones, '') + ',', @comma int, @z varchar(4000);
SET @comma = CHARINDEX(',', @rest);
WHILE @comma > 0
BEGIN
    SET @z = UPPER(LTRIM(RTRIM(LEFT(@rest, @comma - 1))));
    IF @z <> '' AND NOT EXISTS (SELECT 1 FROM @ExcludedZone WHERE zone_key = @z)
        INSERT INTO @ExcludedZone (zone_key) VALUES (@z);
    SET @rest  = SUBSTRING(@rest, @comma + 1, 4001);
    SET @comma = CHARINDEX(',', @rest);
END;

-- Location master (a duplicated location keeps its largest zone and capacity)
INSERT INTO #locstate (loc_key, zone, zone_key, max_capacity, total_qty)
SELECT  l.loc_key,
        MAX(l.zone),
        UPPER(MAX(l.zone)),
        MAX(l.max_capacity),
        0
FROM   (SELECT UPPER(LTRIM(RTRIM(location))) COLLATE Latin1_General_BIN2         AS loc_key,
               LTRIM(RTRIM(ISNULL(zone, ''))) COLLATE Latin1_General_BIN2       AS zone,
               max_capacity
        FROM   #locations
        WHERE  LTRIM(RTRIM(ISNULL(location, ''))) <> '') AS l
GROUP BY l.loc_key;

-- Stock summed per item per location
INSERT INTO #work (item_key, loc_key, item_number, location, start_qty, cur_qty, zone, zone_key, eligible)
SELECT  s.item_key, s.loc_key, s.item_number, s.location, s.qty, s.qty,
        l.zone, l.zone_key,
        CASE WHEN x.zone_key IS NULL THEN 1 ELSE 0 END
FROM   (SELECT  k.item_key, k.loc_key,
                MIN(k.item_number) AS item_number,
                MIN(k.location)    AS location,
                SUM(k.quantity)    AS qty
        FROM   (SELECT UPPER(LTRIM(RTRIM(item_number))) COLLATE Latin1_General_BIN2 AS item_key,
                       UPPER(LTRIM(RTRIM(location)))    COLLATE Latin1_General_BIN2 AS loc_key,
                       LTRIM(RTRIM(item_number))        COLLATE Latin1_General_BIN2 AS item_number,
                       LTRIM(RTRIM(location))           COLLATE Latin1_General_BIN2 AS location,
                       quantity
                FROM   #stock
                WHERE  LTRIM(RTRIM(ISNULL(item_number, ''))) <> ''
                  AND  LTRIM(RTRIM(ISNULL(location, '')))    <> ''
                  AND  quantity IS NOT NULL) AS k
        GROUP BY k.item_key, k.loc_key
        HAVING SUM(k.quantity) > 0) AS s
LEFT JOIN #locstate     AS l ON l.loc_key  = s.loc_key
LEFT JOIN @ExcludedZone AS x ON x.zone_key = l.zone_key;

UPDATE l
SET    total_qty = t.qty
FROM   #locstate AS l
JOIN  (SELECT loc_key, SUM(start_qty) AS qty FROM #work GROUP BY loc_key) AS t
       ON t.loc_key = l.loc_key;

-- Order of work: by item, then smallest quantity first
UPDATE w
SET    src_order = o.rn
FROM   #work AS w
JOIN  (SELECT row_id, ROW_NUMBER() OVER (ORDER BY item_key, start_qty, loc_key) AS rn
       FROM   #work
       WHERE  eligible = 1
         AND  item_key IN (SELECT item_key FROM #work WHERE eligible = 1
                           GROUP BY item_key HAVING COUNT(*) >= 2)) AS o
       ON o.row_id = w.row_id;

CREATE INDEX ix_work_item  ON #work (item_key) INCLUDE (state, eligible, zone_key, loc_key, cur_qty);
CREATE INDEX ix_work_order ON #work (src_order);

--------------------------------------------------------------------------------
-- 3. PLAN THE MOVES
--------------------------------------------------------------------------------
DECLARE @cand  TABLE (row_id int PRIMARY KEY,
                      loc_key varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
                      cur_qty decimal(18,4) NOT NULL,
                      open_cap decimal(18,4) NOT NULL);
DECLARE @alloc TABLE (seq int PRIMARY KEY, row_id int NOT NULL, take decimal(18,4) NOT NULL);

DECLARE @i int = 1,
        @n int = ISNULL((SELECT MAX(src_order) FROM #work), 0),
        @src int, @item varchar(100), @src_loc varchar(100), @src_zone varchar(100),
        @need decimal(18,4), @state tinyint,
        @n_other int, @n_cap int, @sum_open decimal(18,4), @max_open decimal(18,4),
        @best int;

WHILE @i <= @n
BEGIN
    SELECT @src = row_id, @item = item_key, @src_loc = loc_key, @src_zone = zone_key,
           @need = start_qty, @state = state
    FROM   #work
    WHERE  src_order = @i;
    SET @i += 1;

    IF @state <> 0 CONTINUE;          -- has received stock, so it stays

    -- Other locations of the item that may receive stock
    SELECT @n_other = COUNT(*),
           @n_cap   = COUNT(l.max_capacity)
    FROM   #work AS t
    LEFT JOIN #locstate AS l ON l.loc_key = t.loc_key
    WHERE  t.item_key = @item AND t.row_id <> @src AND t.state <> 1 AND t.eligible = 1
      AND (@SameZoneOnly = 0 OR t.zone_key = @src_zone);

    DELETE FROM @cand;
    INSERT INTO @cand (row_id, loc_key, cur_qty, open_cap)
    SELECT t.row_id, t.loc_key, t.cur_qty, l.max_capacity - l.total_qty
    FROM   #work AS t
    JOIN   #locstate AS l ON l.loc_key = t.loc_key
    WHERE  t.item_key = @item AND t.row_id <> @src AND t.state <> 1 AND t.eligible = 1
      AND (@SameZoneOnly = 0 OR t.zone_key = @src_zone)
      AND  l.max_capacity - l.total_qty > 0;

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
                CASE WHEN @n_other = 0    THEN 'No other location of this item in the same zone'
                     WHEN @n_cap = 0      THEN 'Other locations of this item have no max capacity set'
                     WHEN @AllowSplit = 1 THEN 'Not enough room in the item''s other locations'
                     ELSE                      'No single location of this item has room for all of it'
                END);
        CONTINUE;
    END;

    INSERT INTO #moves (item_number, from_location, quantity_to_move, to_location,
                        target_open_capacity, target_max_capacity, target_zone,
                        from_zone, target_open_after_move)
    SELECT s.item_number, s.location, a.take, t.location,
           l.max_capacity - l.total_qty, l.max_capacity, l.zone,
           s.zone, l.max_capacity - l.total_qty - a.take
    FROM   @alloc AS a
    JOIN   #work     AS t ON t.row_id  = a.row_id
    JOIN   #locstate AS l ON l.loc_key = t.loc_key
    CROSS JOIN (SELECT item_number, location, zone FROM #work WHERE row_id = @src) AS s
    ORDER BY a.seq;

    UPDATE l
    SET    total_qty = l.total_qty + a.take
    FROM   #locstate AS l
    JOIN   #work AS t ON t.loc_key = l.loc_key
    JOIN   @alloc AS a ON a.row_id = t.row_id;

    UPDATE t
    SET    cur_qty = t.cur_qty + a.take, state = 2
    FROM   #work AS t
    JOIN   @alloc AS a ON a.row_id = t.row_id;

    UPDATE #work     SET cur_qty = 0, state = 1         WHERE row_id  = @src;
    UPDATE #locstate SET total_qty = total_qty - @need  WHERE loc_key = @src_loc;
END;

--------------------------------------------------------------------------------
-- 4. RESULTS
--------------------------------------------------------------------------------
-- Moves
SELECT  item_number,
        from_location,
        quantity_to_move,
        to_location,
        target_open_capacity,
        target_max_capacity,
        target_zone,
        from_zone,
        target_open_after_move
FROM    #moves
ORDER BY move_seq;

-- Locations that stay (could not be emptied)
SELECT  w.item_number,
        w.location,
        w.zone,
        w.start_qty AS quantity,
        n.room_elsewhere,
        n.reason
FROM    #not_moved AS n
JOIN    #work      AS w ON w.row_id = n.row_id
WHERE   w.state = 0
ORDER BY n.seq;

-- Summary
SELECT  (SELECT COUNT(DISTINCT item_key) FROM #work WHERE src_order IS NOT NULL)        AS items_in_multiple_locations,
        (SELECT COUNT(*) FROM #work WHERE state = 1)                                    AS locations_emptied,
        (SELECT COUNT(*) FROM #moves)                                                   AS moves,
        (SELECT ISNULL(SUM(quantity_to_move), 0) FROM #moves)                           AS units_to_move,
        (SELECT COUNT(*) FROM #not_moved AS n JOIN #work AS w ON w.row_id = n.row_id
         WHERE w.state = 0)                                                             AS locations_not_emptied;
