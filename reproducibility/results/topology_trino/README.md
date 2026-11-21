# Query topology (§7.4): native MR and EIMER on six topologies, on Trino

| Parameter | Value |
|---|---|
| Paper | §7.4, query topology (not plotted in the paper) |
| Approaches | native MR, EIMER (clique-grow's pick) |
| Query | chain, star, triangle, cycle, diamond, complete; no time window |
| Table size N | 8,000 |
| Batches | 25 |
| Pattern length k | 4 |
| Window W | none |
| Band selectivity | 0.01, 0.05, 0.1 |
| Event types | eight, each at 2 % of the rows |
| Query schedule | after every batch |

![Query topology](trino_topology.png)

Speed-up of EIMER over native MR. Hollow markers: a lower bound, native MR did not finish.
