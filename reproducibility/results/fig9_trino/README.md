# Fig. 9: native MR, the join-based prefilter and EIMER, on Trino

| Parameter | Value |
|---|---|
| Paper | Fig. 9 |
| Approaches | native MR, the join-based prefilter (the fastest subsequence filter per workload), EIMER (clique-grow's pick) |
| Query | chain with a window between its first and last variable |
| Table size N | 10,000 (window panel); 25,000 and 50,000 (batch panel) |
| Batches | 10 (window panel); 1 to 25 (batch panel) |
| Pattern length k | 4 |
| Window W | 200 ms to 2 s (window panel); 1 s (batch panel) |
| Band selectivity | generator setting 0.05 and 0.02 (window panel), 0.05 (batch panel); the query's band is wider, its measured selectivity is about 0.1 and 0.05 |
| Event types | first and last type at 1 % of the rows each, the others at 10 % |
| Query schedule | after every batch |

![Fig. 9](trino_fig9_W_B.png)

Left: over the window size W; right: over the number of batches B. Crosses: the approach did not finish.
