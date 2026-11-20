# Reproducing the paper's experiments

Each experiment runs in containers with one script. It writes one record per workload, a summary table with the checks,
and the figure to `reproducibility/out/<experiment>/`. Fig. 8 runs on the paper's engines. The experiments that the paper
reports on DBMS A (Figs. 9 to 12, Table 4) run on Trino here, on smaller tables. Section *Differences from the paper*
lists what differs.

| Experiment | Figure | Script | Figure file under `reproducibility/out/` |
|---|---|---|---|
| Native MR, GDYN and EIMER on Trino | Fig. 8a, Trino series | `bash reproducibility/run_trino_fig8.sh` | `trino_fig8/figs/trino_fig8_N_k.pdf` |
| EIMER on DuckDB vs. re-executing the MR query transpiled to SQL | Fig. 8b | `bash reproducibility/run_duckdb.sh` | `duckdb/figs/duckdb_N_k.pdf` |
| EIMER's strategies on Esper vs. Esper's native MR | Fig. 8c | `bash reproducibility/run_esper.sh` | `esper/figs/esper_N_k.pdf` |
| Native MR, the join-based prefilter and EIMER on Trino | Fig. 9, on Trino | `bash reproducibility/run_trino_fig9.sh` | `trino_fig9/figs/trino_fig9_W_B.pdf` |
| Selection time of clique-grow and of the edge cover over the pattern length, for four topologies (no engine) | §7.3 (text) | `bash reproducibility/run_selection_time.sh` | `selection_time/figs/selection_time.pdf` |
| EIMER and GDYN on acyclic and cyclic queries, under sparser query schedules, and on skewed type frequencies | Fig. 10 and the skew results of §7.4, on Trino | `bash reproducibility/run_trino_fig10.sh` | `trino_fig10/figs/trino_fig10a_N_k.pdf`, `trino_fig10b_schedule.pdf`, `trino_fig10s_skew.pdf` |
| Kleene patterns: a Kleene view maintained by rescan vs. incremental stitch; native MR vs. EIMER without / with the stitch | Fig. 11, Kleene panels, on Trino | `bash reproducibility/run_trino_kleene.sh` | `trino_kleene/figs/trino_fig11_kleene.pdf` |
| Storage budget (speed-up over native MR while M shrinks) and distribution shift (stale plan vs. re-selection) | Fig. 11, budget and shift panels, on Trino | `bash reproducibility/run_trino_budget_shift.sh` | `trino_budget_shift/figs/trino_fig11_budget_shift.pdf` |
| Query topology (native MR vs. EIMER on six topologies) and memory footprint (state and peak query memory of four materialization strategies) | §7.4 (text) and Table 4, on Trino | `bash reproducibility/run_trino_topology_memory.sh` | `trino_topology_memory/figs/trino_topology.pdf`, `trino_memory.pdf` |
| Real-world tables (NYC taxi trips, Chicago crimes, e-commerce events; five queries each): native MR vs. EIMER | Fig. 12, on Trino | `bash reproducibility/run_trino_realworld.sh` | `trino_realworld/figs/trino_fig12_realworld.pdf` |

`bash reproducibility/run_all.sh` builds the images once and runs the ten scripts one after the other. `--list` prints
them; `ONLY="duckdb trino_fig9"` runs a subset. Podman works as well: `DOCKER=podman bash reproducibility/run_duckdb.sh`.

The scripts build two images:

- `reproducibility/docker/Dockerfile.experiments`: Python 3.10 with the pinned packages, a Java 21 runtime, the Esper
  program (built with Maven) and LaTeX for the figures.
- `reproducibility/docker/Dockerfile.trino`: Trino 479 with an Iceberg catalog on the container's local file system and
  an in-memory catalog.

Each experiment writes:

- `figs/`: the figure, as PDF and PNG.
- `summary.md`: one row per workload with runtimes, speed-up, memory, the selected strategy and the checks.
- `records/`: one JSON record per workload. The DuckDB experiment writes them to `runtime/` and `peakmem/`, the Esper
  experiment to one directory per stage.
- `logs/`: one log per workload and stage.

A script exits with 0 only if every check passes. We check three things per workload: the approaches that finish return
the same tuples; the run used the settings of our runs (engine version, single-threaded, join reordering off); and,
where `reference/` has an entry for the workload, the selection arrives at the same strategy and the query has the same
number of matches. An interrupted run can be restarted; workloads that already have a record are skipped.

`reference/` holds, per workload of our runs, the strategy that clique-grow selected, the selectivity estimates it used
and the number of matches. For such a workload the scripts execute this strategy, so that the runtimes can be compared
with ours; they also run the selection on the generated table and check that it selects the same strategy. The summaries
call these values "the paper's". The figures of our runs at `SCALE=paper`, with their parameters, are in
[`results/`](results/README.md).

## Requirements

- Linux with bash 4.4 or newer and Docker or Podman.
- Time: with the small defaults, all ten scripts together take about two and three quarter hours (2 h 43 min on a laptop). At `SCALE=paper` the runs take hours
  to days; Fig. 8a takes more than a day.
