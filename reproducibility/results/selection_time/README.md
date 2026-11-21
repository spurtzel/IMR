# Selection time (§7.3)

| Parameter | Value |
|---|---|
| Paper | §7.3, Selection Time (no figure in the paper) |
| Approaches | clique-grow, and its seed, the edge cover, as a selector of its own |
| Query | chain, star and cycle (k = 3 to 13), complete (k = 3 to 6), without a window |
| Table size N | 100,000 (a constant of the cost model; no engine is involved and no table is read) |
| Batches | 25 |
| Pattern length k | 3 to 13 |
| Window W | none |
| Band selectivity | 0.01 |
| Event types | each type at 5 % of the rows |
| Query schedule | after every batch |

![Selection time](selection_time.png)

Solid lines: clique-grow; dashed lines: the edge cover. Each point is the median of five runs on one core (the complete query at k = 6: one run).
