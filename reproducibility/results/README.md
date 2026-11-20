# Results of our runs at `SCALE=paper`

This directory holds the figures of our own runs at `SCALE=paper`, one subdirectory per figure or table of the paper. Each
contains the figure (PNG and PDF) and the parameters of the run. Runtimes depend on the machine. The selected strategies,
the numbers of matches and the results do not.

| Paper | Experiment | Script | Directory |
|---|---|---|---|
| Fig. 8a, Trino series | native MR, GDYN and EIMER on Trino | `reproducibility/run_trino_fig8.sh` | [`fig8a_trino/`](fig8a_trino/README.md) |
| Fig. 8b | EIMER on DuckDB vs. the transpiled MR query | `reproducibility/run_duckdb.sh` | [`fig8b_duckdb/`](fig8b_duckdb/README.md) |
| Fig. 8c | EIMER's strategies on Esper vs. Esper's native MR | `reproducibility/run_esper.sh` | [`fig8c_esper/`](fig8c_esper/README.md) |
| Fig. 9 | native MR, the join-based prefilter and EIMER, on Trino | `reproducibility/run_trino_fig9.sh` | [`fig9_trino/`](fig9_trino/README.md) |
| Fig. 9 | the same with the paper's type rates, on Trino | `reproducibility/run_trino_fig9.sh` (`FIRST_RATE`, `LAST_RATE`) | [`fig9_trino_type_rates/`](fig9_trino_type_rates/README.md) |
| §7.3 (no figure) | selection time of clique-grow and of the edge cover | `reproducibility/run_selection_time.sh` | [`selection_time/`](selection_time/README.md) |
| Fig. 10a | EIMER and GDYN on acyclic and cyclic queries, on Trino | `reproducibility/run_trino_fig10.sh` | [`fig10a_trino/`](fig10a_trino/README.md) |
| Fig. 10b | the cyclic query under sparser query schedules, on Trino | `reproducibility/run_trino_fig10.sh` | [`fig10b_trino/`](fig10b_trino/README.md) |
| §7.4 (no figure) | EIMER and GDYN on skewed type frequencies, on Trino | `reproducibility/run_trino_fig10.sh` (part `s`) | [`skew_trino/`](skew_trino/README.md) |
| Fig. 11a | Kleene patterns, on Trino | `reproducibility/run_trino_kleene.sh` | [`fig11a_trino/`](fig11a_trino/README.md) |
| Fig. 11b, 11c | storage budget and distribution shift, on Trino | `reproducibility/run_trino_budget_shift.sh` | [`fig11bc_trino/`](fig11bc_trino/README.md) |
| §7.4 (no figure) | query topology: native MR and EIMER on six topologies, on Trino | `reproducibility/run_trino_topology_memory.sh` (part `topology`) | [`topology_trino/`](topology_trino/README.md) |
| Table 4 | memory footprint of EIMER's materialization strategies, on Trino | `reproducibility/run_trino_topology_memory.sh` | [`table4_memory_trino/`](table4_memory_trino/README.md) |
| Fig. 12 | native MR and EIMER on real-world tables, on Trino | `reproducibility/run_trino_realworld.sh` | [`fig12_trino/`](fig12_trino/README.md) |
