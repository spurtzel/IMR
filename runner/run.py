"""Single-run orchestrator: experiment.yaml + environment.yaml -> one run.

    python3 runner/run.py \
        --experiment experiments/example_experiment.yaml \
        --environment experiments/example_environment.yaml \
        --out-dir <per-experiment output dir>

Flow: VALIDATE (reject illegal combos, naming the violated constraint) -> MATERIALIZE
(spec + dataset) -> ORCHESTRATE (load -> sigma -> select -> execute the rank-1 pick) ->
COLLECT (experiment_manifest.json + candidate_rows.csv). Config is scalar; a list value
is a grid axis (run_grid.py).

Backends ,,trino'' and ,,memory'' (Trino's in-RAM catalog) both run the full spine. The
backend is REQUIRED: a missing one fails loud in validation, never a silent fallback.

Every seed used is stamped into the manifest (the datagen seed is drawn and recorded when
the config omits it).

The expensive captures (QEP / correctness-vs-MR / mode-c ground truth) are opt-in;
requesting them fails loud rather than silently no-oping.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "execution"))
import bootstrap  # noqa: F401,E402

from runner.materialize import draw_seed, materialize_dataset, write_query_spec  # noqa: E402
from runner.schema import (  # noqa: E402
    ConfigError, EnvironmentConfig, ExperimentConfig, load_environment, load_experiment)
from runner.validate import validate_environment, validate_experiment  # noqa: E402

# Curation bounds for the strategy-catalog build on n>=4 specs: cap catalog SIZE,
# not N-id numbering. Overridable via execution.caps.
DEFAULT_CAPS = {"EIMER_CURATED_MAX_STRATEGIES": "16", "EIMER_CURATED_MAX_VIEWS": "4"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run(cmd: list[str], log_path: Path, extra_env: dict | None = None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n+ " + " ".join(str(c) for c in cmd) + "\n")
        log.flush()
        proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, stdout=log,
                              stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"step failed (rc={proc.returncode}): {' '.join(cmd[:3])}... "
                           f"see {log_path}")


def _trino_env(env: EnvironmentConfig) -> dict[str, str]:
    """Explicit Trino connection: config values first, ambient TRINO_* second, and a
    hard failure if neither provides a value (no silent memory@localhost)."""
    conn = env.connection
    catalog = "memory" if env.backend == "memory" else conn.get("catalog") or os.environ.get("TRINO_CATALOG")
    values = {
        "TRINO_SERVER": conn.get("server") or os.environ.get("TRINO_SERVER"),
        "TRINO_CATALOG": catalog,
        "TRINO_SCHEMA": conn.get("schema") or os.environ.get("TRINO_SCHEMA"),
        "TRINO_USER": conn.get("user") or os.environ.get("TRINO_USER"),
    }
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise ConfigError(f"backend {env.backend!r}: no value for {', '.join(missing)}, "
                          "set environment.connection.{server,catalog,schema,user} or the "
                          "corresponding env vars (there is no silent default)")
    return values


# --------------------------------------------------------------------------- #
# Backend arms
# --------------------------------------------------------------------------- #

def _execute_trino(exp: ExperimentConfig, env: EnvironmentConfig, out: Path,
                   spec_path: Path, manifest_path: Path, workload_seed: int,
                   caps_env: dict[str, str]) -> dict:
    trino_env = _trino_env(env)
    logs = out / "logs"
    stream = exp.data.stream
    # data.stream IS the execution schedule: forwarded verbatim as --batch-sizes to
    # both select and execution, so the cost model prices exactly the schedule that runs.
    stream_csv = ",".join(str(size) for size in stream)
    total_events, batches = sum(stream), len(stream)
    fingerprint = json.loads(manifest_path.read_text(encoding="utf-8"))["fingerprint"]

    # load (idempotent on fingerprint match)
    _run([sys.executable, "execution/load_external_events.py", "--manifest",
          str(manifest_path)], logs / "load.log", extra_env=trino_env)

    # sigma on the LOADED table, bound to the dataset fingerprint
    sigma_path = out / f"selectivities_{exp.execution.selectivity_mode}.json"
    _run([sys.executable, "execution/compute_selectivities.py",
          "--mode", exp.execution.selectivity_mode,
          "--query-spec-file", str(spec_path),
          "--events-table", "generated_events",
          "--external-manifest-fingerprint", fingerprint,
          "--output", str(sigma_path)], logs / "sigma.log", extra_env=trino_env)

    # select: pick the cover with the configured selector (clique_grow / edge_cover)
    rank1 = out / "rank1.jsonl"
    _run([sys.executable, "execution/select_cover.py",
          "--query-spec-file", str(spec_path),
          "--selector", exp.execution.selector,
          "--selectivities", str(sigma_path),
          "--total-events", str(total_events), "--batches", str(batches),
          "--batch-sizes", stream_csv,
          "--descriptor-out", str(rank1)],
         logs / "select.log", extra_env={**trino_env, **caps_env})
    # single-pick selection: the descriptor is the rank-1 pick (no ranked shortlist)
    shortlist_csv = shortlist_jsonl = rank1

    # execute the pick on the loaded dataset; opt-in captures ride the same call
    # (--with-correctness = per-batch native-MR full-tuple diff)
    exec_dir = out / "execution"
    cmd = [sys.executable, "execution/run_selected_plan_manifest.py",
           "--plan-descriptor-manifest", str(rank1),
           "--query-spec-file", str(spec_path),
           "--external-events-manifest", str(manifest_path),
           "--selectivity-mode", exp.execution.selectivity_mode,
           "--selectivities", str(sigma_path),
           "--total-events", str(total_events), "--batches", str(batches),
           "--batch-sizes", stream_csv,
           "--workload-seed", str(workload_seed),
           "--trino-join-reordering", "none",
           "--max-descriptors", "1", "--out-dir", str(exec_dir)]
    if exp.output.with_correctness:
        cmd += ["--with-correctness"]
    if exp.output.with_qep:
        # one opt-in dumps both: the static distributed plan and EXPLAIN ANALYZE
        cmd += ["--dump-qep", "--dump-explain-analyze"]
    _run(cmd, logs / "execute.log", extra_env={**trino_env, **caps_env})

    summary_csv = exec_dir / "selected_manifest_benchmark_summary.csv"
    rows = list(csv.DictReader(summary_csv.open(encoding="utf-8")))
    captures = collect_trino_captures(rows, exec_dir,
                                      with_qep=exp.output.with_qep,
                                      with_correctness=exp.output.with_correctness)
    return {"sigma_path": str(sigma_path), "shortlist_csv": str(shortlist_csv),
            "shortlist_jsonl": str(shortlist_jsonl), "execution_dir": str(exec_dir),
            "captures": captures, "summary_rows": rows}


def collect_trino_captures(rows: list[dict], exec_dir: Path, *, with_qep: bool,
                           with_correctness: bool) -> dict:
    """Add the Trino-side capture columns to the summary rows and build the record's
    ,,captures'' section. Memory peaks are captured by default; QEP/correctness only when
    flagged."""
    captures: dict = {}
    for row in rows:
        run_dir = Path(row.get("run_dir") or exec_dir)
        peaks_user, peaks_total = [], []
        for mem_csv in run_dir.glob("run*_memory_query_summary.csv"):
            for mem_row in csv.DictReader(mem_csv.open(encoding="utf-8")):
                if mem_row.get("peak_user_memory_bytes"):
                    peaks_user.append(int(mem_row["peak_user_memory_bytes"]))
                if mem_row.get("peak_total_memory_bytes"):
                    peaks_total.append(int(mem_row["peak_total_memory_bytes"]))
        if peaks_user:
            row["memory_peak_user_bytes"] = max(peaks_user)
        if peaks_total:
            row["memory_peak_total_bytes"] = max(peaks_total)
        if with_qep:
            qep_dirs = sorted(str(d) for d in run_dir.glob("run*_qep")) + \
                       sorted(str(d) for d in run_dir.glob("run*_explain_analyze"))
            if qep_dirs:
                row["qep_path"] = qep_dirs[0].rsplit("/run", 1)[0]
                captures["qep"] = qep_dirs
    if with_correctness and rows:
        captures["correctness"] = {
            "mechanism": "trino per-batch native-MR full-tuple multiset diff "
                         "(the Gate B renderer)",
            "status": rows[0].get("correctness_status"),
            "detail_path": rows[0].get("correctness_path")}
    return captures


# --------------------------------------------------------------------------- #
# The unified per-experiment record
# --------------------------------------------------------------------------- #

def _achieved_sigma(manifest_path: Path) -> dict:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    verification = data.get("verification", {})
    return {
        "independent": {r["label"].split(":")[0].strip(): round(r["rate"], 6)
                        for r in verification.get("independents", [])},
        "edges": {e["name"]: {"sigma": round(e["sigma"], 6),
                              "target_sigma": e.get("target_sigma")}
                  for e in verification.get("edges", [])},
    }


def _write_record(out: Path, exp_path: Path, env_path: Path, exp: ExperimentConfig,
                  env: EnvironmentConfig, spec_path: Path, manifest_path: Path,
                  datagen_seed: int, seed_was_drawn: bool, workload_seed: int,
                  result: dict) -> None:
    dataset = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = {
        "created_utc": _utc_now(),
        "config": {"experiment_file": str(exp_path), "environment_file": str(env_path),
                   "experiment": json.loads(json.dumps(exp, default=lambda o: o.__dict__)),
                   "backend": env.backend, "parallelism": env.parallelism.__dict__},
        "query_spec": {"path": str(spec_path),
                       "name": json.loads(spec_path.read_text(encoding="utf-8"))["name"]},
        "dataset": {"manifest": str(manifest_path), "fingerprint": dataset["fingerprint"],
                    "total_rows": dataset["total_rows"],
                    "batch_sizes": dataset["batch_sizes"]},
        # every seed used, always recorded:
        "seeds": {"datagen_seed": datagen_seed, "datagen_seed_was_drawn": seed_was_drawn,
                  "workload_seed": workload_seed},
        "achieved_sigma": _achieved_sigma(manifest_path),
        "selectivity_mode": exp.execution.selectivity_mode,
        # opt-in captures actually taken this run (absent = cheap default path):
        "captures": result.get("captures", {}),
        "capture_flags": {"with_qep": exp.output.with_qep,
                          "with_correctness": exp.output.with_correctness,
                          "selectivity_mode_c": exp.execution.selectivity_mode == "c"},
        "results": {k: v for k, v in result.items() if k != "summary_rows"},
    }
    (out / "experiment_manifest.json").write_text(json.dumps(manifest, indent=2),
                                                  encoding="utf-8")

    # the core per-candidate row(s). Capture columns are non-empty only when their flag
    # was on; memory columns are backend-dependent (empty = the backend does not offer it).
    rows = result.get("summary_rows") or []
    if rows:
        keep = ["selector_rank", "strategy_id", "ranking_profile_name", "ranking_score",
                "estimated_total_cost", "estimated_update_cost", "estimated_compose_cost",
                "estimated_post_filter_cost", "measured_total_ms", "measured_update_ms",
                "measured_compose_ms", "measured_post_filter_ms", "output_row_count",
                "distinct_anchor_count", "memory_materialized_bytes",
                "memory_peak_workarea_bytes", "memory_peak_tempseg_bytes",
                "memory_peak_user_bytes", "memory_peak_total_bytes",
                "correctness_status", "qep_path", "status", "run_dir"]
        with (out / "candidate_rows.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keep, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)


def run_experiment(exp_path: Path, env_path: Path, out: Path) -> int:
    exp = load_experiment(exp_path)
    env = load_environment(env_path)
    validate_environment(env)
    validate_experiment(exp)

    if exp.execution.query_frequency != "all":
        raise ConfigError(f"execution.query_frequency {exp.execution.query_frequency!r} "
                          "is not supported; trino/memory run the 'all' schedule")

    out.mkdir(parents=True, exist_ok=True)
    # caps keys are whitelist-validated in validate_experiment: forward them all.
    caps_env = dict(DEFAULT_CAPS)
    caps_env.update({k: str(v) for k, v in exp.execution.caps.items()})

    seed_was_drawn = exp.data.seed is None
    datagen_seed = draw_seed() if seed_was_drawn else exp.data.seed
    workload_seed = 0  # scalar default; forwarded to the executor AND stamped

    print(f"[1/5] materialize query spec ...", flush=True)
    spec_path = out / "query_spec.json"
    write_query_spec(exp, spec_path)
    print(f"[2/5] materialize dataset (seed={datagen_seed}"
          f"{' drawn' if seed_was_drawn else ''}) ...", flush=True)
    manifest_path = materialize_dataset(exp, spec_path, out / "dataset", seed=datagen_seed)

    print(f"[3-5/5] load -> sigma({exp.execution.selectivity_mode}) -> select -> "
          f"execute on backend {env.backend!r} ...", flush=True)
    result = _execute_trino(exp, env, out, spec_path, manifest_path,
                            workload_seed, caps_env)

    _write_record(out, exp_path, env_path, exp, env, spec_path, manifest_path,
                  datagen_seed, seed_was_drawn, workload_seed, result)
    rows = result.get("summary_rows") or []
    status = (rows[0].get("status") or "?") if rows else "?"
    print(f"done: status={status}; record -> {out / 'experiment_manifest.json'}", flush=True)
    return 0 if status.lower() == "ok" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--out-dir", "--out", dest="out_dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run_experiment(args.experiment, args.environment, args.out_dir)
    except ConfigError as exc:
        print(f"INVALID CONFIG: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
