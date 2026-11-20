# EIMER: Efficient Incremental Maintenance for Row Pattern Recognition with SQL MATCH_RECOGNIZE

EIMER evaluates SQL `MATCH_RECOGNIZE` (MR) queries over append-only tables incrementally. It maintains materialized views over subqueries of the pattern, updates them with the rows of each new batch, and composes the query result from the views. A cost model selects the set of views, the materialization strategy (`clique_grow`, or its seed `edge_cover`), optionally under a storage budget *M*. Wherever native MR finishes, we verify that EIMER returns the same tuples. The code generates SQL for **Trino** and **DuckDB** (Section *DuckDB backend*) and EPL for **Esper** (`backends/esper/`). The demo uses Trino 481, the paper's experiments Trino 479.

## Requirements

- **With Docker:** nothing else. The demo and all experiments build their own images. The scripts need Linux with bash 4.4 or newer; the experiments also run with Podman.
- **Without containers:** Python >= 3.10.9 with the pinned packages (`pip install -r requirements.txt`) and a Trino server reachable at `$TRINO_SERVER` (e.g. `localhost:8080`). The Esper experiment also needs Java 21 and Maven.

## Docker demo

One command builds the Trino image and the pipeline image and runs four demos: the pipeline end to end, EIMER against native MR, the storage budget, and three kinds of dependent conditions (band, equality, both).

```
bash docker/run_demo.sh
```

The results are in `docker/out/`. The run passes if every EIMER result has the same tuples as native MR, and if every strategy selected under a budget *M* is predicted to fit *M* and returns the same number of results.

## Experiment reproducibility

The paper evaluates EIMER on four engines: DBMS A, Trino, DuckDB and Esper. DBMS A is a commercial system that we cannot distribute. We therefore reproduce the experiments on the three open engines, in containers, with one script per experiment. The Trino series of Fig. 8a, Fig. 8b and Fig. 8c run on the paper's engines. The experiments that the paper reports on DBMS A run on Trino here: Figs. 9 to 12, Table 4, and the results of §7.4 that have no figure.

Trino is much slower than DBMS A on these workloads; the paper's Fig. 8a shows both on the same data. On Trino we therefore use smaller tables and, in some experiments, other parameters. [`reproducibility/README.md`](reproducibility/README.md) lists the differences. Runtimes and speed-ups therefore differ from the paper's numbers, while the comparison between the approaches carries over.

