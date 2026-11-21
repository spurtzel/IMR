# Fig. 12: native MR and EIMER on real-world tables, on Trino

| Parameter | Value |
|---|---|
| Paper | Fig. 12 |
| Approaches | native MR, EIMER (the strategy stored in `reproducibility/reference/`) |
| Query | Q1 to Q5 on each of three tables (NYC taxi trips, Chicago crimes, e-commerce events) |
| Table size N | the first 100,000 rows of each table |
| Batches | 200 rows per batch (500 batches) |
| Pattern length k | 2 to 5 |
| Window W | per query, 30 minutes to 14 days in total (taxi Q2: none) |
| Band selectivity | - (real data) |
| Event types | - (real data) |
| Query schedule | after every batch |

![Fig. 12](trino_fig12_realworld.png)

Upper panel: speed-up of EIMER over native MR (hatched: a lower bound, native MR did not finish); lower panel: EIMER's persistent state plus its peak query memory.
