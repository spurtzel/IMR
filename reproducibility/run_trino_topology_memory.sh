#!/usr/bin/env bash
# Query topology and memory footprint on Trino (§7.4; the memory footprint is the paper's Table 4): native
# MATCH_RECOGNIZE vs. EIMER on six topologies at k = 4, and the persistent state and peak query memory of four
# materialization strategies on the chain at k = 3 to 6.
#   bash reproducibility/run_trino_topology_memory.sh                              the small default
#   PARTS=memory MEM_N=20000 bash reproducibility/run_trino_topology_memory.sh     one part, own values
# Results: reproducibility/out/trino_topology_memory/ (records, summary.md, figs/trino_topology.pdf, figs/trino_memory.pdf). The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
exec "$(dirname "$0")/docker/run_trino_experiment.sh" topology_memory
