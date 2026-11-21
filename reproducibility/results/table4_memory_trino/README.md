# Table 4: memory footprint of EIMER's materialization strategies, on Trino

| Parameter | Value |
|---|---|
| Paper | Table 4 |
| Approaches | EIMER with all singletons, clique-grow's pick, one view over the full pattern, all pattern prefixes |
| Query | chain |
| Table size N | 400,000 |
| Batches | 25 |
| Pattern length k | 3 to 6 |
| Window W | none |
| Band selectivity | 0.002 |
| Event types | eight, each at 2 % of the rows |
| Query schedule | after every batch |

![Table 4](trino_memory.png)

Left: stored state of a strategy's views at the end of the run; right: its peak query memory.