- Memory: with the small defaults, Trino runs with a heap of 8 GB. At `SCALE=paper`, one approach may use up to 12 GB on
  DuckDB and Esper, and the real-world experiment starts Trino with a heap of 40 GB, because the tables are held in memory.

## Scale

By default, every experiment runs with the small parameters of the middle column. `SCALE=paper` sets the parameters of
the right column. Single parameters are set as environment variables, e.g.
`NS=5000,10000,25000,50000 KS=3,4,5,6 bash reproducibility/run_duckdb.sh`.

| | Default | `SCALE=paper` |
|---|---|---|
| Fig. 8a, table size `NS` (200 events per batch, k = 4) | `2000,5000` | `2000,5000,10000,25000,50000,100000,250000` |
| Fig. 8a, pattern length `KS` (N = 5,000, B = 25) | `3,4,5` | `3,4,5,6,7,8` |
| Fig. 8b (DuckDB), table size `NS` (batches of 1,000 events) | `5000,10000,25000` | `5000,10000,25000,50000,100000,250000,500000,1000000` |
| Fig. 8b (DuckDB), pattern length `KS` (N = 25,000, B = 25) | `3,4,5` | `3,4,5,6,7,8` |
| Fig. 8c (Esper), table size `NS` (batches of 200 events) | `2000,5000,10000` | `2000,5000,10000,25000,50000,100000,250000,500000` |
| Fig. 8c (Esper), pattern length `KS` (N = 25,000, B = 25) | `3,4,5` | `3,4,5,6,7,8` |
| Fig. 9, window panel: table size `NW` (B = 10), selectivities `SIGMAS`, windows `WS` [ms] | `5000`; `0.05`; `200,500,1000` | `10000`; `0.05,0.02`; `200,500,1000,2000` |
| Fig. 9, batch panel: table sizes `NBS`, numbers of batches `BS`, window `BW` [ms] | `5000`; `1,5,25`; `1000` | `25000,50000`; `1,2,5,10,25`; `1000` |
| Selection time: pattern lengths `KS` (chain, star, cycle) and `COMPLETE_KS` (complete), runs per query `REPS` | `3,4,5,6,7,8`; `3,4,5`; `5` | `3` to `13`; `3,4,5,6`; `5` |
| Fig. 10a, table size `NS` (200 events per batch, k = 4), query classes `CLASSES` | `5000,10000`; `acyclic,cyclic` | `10000,25000,50000,100000`; `acyclic,cyclic` |
| Fig. 10a, pattern length `KS` at table size `NC` | `3,4,5`; `5000` | `3,4,5,6,7,8`; `25000` |
| Fig. 10b, table size `SCHED_N` in `SCHED_B` batches, queried percentages `PCTS` | `5000`; `100`; `100,50,10,1` | `50000`; `1000`; `100,90,80,70,60,50,40,30,20,10,1` |
| Skewed type frequencies (part `s` of the Fig. 10 script), table size `SKEW_N` (200 events per batch, k = 4) | `10000` | `25000` |
| Kleene, one view: table size `PAYOFF_N` (batches of 1,000), run length `PAYOFF_L`, seeds `SEEDS` | `50000`; `30`; `1,2` | `100000`; `30`; `1,2,3` |
| Kleene, the query: table size `KN` in `KB` batches, run lengths `LS` | `10000`; `10`; `5,25,100` | `100000`; `100`; `5,10,25,50,100` |
| Budget: `WORKLOADS` (topology:k:sigma:type rate:N:batches) | chain, star, triangle, diamond at k = 4, N = 3,000, 5 batches | the paper's 12 workloads on tables of 5,000 to 12,000 rows |
| Shift: table size `SHIFT_N` (20 batches), `SHIFT_PARAMS` (sigma, type rate before / after) | `4000`; `0.012,0.08,0.16,0.12` | `10000`; `0.0125,0.03,0.25,0.045` |
| Topology: structures `TOPOLOGIES` at k = 4, selectivities `TOPO_SIGMAS`, table size `TOPO_N` in `TOPO_B` batches, type rate `TOPO_RATE` | all six; `0.01,0.05,0.1`; `2500`; `5`; `0.05` | all six; `0.01,0.05,0.1`; `8000`; `25`; `0.02` |
| Memory: pattern lengths `MEM_KS` (chain), table size `MEM_N` in `MEM_B` batches, selectivity `MEM_SIGMA`, type rate `MEM_RATE` | `3,4,5,6`; `5000`; `5`; `0.02`; `0.05` | `3,4,5,6`; `400000`; `25`; `0.002`; `0.02` |
| Real-world: rows of each table `RW_N` (batches of `RW_BATCH` = 200), limit per approach `RW_TOTAL` [s] | `2000`; `120` | `100000`; `14400` |

Further options:

