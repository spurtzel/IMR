WITH RECURSIVE 
  automaton("from", "to", transition, penalty, start, finished, all_rows) AS MATERIALIZED (
    SELECT *
    FROM   ((VALUES (0, 1, 1, 0, TRUE, FALSE, TRUE),
                    (3, 4, 2, 0, FALSE, FALSE, TRUE),
                    (1, 3, 4, 1, FALSE, FALSE, TRUE),
                    (2, 3, 4, 1, FALSE, FALSE, TRUE),
                    (1, 2, 2, 0, FALSE, FALSE, TRUE),
                    (2, 2, 2, 0, FALSE, FALSE, TRUE),
                    (4, 4, 2, 0, FALSE, FALSE, TRUE),
                    (3, 5, 8, 2, FALSE, TRUE, TRUE),
                    (4, 5, 8, 2, FALSE, TRUE, TRUE)))
  ),
  prep AS MATERIALIZED (
    SELECT t.*,
           row_number() OVER win1 AS row_id
    FROM   events_window AS t
    WINDOW win1 AS (ORDER BY t.time)
  ),
  bfs AS MATERIALIZED (
    SELECT a.finished,
           p.row_id AS start,
           p.row_id,
           a."to" AS cur,
           CAST(a.penalty AS uhugeint) AS path,
           1 AS length,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
           END AS a_id,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
           END AS id_1,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
           END AS a_time,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
           END AS time_2,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
           END AS b_id,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
           END AS id_3,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
           END AS b_time,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
           END AS time_4,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
           END AS c_id,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
           END AS id_5,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
           END AS c_time,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
           END AS time_6,
           CASE WHEN (a.transition & 1) > 0
                THEN p.primary_type
           END AS primary_type_7,
           CASE WHEN (a.transition & 4) > 0
                THEN p.primary_type
           END AS primary_type_8,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_9,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
           END AS lon_10,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_11,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
           END AS lon_12,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_13,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
           END AS lat_14,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_15,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
           END AS lat_16,
           CASE WHEN (a.transition & 8) > 0
                THEN p.primary_type
           END AS primary_type_17,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
           END AS lon_18,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_19,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
           END AS lon_20,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
           END AS lon_21,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
           END AS lat_22,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_23,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
           END AS lat_24,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
           END AS lat_25,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
           END AS ts_26,
           CASE WHEN (a.transition & 8) > 0
                THEN p.ts
           END AS ts_27,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
           END AS ts_28,
           CASE WHEN (a.transition & 2) > 0
                THEN p.ts
           END AS ts_29
    FROM   prep AS p, automaton AS a
    WHERE  a.start
      AND  CASE a.transition
           WHEN 1
           THEN CASE WHEN (a.transition & 1) > 0
                     THEN p.primary_type
                END = 'T0'
           WHEN 4
           THEN CASE WHEN (a.transition & 4) > 0
                     THEN p.primary_type
                END = 'T1'
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                        END + 0.02)
           WHEN 8
           THEN CASE WHEN (a.transition & 8) > 0
                     THEN p.primary_type
                END = 'T2'
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                        END + 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                        END + 0.02)
           AND  abs(date_diff('millisecond',
                              CASE WHEN (a.transition & 1) > 0
                                   THEN p.ts
                              END,
                              CASE WHEN (a.transition & 8) > 0
                                   THEN p.ts
                              END)) <= 60
           WHEN 2
           THEN date_diff('millisecond',
                          CASE WHEN (a.transition & 1) > 0
                               THEN p.ts
                          END,
                          CASE WHEN (a.transition & 2) > 0
                               THEN p.ts
                          END) <= 60
           END

    UNION ALL

    SELECT a.finished,
           b.start,
           p.row_id,
           a."to" AS cur,
           (b.path << CAST(2 AS uhugeint)) + CAST(a.penalty AS uhugeint) AS path,
           b.length + 1 AS length,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
                ELSE b.id_1
           END AS a_id,
           CASE WHEN (a.transition & 1) > 0
                THEN p.id
                ELSE b.id_1
           END AS id_1,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
                ELSE b.time_2
           END AS a_time,
           CASE WHEN (a.transition & 1) > 0
                THEN p.time
                ELSE b.time_2
           END AS time_2,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
                ELSE b.id_3
           END AS b_id,
           CASE WHEN (a.transition & 4) > 0
                THEN p.id
                ELSE b.id_3
           END AS id_3,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
                ELSE b.time_4
           END AS b_time,
           CASE WHEN (a.transition & 4) > 0
                THEN p.time
                ELSE b.time_4
           END AS time_4,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
                ELSE b.id_5
           END AS c_id,
           CASE WHEN (a.transition & 8) > 0
                THEN p.id
                ELSE b.id_5
           END AS id_5,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
                ELSE b.time_6
           END AS c_time,
           CASE WHEN (a.transition & 8) > 0
                THEN p.time
                ELSE b.time_6
           END AS time_6,
           CASE WHEN (a.transition & 1) > 0
                THEN p.primary_type
                ELSE b.primary_type_7
           END AS primary_type_7,
           CASE WHEN (a.transition & 4) > 0
                THEN p.primary_type
                ELSE b.primary_type_8
           END AS primary_type_8,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_9
           END AS lon_9,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
                ELSE b.lon_10
           END AS lon_10,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_11
           END AS lon_11,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lon
                ELSE b.lon_12
           END AS lon_12,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_13
           END AS lat_13,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
                ELSE b.lat_14
           END AS lat_14,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_15
           END AS lat_15,
           CASE WHEN (a.transition & 1) > 0
                THEN p.lat
                ELSE b.lat_16
           END AS lat_16,
           CASE WHEN (a.transition & 8) > 0
                THEN p.primary_type
                ELSE b.primary_type_17
           END AS primary_type_17,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
                ELSE b.lon_18
           END AS lon_18,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_19
           END AS lon_19,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lon
                ELSE b.lon_20
           END AS lon_20,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lon
                ELSE b.lon_21
           END AS lon_21,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
                ELSE b.lat_22
           END AS lat_22,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_23
           END AS lat_23,
           CASE WHEN (a.transition & 8) > 0
                THEN p.lat
                ELSE b.lat_24
           END AS lat_24,
           CASE WHEN (a.transition & 4) > 0
                THEN p.lat
                ELSE b.lat_25
           END AS lat_25,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
                ELSE b.ts_26
           END AS ts_26,
           CASE WHEN (a.transition & 8) > 0
                THEN p.ts
                ELSE b.ts_27
           END AS ts_27,
           CASE WHEN (a.transition & 1) > 0
                THEN p.ts
                ELSE b.ts_28
           END AS ts_28,
           CASE WHEN (a.transition & 2) > 0
                THEN p.ts
                ELSE b.ts_29
           END AS ts_29
    FROM   bfs AS b, prep AS p, automaton AS a
    WHERE  a."from" = b.cur
      AND  (b.row_id + 1) = p.row_id
      AND  CASE a.transition
           WHEN 1
           THEN CASE WHEN (a.transition & 1) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_7
                END = 'T0'
           WHEN 4
           THEN CASE WHEN (a.transition & 4) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_8
                END = 'T1'
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                     ELSE b.lon_9
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                             ELSE b.lon_10
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lon
                     ELSE b.lon_11
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lon
                             ELSE b.lon_12
                        END + 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                     ELSE b.lat_13
                END >= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                             ELSE b.lat_14
                        END - 0.02)
           AND  CASE WHEN (a.transition & 4) > 0
                     THEN p.lat
                     ELSE b.lat_15
                END <= (CASE WHEN (a.transition & 1) > 0
                             THEN p.lat
                             ELSE b.lat_16
                        END + 0.02)
           WHEN 8
           THEN CASE WHEN (a.transition & 8) > 0
                     THEN p.primary_type
                     ELSE b.primary_type_17
                END = 'T2'
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                     ELSE b.lon_18
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                             ELSE b.lon_19
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lon
                     ELSE b.lon_20
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lon
                             ELSE b.lon_21
                        END + 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                     ELSE b.lat_22
                END >= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                             ELSE b.lat_23
                        END - 0.02)
           AND  CASE WHEN (a.transition & 8) > 0
                     THEN p.lat
                     ELSE b.lat_24
                END <= (CASE WHEN (a.transition & 4) > 0
                             THEN p.lat
                             ELSE b.lat_25
                        END + 0.02)
           AND  abs(date_diff('millisecond',
                              CASE WHEN (a.transition & 1) > 0
                                   THEN p.ts
                                   ELSE b.ts_26
                              END,
                              CASE WHEN (a.transition & 8) > 0
                                   THEN p.ts
                                   ELSE b.ts_27
                              END)) <= 60
           WHEN 2
           THEN date_diff('millisecond',
                          CASE WHEN (a.transition & 1) > 0
                               THEN p.ts
                               ELSE b.ts_28
                          END,
                          CASE WHEN (a.transition & 2) > 0
                               THEN p.ts
                               ELSE b.ts_29
                          END) <= 60
           END
  ),
  matches AS MATERIALIZED (
    SELECT b.start,
           arg_min(b.row_id,
                   b.path << CAST((64 - b.length) * 2 AS uhugeint)) AS "end",
           arg_min(b.path,
                   b.path << CAST((64 - b.length) * 2 AS uhugeint)) AS path,
           arg_min(b.length,
                   b.path << CAST((64 - b.length) * 2 AS uhugeint)) AS length
    FROM   bfs AS b
    WHERE  b.finished
    GROUP BY b.start
  ),
  skipping AS (
    SELECT *
    FROM   matches AS m
  ),
  collect AS (
    SELECT s.start,
           b.row_id,
           s.path,
           s.length,
           a."to" AS cur,
           a.all_rows,
           b.a_id,
           b.a_time,
           b.b_id,
           b.b_time,
           b.c_id,
           b.c_time
    FROM   skipping AS s, bfs AS b, automaton AS a
    WHERE  s.start = b.start
      AND  s.start = b.row_id
      AND  a.start
      AND  a."to" = b.cur
      AND  s.length >= b.length
      AND  (s.path >> CAST((s.length - b.length) * 2 AS uhugeint)) = b.path
      AND  s.start <> 0

    UNION ALL

    SELECT c.start,
           b.row_id,
           c.path,
           c.length,
           a."to" AS cur,
           a.all_rows,
           b.a_id,
           b.a_time,
           b.b_id,
           b.b_time,
           b.c_id,
           b.c_time
    FROM   collect AS c, bfs AS b, automaton AS a
    WHERE  c.start = b.start
      AND  (c.row_id + 1) = b.row_id
      AND  a."from" = c.cur
      AND  a."to" = b.cur
      AND  c.length >= b.length
      AND  (c.path >> CAST((c.length - b.length) * 2 AS uhugeint)) = b.path
  ),
  postprocessing AS (
    SELECT p.* EXCLUDE(row_id),
           c.a_id,
           c.a_time,
           c.b_id,
           c.b_time,
           c.c_id,
           c.c_time
    FROM   (SELECT arg_max(c.a_id, c.row_id) AS a_id,
                   arg_max(c.a_time, c.row_id) AS a_time,
                   arg_max(c.b_id, c.row_id) AS b_id,
                   arg_max(c.b_time, c.row_id) AS b_time,
                   arg_max(c.c_id, c.row_id) AS c_id,
                   arg_max(c.c_time, c.row_id) AS c_time,
                   max(c.row_id) AS row_id
            FROM   collect AS c
            GROUP BY c.start) AS c,
           prep AS p
    WHERE  p.row_id = c.row_id
  )
SELECT *
FROM   postprocessing;
