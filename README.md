# EIMER: Efficient Incremental Maintenance for Row Pattern Recognition with SQL MATCH_RECOGNIZE

EIMER evaluates SQL `MATCH_RECOGNIZE` pattern queries over append-only event streams *incrementally*: it decomposes the pattern into a cover of sub-pattern views, maintains each view with delta-only updates per arriving batch, and composes the answer from the views on demand. A cost-model-driven selector (`clique_grow` / `edge_cover`) chooses the cover (optionally under a storage budget *M*), and every result is verified **tuple-identical** to the engine's native `MATCH_RECOGNIZE`. The implementation targets **Trino** (tested with Trino 481).

## Requirements

- Python (version >= 3.10.9) with the pinned libraries: `pip install -r requirements.txt`(numpy, pandas, pyarrow, PyYAML, matplotlib).
- **Docker**: the easiest path; it builds Trino and runs everything (Sections *Docker demo* and *Sensitivity analysis*). Nothing else is needed.
- To run *without* Docker (Section *Running the pipeline*): a local Trino reachable at `$TRINO_SERVER` (e.g. `localhost:8080`).

## Docker demo

One command builds the pinned Trino image + the pipeline image, runs the end-to-end pipeline plus the EIMER-vs-`MATCH_RECOGNIZE`, storage-budget, and predicate-class (band, equi, band+equi) demos, prints a verdict, and writes everything down:

```
bash docker/run_demo.sh
```

Results are written to `docker/out/`. The run passes iff every EIMER result is tuple-identical to native `MATCH_RECOGNIZE` and the storage budget behaves as specified.

## Running the pipeline

The config runner takes an experiment file and an environment file and performs one run:
generate data &rarr; select the cover &rarr; maintain the subquery views &rarr; execute &rarr; verify.

```
export TRINO_SERVER=localhost:8080
python3 runner/run.py \
    --experiment demo/configs/demo_experiment.yaml \
    --environment demo/configs/demo_environment.yaml \
    --out-dir out/pipeline
```

Outputs (`out/pipeline/`): `candidate_rows.csv` (per-plan predicted vs. measured cost), `experiment_manifest.json` (every seed + config stamped), and the emitted SQL.

**Entering your own query.** Edit `demo/configs/demo_experiment.yaml`: the pipeline builds the query from it:

- `query.pattern_length`: number of pattern variables.
- `query.topology`: dependency shape (`chain`, `star`, …).
- `query.predicate_class`: the dependent-predicate family (`spatial`).
- `data.stream`: the batch sizes `[initial, Δ1, Δ2, …]` (their sum is the table size *N*).
- `execution.selectivity_mode`, `data.seed`, `band_tightness`: estimator, RNG seed, predicate tightness.

`demo/configs/demo_environment.yaml` selects the Trino catalog (`memory` needs no external storage) and connection.

## Sensitivity analysis

A one-at-a-time sweep of `clique_grow` vs. native `MATCH_RECOGNIZE` around a baseline cell, over four axes (pattern length *k*, table size *N*, batch count *B*, selectivity *\sigma*). Each cell selects its cover from selectivities estimated on the loaded data (`b_unified`);
every arm runs an untimed warm-up pass before the timed one. One command builds the images, runs the sweep in the container, plots it, and tears down:

```
bash docker/run_small_sensitivity_analysis.sh
```

Outputs (`docker/out/sensitivity/`): `results.json`, `sensitivity.png`, `sensitivity.pdf`.
The exit code is 0 iff every executed cell's tuple-identity correctness gate passed.

### Parameters

Change these via environment variables, e.g.
`KS=3,4 NS=2000,4000 WALL=300 bash docker/run_small_sensitivity_analysis.sh`.

- `KS=3,4,5`: pattern-length axis values (chain specs exist for k = 3...8).
- `NS=1000,2500,5000,10000`: table-size axis values.
- `BS=2,5,10`: batch-count axis values (at the baseline *N*).
- `SIGMAS=0.01,0.05,0.20`: dependent-selectivity axis values.
- `BASELINE_K=4 BASELINE_N=5000 BASELINE_B=5 BASELINE_SIGMA=0.05`: the shared baseline cell.
- `WALL=600`: per-query wall in seconds; an arm exceeding it counts as DNF (plotted as such).
- `TRINO_MEM=6g`: memory limit for the Trino container.
- `KEEP_UP=1`: leave the Trino container running afterwards.
