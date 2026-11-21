# Fig. 8a: native `MATCH_RECOGNIZE`, GDYN and EIMER on Trino

| Parameter | Value |
|---|---|
| Paper | Fig. 8a, Trino series |
| Approaches | native MR, GDYN, EIMER (clique-grow's pick) |
| Query | chain with a window between its first and last variable |
| Table size N | 2,000 to 250,000 (left panel); 5,000 (right panel) |
| Batches | 200 events per batch (left); 25 batches (right) |
| Pattern length k | 4 (left); 3 to 8 (right) |
| Window W | 1 s |
| Band selectivity | 0.05 |
| Event types | eight, each at 5 % of the rows |
| Query schedule | after every batch |

![Fig. 8a](trino_fig8_N_k.png)

Left: over the table size N; right: over the pattern length k. Hollow markers: the approach did not finish.
