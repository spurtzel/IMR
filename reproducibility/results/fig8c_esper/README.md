# Fig. 8c: EIMER's strategies on Esper vs. Esper's `match_recognize`

| Parameter | Value |
|---|---|
| Paper | Fig. 8c |
| Approaches | Esper's native MR; EIMER with clique-grow's pick, the edge cover, all prefixes, all singletons |
| Query | chain with a window between its first and last variable |
| Table size N | 2,000 to 500,000 (left panel); 25,000 (right panel) |
| Batches | 200 events per batch (left); 25 batches (right) |
| Pattern length k | 4 (left); 3 to 8 (right) |
| Window W | 1 s |
| Band selectivity | 0.05 |
| Event types | eight, each at 5 % of the rows |
| Query schedule | after every batch |

Over N, clique-grow's pick is 1.2x to 3.5x faster than Esper's MR up to N = 100,000; Esper's MR does not finish from
N = 250,000 and the pick does not finish at N = 500,000. The prefix strategy is the fastest on Esper at k = 4 (0.46 s to 693 s). Over k, the pick is 1.6x to 3.8x faster than Esper's MR up to
k = 7 and slower at k = 8 (66.5 s vs. 20.1 s). Bars: peak memory.

![Fig. 8c](esper_N_k.png)