| Option | Meaning |
|---|---|
| `PARTS` | the parts of an experiment to run: `a,b,s` (Fig. 10), `payoff,sweep` (Kleene), `budget,shift`, `topology,memory`; Fig. 9 takes `PANELS=W,B` |
| `ARMS` | a subset of the approaches of Fig. 8a and Fig. 9, e.g. `ARMS=eimer,gdyn` |
| `WALL`, `MR_WALL` | time limit per statement and per native MR statement [s] |
| `LIMIT` | selection time: a selection slower than this [s] is not repeated, and clique-grow is not run for larger k of that topology (default 300) |
| `MR_TOTAL`, `MEM_TOTAL`, `RW_TOTAL` | time limit per workload for native MR, for a strategy of the memory experiment and for an approach of the real-world experiment [s]; an approach beyond its limit is reported as not finished |
| `CATALOG` | `iceberg` or `memory`: the Trino catalog of the tables (default: `memory` for the Kleene and the real-world experiment, `iceberg` otherwise) |
| `TRINO_HEAP_GB`, `CPUSET` | heap of the Trino server (default 8) and the cores the containers are pinned to |
| `SKIP_BUILD=1`, `WITH_TEX=0` | use the existing images; build the image without LaTeX (the figures then use matplotlib's fonts) |
| `OUT_NAME` | name of the output directory under `reproducibility/out/` |

## Differences from the paper

At `SCALE=paper`, the Trino experiments differ from the paper as follows.

| Experiment | Here, on Trino | In the paper | Reason |
|---|---|---|---|
| Fig. 9 | first and last type at 1 % of the rows; window panel on 10,000 rows in 10 batches; batch panel on 25,000 and 50,000 rows in 1 to 25 batches; the prefilter computes its candidate intervals per query | first type at 10 %, last type at 1 % or 0.1 %; window panel on 50,000 rows in 50 batches; batch panel on 50,000 and 100,000 rows in up to 250 batches; the prefilter materializes its candidate intervals | on Trino the baselines' runtime per query grows steeply with the window and the table |
| Fig. 10 | GDYN without indexes | also a GDYN variant with indexes | Trino has no indexes |
| Fig. 11b, storage budget | the 12 workloads on 5,000 to 12,000 rows | on 100,000 to 200,000 rows | the queries have no window, so native MR finishes on Trino for small tables only |
| Fig. 11c, distribution shift | 10,000 rows; selectivity 0.0125 to 0.25, type rate 0.03 to 0.045 | 100,000 rows; selectivity 0.002 to 0.013, type rate 0.02 to 0.03 | as above; on the small table the stale plan degrades visibly only under a stronger shift |
| Query topology | 8,000 rows at type rate 0.02 | 50,000 rows at type rate 0.01 | no window, as above |
| Table 4, memory footprint | EIMER's strategies only | also native MR | native MR does not finish on Trino at this table size |
| Fig. 12 | time limit of 4 hours per approach and query | 48 hours | runtime of the experiment |

Notes:

- Where native MR does not finish, we compare the results of EIMER's strategies with each other.
- The generator calibrates the band on the rarest type. Fig. 9 therefore needs at least 7,072 rows at selectivity 0.02,
  and at least 44,722 rows for a type at 0.1 %.
- In Fig. 9, `SIGMAS` is the selectivity to which the generator calibrates a band of ±0.02 in both coordinates. The
  query's band is wider in longitude (±0.05), so the measured selectivity of a condition is about 0.1 for 0.05 and about
  0.05 for 0.02. The same holds for the paper's Fig. 9.
- For Fig. 9, `reference/` holds the values of our Trino runs with the type rates of this table, not of the paper's
  workloads.
- To run the window panel of Fig. 9 with the paper's type rates (first type at 10 %, last type at 1 % or 0.1 %), use the
  command below once per `LAST_RATE` (`0.01`, `0.001`). Both runs write to the same directory, and the figure then shows
  both rates. Native MR takes hours per workload on Trino; our run of this kind
  ([`results/fig9_trino_type_rates/`](results/fig9_trino_type_rates/README.md)) used `WB=10 WS=200,500,1000` and, for
  its batch panel, `PANELS=B NBS=50000 BS=1,2,5,10 BW=500` with the same rates.
  `FIRST_RATE=0.10 LAST_RATE=0.001 NW=50000 WB=50 SIGMAS=0.05 PANELS=W OUT_NAME=trino_fig9_type_rates bash reproducibility/run_trino_fig9.sh`
- The memory experiment stores the state in compressed columnar files. Its size in MB is not comparable with the paper's;
  the order of the strategies by persistent state is.

## Without containers

With the packages of `requirements.txt` installed (and, for Esper, a Java 21 runtime and `mvn package` in
`backends/esper/java`), the same experiments run directly: `bash reproducibility/docker/in_container/reproduce_duckdb.sh`
`bash reproducibility/docker/in_container/reproduce_esper.sh` and
`bash reproducibility/docker/in_container/reproduce_selection_time.sh` (output under `reproducibility/out/`). The Trino experiments run with
`bash reproducibility/docker/in_container/reproduce_trino.sh fig8` (or `fig9`, `fig10`, `kleene`, `budget_shift`,
`topology_memory`, `realworld`) against a Trino 479 server (`TRINO_SERVER`, `TRINO_CATALOG`, `TRINO_SCHEMA`). The
default catalog is `iceberg`; the containers run the Kleene and the real-world experiment with `TRINO_CATALOG=memory`.
