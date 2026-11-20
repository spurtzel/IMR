# Fig. 9 with the paper's type rates: native MR, the join-based prefilter and EIMER, on Trino

| Parameter | Value |
|---|---|
| Paper | Fig. 9 |
| Approaches | native MR, the join-based prefilter (the fastest subsequence filter per workload), EIMER (clique-grow's pick) |
| Query | chain with a window between its first and last variable |
| Table size N | 50,000 |
| Batches | 10 (window panel); 1 to 10 (batch panel) |
| Pattern length k | 4 |
| Window W | 200 ms to 1 s (window panel); 500 ms (batch panel) |
| Band selectivity | generator setting 0.05; the query's band is wider, its measured selectivity is about 0.1 |
| Event types | first type at 10 % of the rows, the others at 10 %; last type at 1 % or 0.1 % (window panel), at 0.1 % (batch panel) |
| Query schedule | after every batch |

Type rates and table size are those of the paper's Fig. 9. The paper's window panel has 50 batches and windows up to 2 s;
its batch panel has W = 1 s and up to 250 batches, which native MR does not finish on Trino.

Window panel: the prefilter pays off only if the last type is rare. At 0.1 % it is 7.1x faster than native MR at
W = 200 ms (24 s against 171 s) and 5.7x at W = 500 ms (943 s against 5,367 s). At 1 % it does not help (202 s against
160 s, 4,366 s against 4,251 s). EIMER takes 19 s to 42 s in all six workloads: it is 8x faster than native MR at
W = 200 ms and 111x to 152x at W = 500 ms, and 1.2x to 114x faster than the prefilter. At W = 1 s neither baseline
finishes, because one of their queries exceeds the limit of 30 minutes per statement.

Batch panel (last type at 0.1 %): both baselines re-run over the growing table. Native MR takes 1,018 s for a single
query over the whole table and 5,367 s with ten batches; the prefilter is 5.6x to 5.9x faster (183 s to 943 s). EIMER
takes 15 s to 35 s, 70x to 152x faster than native MR and 13x to 27x faster than the prefilter. The point at ten batches
is the window panel's workload at W = 500 ms.

All approaches return the same result wherever a baseline finishes.

![Fig. 9 with the paper's type rates](trino_fig9_W_B.png)