| Paper | Experiment | Script | Queries under `evaluation_queries/` |
|---|---|---|---|
| §7.2, Fig. 8a | Feasibility across engines (Trino series) | `reproducibility/run_trino_fig8.sh` | `7.2_state_of_the_art/feasibility_across_engines/` |
| §7.2, Fig. 8b | MR-to-SQL transpilation (DuckDB) | `reproducibility/run_duckdb.sh` | `7.2_state_of_the_art/mr_to_sql_transpilation/` |
| §7.2, Fig. 8c | CEP engine (Esper) | `reproducibility/run_esper.sh` | `7.2_state_of_the_art/cep_engine/` |
| §7.2, Fig. 9 | Prefilter acceleration (on Trino) | `reproducibility/run_trino_fig9.sh` | `7.2_state_of_the_art/prefilter_acceleration/` |
| §7.3, Tables 2 and 3 | Cost model and pick quality, ablation, robustness to statistics | (measured on DBMS A; queries only) | `7.3_plan_selection/cost_model_and_pick_quality/` |
| §7.3 | Selection time (no engine) | `reproducibility/run_selection_time.sh` | `7.3_plan_selection/selection_time/` |
| §7.4, Fig. 10 | Dynamic query evaluation: EIMER and GDYN (on Trino) | `reproducibility/run_trino_fig10.sh` | `7.4_sensitivity/dynamic_query_evaluation/` |
| §7.4 | Skewed type frequencies (on Trino) | `reproducibility/run_trino_fig10.sh` (its part `s`) | `7.4_sensitivity/skewed_type_frequencies/` |
| §7.4 | Query topology (on Trino) | `reproducibility/run_trino_topology_memory.sh` | `7.4_sensitivity/query_topology/` |
| §7.4, Fig. 11a | Kleene patterns (on Trino) | `reproducibility/run_trino_kleene.sh` | `7.4_sensitivity/kleene_patterns/` |
| §7.4, Fig. 11b | Storage budget (on Trino) | `reproducibility/run_trino_budget_shift.sh` | `7.4_sensitivity/storage_budget/` |
| §7.4, Table 4 | Memory footprint (on Trino, EIMER's strategies) | `reproducibility/run_trino_topology_memory.sh` | `7.4_sensitivity/memory_footprint/` |
| §7.4, Fig. 11c | Distribution shift (on Trino) | `reproducibility/run_trino_budget_shift.sh` | `7.4_sensitivity/distribution_shift/` |
| §7.5, Fig. 12 | Real-world tables (on Trino) | `reproducibility/run_trino_realworld.sh` | `7.5_real_world/{chicago,ecommerce,taxi}/` |

`bash reproducibility/run_all.sh` runs all scripts one after the other. Each script writes one record per workload, a summary table with the checks, and the figure to `reproducibility/out/<experiment>/`. By default, the experiments run on small tables; all ten scripts together take about two and three quarter hours. `SCALE=paper` sets the parameters of our runs, which take hours to days. The figures of these runs are in [`reproducibility/results/`](reproducibility/results/README.md).

The baselines are in `reproducibility/experiments/`:

- GDYN: `trino/gdyn.py`
- the join-based prefilter: `trino/prefilter.py`
- the transpiled queries: `duckdb/queries/`, produced by `duckdb/transpile.py`
- Esper's native MR: `esper/run_cells.py`

[`reproducibility/README.md`](reproducibility/README.md) gives the parameters of every experiment and explains how to run it without containers.

## Repository layout

| Directory | Content |
|---|---|
| `eimer/` | the implementation: query model, plans, cost model, plan selection and SQL generation (next section) |
| `backends/` | the engine-specific parts for Trino, DuckDB and Esper |
| `evaluation_queries/` | every query of the paper's evaluation as SQL, specification and query graph |
| `reproducibility/` | the paper's experiments: scripts, containers, baselines, real-world tables and the results of our runs |
| `demo/`, `execution/`, `workload/` | the data generator, the statistics and the execution of a plan on an engine; used by the demos and by the experiments |
| `docker/`, `runner/`, `examples/` | the demos (Sections *Docker demo*, *Running the pipeline* and *Sensitivity analysis*), which are not experiments of the paper |
| `EIMER_appendix.pdf` | the appendix that the paper refers to |

## From the paper to the code

The table maps the sections of the paper to the source files.

| Paper | Concept | Source files (entry points) |
|---|---|---|
| §2.1 Data Model | append-only table, batches, query schedule | `eimer/workload.py`, `eimer/query/query_schedule.py` |
| §2.2 Query Model | subsequence pattern `V1 Z* V2 … Z* Vn`, independent and dependent conditions | `eimer/query/parser.py` (the `MATCH_RECOGNIZE` front end), `eimer/query/query_spec.py` (`QuerySpec`), `eimer/models.py` |
| §3 Incremental Pattern Recognition | from a query to the SQL of an evaluation plan, end to end | `eimer/pipeline.py`, `eimer/cli.py` |
| §4.1 Subqueries and Materialized Views | valid subqueries | `eimer/plans/subpattern_enumeration.py`, `eimer/plans/strategy_space.py` (`enumerate_valid_subpatterns`) |
| §4.2 Materialization Strategies | strategies as sets of views | `eimer/plans/strategy_space.py` (`enumerate_strategies`) |
| §4.3.1 Update Phase | Lemma 1 (last assignment): the new matches of a view are those whose last variable is assigned a row of the batch | `eimer/plans/update_plans.py`, `eimer/sql/sql_emitter.py` (`generate_update_sql_statements`) |
| §4.3.2 Query Phase | composition of the views and the post-filter | `eimer/plans/composition.py`, `eimer/sql/sql_emitter.py` (`generate_composition_tree_sql`, `generate_post_filter_sql`) |
| §4.3.3 Incremental Kleene Stitching | a Kleene view maintained from the run left open at the last batch boundary | `eimer/sql/sql_emitter.py` (`_mr_group_detect_update_sql`), `demo/eimer_arm.py` (`eimer_delta_streaming`, `kleene="stitch"`) |
| §5.1 Join Graphs | dependency graph of the query, join graph over a set of views | `eimer/query/dependency_graph.py`, `eimer/plans/composition.py` (`build_composition_join_graph_for_nodes`) |
| §5.2 Composition and Update Plans | join trees of both phases | `eimer/plans/composition.py` (`enumerate_composition_plans`), `eimer/plans/update_plans.py` (`enumerate_update_plans`, `construct_bushy_update_plan`), `eimer/selection/join_order.py` |
| §5.3 Evaluation Plans | a strategy with its update and composition plans, lowered to operators | `eimer/plans/evaluation_plan.py` (`EvaluationPlan`), `eimer/plans/executable_plan.py`, `eimer/plans/predicate_ledger.py` |
| §5.4 Cost Model | cardinalities and cost of update, composition and post-filter | `eimer/cost/cost_model.py` (`update_op_cost`, `compose_cost`, `postfilter_cost`, `plan_cost`), `eimer/cost/cost_estimation.py` |
| §5.4, §7.1 Statistics | selectivities estimated from single-pass table statistics | `execution/mode_b_estimator/`, `execution/compute_selectivities.py` |
| §6.1 The Materialization Strategy Objective | the score Φ of a strategy; the weight κ | `eimer/selection/plan_selection_clique_grow.py` (`_total_cost`, `KAPPA_DEFAULT`) |
| §6.2 The Clique-Grow Search | Algorithm 1; its seed, the edge cover; the storage budget M | `eimer/selection/plan_selection_clique_grow.py` (`select_clique_grow_plan`, whose parameter `max_storage_bytes` is the budget M; `_hill_climb`), `eimer/selection/plan_selection_edge_cover.py` (the edge cover as a strategy of its own), `eimer/selection/state_size_trino.py` (predicted state size), `eimer/selection/plan_build.py` |
| §7.1 Experimental Setup | SQL per engine | `eimer/sql/`, `backends/trino/`, `backends/duckdb/`, `backends/esper/` |

The extensions of §4.4 are described in the paper and are not part of the code.

## Running the pipeline

The runner takes an experiment file and an environment file and performs one run:
generate data &rarr; select the strategy &rarr; maintain the views &rarr; execute &rarr; verify.

```
export TRINO_SERVER=localhost:8080
python3 runner/run.py \
    --experiment demo/configs/demo_experiment.yaml \
    --environment demo/configs/demo_environment.yaml \
    --out-dir out/pipeline
```

The run writes to `out/pipeline/`: `candidate_rows.csv` with the predicted and the measured cost of each plan, `experiment_manifest.json` with all seeds and settings, and the generated SQL.

**Your own query.** The pipeline builds the query from `demo/configs/demo_experiment.yaml`. Edit these fields:

- `query.pattern_length`: number of pattern variables.
- `query.topology`: topology of the dependent conditions (`chain`, `star`, …).
- `query.predicate_class`: the kind of dependent conditions (`spatial`).
- `data.stream`: the batch sizes `[initial, Δ1, Δ2, …]` (their sum is the table size *N*).
- `execution.selectivity_mode`, `data.seed`, `band_tightness`: estimator, RNG seed, predicate tightness.

`demo/configs/demo_environment.yaml` selects the Trino catalog (`memory` needs no external storage) and the connection.

## DuckDB backend

EIMER's SQL compiler also targets DuckDB (`--target-dialect duckdb`; requires `duckdb==1.5.5`, pinned in `requirements.txt`):

```
python3 demo/run_eimer_vs_mr.py --engine duckdb --n 5000 --k 4 --batches 5 \
    --baseline-sql my_baseline.sql --out-dir out/demo_eimer_vs_mr_duckdb
```

DuckDB has no native `MATCH_RECOGNIZE`. Kleene queries, whose views EIMER maintains with MR, are therefore rejected, and the baseline is a SQL file that you supply with `--baseline-sql`: a `SELECT` over the relation `events_window`.

## Sensitivity analysis

This demo is not one of the paper's experiments. It varies one parameter at a time (pattern length, table size, number of batches, selectivity) and compares EIMER with native MR on Trino.

```
bash docker/run_small_sensitivity_analysis.sh
```

It writes `results.json` and `sensitivity.png` to `docker/out/sensitivity/`. The axis values and limits are environment variables (`KS`, `NS`, `BS`, `SIGMAS`, `WALL`, ...); the header of the script lists them.
