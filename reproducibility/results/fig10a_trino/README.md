# Fig. 10a: EIMER and GDYN on acyclic and cyclic queries, on Trino

| Parameter | Value |
|---|---|
| Paper | Fig. 10a |
| Approaches | EIMER (clique-grow's pick), GDYN |
| Query | chain (acyclic); chain with a window between its first and last variable (cyclic) |
| Table size N | 10,000 to 100,000 (N panels); 25,000 (k panels) |
| Batches | 200 events per batch |
| Pattern length k | 4 (N panels); 3 to 8 (k panels) |
| Window W | 1 s (cyclic query) |
| Band selectivity | 0.02 |
| Event types | eight, each at 5 % of the rows |
| Query schedule | after every batch |

Lines: runtime; bars: persistent state; lower panels: share of maintenance and query time. On the acyclic query GDYN is 2.0x to
2.6x slower than EIMER up to k = 6, on par at k = 7 and faster at k = 8 (1,554 s vs. 3,519 s); it does not finish at
N = 100,000. On the cyclic query GDYN is 3.0x to 4.6x slower where it finishes (N up to 25,000, k up to 5).

![Fig. 10a](trino_fig10a_N_k.png)
