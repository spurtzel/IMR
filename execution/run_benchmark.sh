#!/usr/bin/env bash
set -euo pipefail

# Runs `benchmark.sql` statement-by-statement over Trino's HTTP API and measures
# wall-clock time per statement.
# Repeats the whole benchmark RUNS times and writes per-run + averaged summaries.
#
# Optional parameterized SQL generation:
#   This repo can generate a benchmark SQL on-the-fly via `python3 execution/cli.py emit-sql`.
#   If you pass any of the sizing flags below, the script will generate
#   `$OUT_DIR/benchmark_generated.sql` and benchmark that.
#
#   Flags:
#     --initial-history N   (default: 10000)
#     --updates N           (default: 5)
#     --update-size N       (default: 2000)
#     --scale F             (default: disabled; scales update size each batch)
#     --sel-r F             (default: disabled; ROBBERY selectivity in [0,1])
#     --sel-b F             (default: disabled; BATTERY selectivity in [0,1])
#     --sel-m F             (default: disabled; MOTOR VEHICLE THEFT selectivity in [0,1])
#     --workload-profile P  (default: deterministic_grid_default)
#     --workload-seed N     (default: 0)
#     --query-spec NAME     (default: canonical_r_b_m)
#     --query-spec-file PATH (external restricted QuerySpec JSON)
#     --strategies CSV      (default: S0,S1,S2,S3,S4; e.g. S1,S2,S3,S4)
#     --no-generate         (force using the provided SQL file as-is)
#     --with-correctness    (include tuple-level correctness checks)
#     --query-batches CSV   (1-based query batches; 'none' for update-only)
#     --correctness-total-events N (cap generated correctness data at <=5000)
#     --MR-postprocessing   (use MATCH_RECOGNIZE-based post-processing for S2/S4)
#     --dump-qep            (dump EXPLAIN distributed JSON plans for update/compose statements)
#     --cost-model-validation  (enable per-operator cost-model validation mode)
#     --selectivities PATH     (JSON file required by --cost-model-validation)
#     --selectivity-mode b|c   (artifact label for selectivities payload)
#     --trino-join-reordering none (SET SESSION join_reordering_strategy='NONE')
#     --trino-task-concurrency N   (SET SESSION task_concurrency=N; default 1 = no
#                              intra-query parallelism; 'default' = engine default)
#     --external-events-manifest PATH (or env EXTERNAL_EVENTS_MANIFEST: benchmark a
#                              preloaded external dataset: skips the in-Trino CTAS,
#                              runs execution/load_external_events.py before the stream)
#
# Output directory:
#   results_bench/<timestamp>/
#     run1_trino.log, run1_timings.csv, run1_timings_summary.csv
#     run1_memory_query_summary.csv
#     run1_memory_state.csv, run1_memory_state_summary.csv
#     run1_bench_results.csv, run1_correctness_check.txt
#     run2_...
#     run3_...
#     timings_summary_avg.csv
#     memory_query_summary_avg.csv
#     memory_state_summary_avg.csv
#     timings_totals_avg.csv

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCHMARK_SQL="benchmark.sql"

INITIAL_HISTORY="${INITIAL_HISTORY:-10000}"
UPDATES="${UPDATES:-5}"
UPDATE_SIZE="${UPDATE_SIZE:-2000}"
WARMUP_RUNS="${WARMUP_RUNS:-1}"
WITH_CORRECTNESS=0
MR_POSTPROCESSING=0
SCALE_FACTOR=""
BATCH_SIZES=""
SEL_R=""
SEL_B=""
SEL_M=""
WORKLOAD_PROFILE="${WORKLOAD_PROFILE:-deterministic_grid_default}"
WORKLOAD_SEED="${WORKLOAD_SEED:-0}"
QUERY_SPEC="${QUERY_SPEC:-canonical_r_b_m}"
QUERY_SPEC_FILE="${QUERY_SPEC_FILE:-}"
STRATEGIES=""
EXPLICIT_COVER=""
COMPOSITION_PLAN_IDX="${COMPOSITION_PLAN_IDX:-0}"
COMPOSITION_VARIANT_IDX="${COMPOSITION_VARIANT_IDX:-}"
COMPOSITION_VARIANT_MODE="${COMPOSITION_VARIANT_MODE:-canonical}"
MAX_JOIN_ORDERS_PER_TREE="${MAX_JOIN_ORDERS_PER_TREE:-}"
UPDATE_PLAN_IDX="${UPDATE_PLAN_IDX:-0}"
QUERY_BATCHES="${QUERY_BATCHES:-}"
CORRECTNESS_TOTAL_EVENTS="${CORRECTNESS_TOTAL_EVENTS:-}"
DUMP_QEP=0
DUMP_EXPLAIN_ANALYZE=0
COST_MODEL_VALIDATION=0
SELECTIVITIES=""
SELECTIVITY_MODE=""
TRINO_JOIN_REORDERING="${TRINO_JOIN_REORDERING:-default}"
# Intra-query parallelism pin: 1 (default) sets SET SESSION task_concurrency = 1,
# disabling intra-query operator parallelism; "default" leaves the engine default.
TRINO_TASK_CONCURRENCY="${TRINO_TASK_CONCURRENCY:-1}"
EXTERNAL_EVENTS_MANIFEST="${EXTERNAL_EVENTS_MANIFEST:-}"

GENERATE_SQL=0
NO_GENERATE=0

if [[ $# -gt 0 && "${1:-}" != --* ]]; then
  BENCHMARK_SQL="$1"
  shift
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --initial-history)
      INITIAL_HISTORY="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --updates)
      UPDATES="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --update-size)
      UPDATE_SIZE="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --scale)
      SCALE_FACTOR="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --batch-sizes)
      BATCH_SIZES="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --sel-r)
      SEL_R="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --sel-b)
      SEL_B="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --sel-m)
      SEL_M="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --workload-profile)
      WORKLOAD_PROFILE="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --workload-seed)
      WORKLOAD_SEED="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --query-spec)
      QUERY_SPEC="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --query-spec-file)
      QUERY_SPEC_FILE="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --strategies)
      STRATEGIES="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --explicit-cover)
      EXPLICIT_COVER="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --composition-plan-idx)
      COMPOSITION_PLAN_IDX="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --composition-variant-idx)
      COMPOSITION_VARIANT_IDX="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --composition-variant-mode)
      COMPOSITION_VARIANT_MODE="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --max-join-orders-per-tree)
      MAX_JOIN_ORDERS_PER_TREE="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --update-plan-idx)
      UPDATE_PLAN_IDX="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --query-batches)
      QUERY_BATCHES="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --selectivity-mode)
      SELECTIVITY_MODE="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --trino-join-reordering)
      TRINO_JOIN_REORDERING="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --trino-task-concurrency)
      TRINO_TASK_CONCURRENCY="$2"
      shift 2
      ;;
    --correctness-total-events)
      CORRECTNESS_TOTAL_EVENTS="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    --with-correctness)
      WITH_CORRECTNESS=1
      GENERATE_SQL=1
      shift
      ;;
    --MR-postprocessing)
      MR_POSTPROCESSING=1
      GENERATE_SQL=1
      shift
      ;;
    --no-generate)
      NO_GENERATE=1
      shift
      ;;
    --warmup)
      WARMUP_RUNS="$2"
      shift 2
      ;;
    --dump-qep)
      DUMP_QEP=1
      shift
      ;;
    --dump-explain-analyze)
      DUMP_EXPLAIN_ANALYZE=1
      shift
      ;;
    --cost-model-validation)
      COST_MODEL_VALIDATION=1
      GENERATE_SQL=1
      shift
      ;;
    --selectivities)
      SELECTIVITIES="$2"
      shift 2
      ;;
    --external-events-manifest)
      EXTERNAL_EVENTS_MANIFEST="$2"
      GENERATE_SQL=1
      shift 2
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -n "$SEL_R" || -n "$SEL_B" || -n "$SEL_M" ]]; then
  if [[ -z "$SEL_R" || -z "$SEL_B" || -z "$SEL_M" ]]; then
    echo "Selectivity args must be provided together: --sel-r, --sel-b, --sel-m" >&2
    exit 2
  fi
fi

if [[ "$COST_MODEL_VALIDATION" -eq 1 ]]; then
  if [[ "$MR_POSTPROCESSING" -eq 1 ]]; then
    echo "--cost-model-validation is incompatible with --MR-postprocessing" >&2
    exit 2
  fi
  if [[ "$NO_GENERATE" -eq 1 || "$GENERATE_SQL" -ne 1 ]]; then
    echo "--cost-model-validation requires benchmark SQL generation; do not use --no-generate or a prewritten SQL file" >&2
    exit 2
  fi
  if [[ -z "$SELECTIVITIES" ]]; then
    echo "--cost-model-validation requires --selectivities <path>" >&2
    exit 2
  fi
  if [[ ! -f "$SELECTIVITIES" ]]; then
    echo "selectivities file not found: $SELECTIVITIES" >&2
    exit 2
  fi
fi

if [[ -n "$EXTERNAL_EVENTS_MANIFEST" ]]; then
  # A prewritten benchmark.sql still contains the in-Trino generated_events
  # CTAS and would silently clobber the loaded external table.
  if [[ "$NO_GENERATE" -eq 1 || "$GENERATE_SQL" -ne 1 ]]; then
    echo "EXTERNAL_EVENTS_MANIFEST requires benchmark SQL generation; do not use --no-generate or a prewritten SQL file" >&2
    exit 2
  fi
  if [[ ! -f "$EXTERNAL_EVENTS_MANIFEST" ]]; then
    echo "external events manifest not found: $EXTERNAL_EVENTS_MANIFEST" >&2
    exit 2
  fi
fi

TRINO_SERVER="${TRINO_SERVER:-localhost:8080}"
TRINO_CATALOG="${TRINO_CATALOG:-memory}"
TRINO_SCHEMA="${TRINO_SCHEMA:-default}"
TRINO_USER="${TRINO_USER:-${USER:-trino}}"
TRINO_HTTP_SOURCE="${TRINO_HTTP_SOURCE:-benchmark_runner}"
TRINO_HTTP_TIMEOUT_SECONDS="${TRINO_HTTP_TIMEOUT_SECONDS:-3600}"

RUNS="${RUNS:-3}"

OUT_ROOT="${OUT_ROOT:-execution/results_bench}"
if [[ -z "${OUT_DIR:-}" ]]; then
  ts="$(date +%Y%m%d_%H%M%S)"
  param_tag="history${INITIAL_HISTORY}_updates${UPDATES}_batch${UPDATE_SIZE}"
  if [[ -n "$SCALE_FACTOR" ]]; then
    scale_tag="$(printf '%s' "$SCALE_FACTOR" | tr -cd '[:alnum:]_.-')"
    param_tag="${param_tag}_scale${scale_tag}"
  fi
  if [[ -n "$SEL_R" || -n "$SEL_B" || -n "$SEL_M" ]]; then
    sel_r_tag="$(printf '%s' "${SEL_R:-na}" | tr -cd '[:alnum:]_.-')"
    sel_b_tag="$(printf '%s' "${SEL_B:-na}" | tr -cd '[:alnum:]_.-')"
    sel_m_tag="$(printf '%s' "${SEL_M:-na}" | tr -cd '[:alnum:]_.-')"
    param_tag="${param_tag}_selR${sel_r_tag}_selB${sel_b_tag}_selM${sel_m_tag}"
  fi
  if [[ "$WORKLOAD_PROFILE" != "deterministic_grid_default" || "$WORKLOAD_SEED" != "0" ]]; then
    profile_tag="$(printf '%s' "$WORKLOAD_PROFILE" | tr -cd '[:alnum:]_.-')"
    seed_tag="$(printf '%s' "$WORKLOAD_SEED" | tr -cd '[:alnum:]_.-')"
    param_tag="${param_tag}_profile${profile_tag}_seed${seed_tag}"
  fi
  if [[ -n "$STRATEGIES" ]]; then
    strategies_tag="$(printf '%s' "$STRATEGIES" | tr -cd '[:alnum:]_,.-' | tr ',' '-')"
    param_tag="${param_tag}_strategies${strategies_tag}"
  fi
  param_tag="${param_tag}_correctness${WITH_CORRECTNESS}"
  param_tag="${param_tag}_mrpost${MR_POSTPROCESSING}"
  param_tag="${param_tag}_cmvalid${COST_MODEL_VALIDATION}"
  param_tag="${param_tag}_warmup${WARMUP_RUNS}_runs${RUNS}"
  OUT_DIR="$OUT_ROOT/${ts}_${param_tag}"
fi
mkdir -p "$OUT_DIR"

if [[ "$NO_GENERATE" -eq 1 ]]; then
  GENERATE_SQL=0
fi

if [[ "$GENERATE_SQL" -eq 1 ]]; then
  GEN_SQL="$OUT_DIR/benchmark_generated.sql"
  GEN_ARGS=()
  if [[ "$WITH_CORRECTNESS" -eq 1 ]]; then
    GEN_ARGS+=(--with-correctness)
  fi
  if [[ "$MR_POSTPROCESSING" -eq 1 ]]; then
    GEN_ARGS+=(--MR-postprocessing)
  fi
  if [[ -n "$SCALE_FACTOR" ]]; then
    GEN_ARGS+=(--scale "$SCALE_FACTOR")
  fi
  if [[ -n "$SEL_R" ]]; then
    GEN_ARGS+=(--sel-r "$SEL_R")
  fi
  if [[ -n "$SEL_B" ]]; then
    GEN_ARGS+=(--sel-b "$SEL_B")
  fi
  if [[ -n "$SEL_M" ]]; then
    GEN_ARGS+=(--sel-m "$SEL_M")
  fi
  GEN_ARGS+=(--workload-profile "$WORKLOAD_PROFILE")
  GEN_ARGS+=(--workload-seed "$WORKLOAD_SEED")
  if [[ -n "$QUERY_SPEC_FILE" ]]; then
    GEN_ARGS+=(--query-spec-file "$QUERY_SPEC_FILE")
  else
    GEN_ARGS+=(--query-spec "$QUERY_SPEC")
  fi
  if [[ -n "$STRATEGIES" ]]; then
    GEN_ARGS+=(--strategies "$STRATEGIES")
  fi
  if [[ -n "$EXPLICIT_COVER" ]]; then
    # a clique_grow/edge_cover pick: emit-sql rebuilds this cover directly (needs the selectivities)
    GEN_ARGS+=(--explicit-cover "$EXPLICIT_COVER" --selectivities "$SELECTIVITIES")
  fi
  GEN_ARGS+=(--composition-plan-idx "$COMPOSITION_PLAN_IDX")
  if [[ -n "$COMPOSITION_VARIANT_IDX" ]]; then
    GEN_ARGS+=(--composition-variant-idx "$COMPOSITION_VARIANT_IDX")
  fi
  GEN_ARGS+=(--composition-variant-mode "$COMPOSITION_VARIANT_MODE")
  if [[ -n "$MAX_JOIN_ORDERS_PER_TREE" ]]; then
    GEN_ARGS+=(--max-join-orders-per-tree "$MAX_JOIN_ORDERS_PER_TREE")
  fi
  GEN_ARGS+=(--update-plan-idx "$UPDATE_PLAN_IDX")
  if [[ -n "$QUERY_BATCHES" ]]; then
    GEN_ARGS+=(--query-batches "$QUERY_BATCHES")
  fi
  if [[ -n "$SELECTIVITY_MODE" ]]; then
    GEN_ARGS+=(--selectivity-mode "$SELECTIVITY_MODE")
  fi
  if [[ "$TRINO_JOIN_REORDERING" != "default" ]]; then
    GEN_ARGS+=(--trino-join-reordering "$TRINO_JOIN_REORDERING")
  fi
  if [[ -n "$CORRECTNESS_TOTAL_EVENTS" ]]; then
    GEN_ARGS+=(--correctness-total-events "$CORRECTNESS_TOTAL_EVENTS")
  fi
  if [[ "$COST_MODEL_VALIDATION" -eq 1 ]]; then
    GEN_ARGS+=(--cost-model-validation)
  fi
  if [[ -n "$EXTERNAL_EVENTS_MANIFEST" ]]; then
    GEN_ARGS+=(--external-events-manifest "$EXTERNAL_EVENTS_MANIFEST")
  fi
  if [[ -n "$BATCH_SIZES" ]]; then
    GEN_ARGS+=(--batch-sizes "$BATCH_SIZES")
  fi
  python3 "$SCRIPT_DIR/cli.py" emit-sql     --out "$GEN_SQL"     --catalog-out "$OUT_DIR/strategy_catalog.json"     --initial-history "$INITIAL_HISTORY"     --updates "$UPDATES"     --update-size "$UPDATE_SIZE"     "${GEN_ARGS[@]}"
  BENCHMARK_SQL="$GEN_SQL"
fi

if [[ -n "$EXTERNAL_EVENTS_MANIFEST" ]]; then
  # Idempotent: a matching fingerprint in generated_events_meta makes this a
  # cheap no-op, so per-cell invocations do not reload identical data.
  python3 "$SCRIPT_DIR/load_external_events.py" \
    --manifest "$EXTERNAL_EVENTS_MANIFEST" \
    --trino-server "$TRINO_SERVER" \
    --trino-catalog "$TRINO_CATALOG" \
    --trino-schema "$TRINO_SCHEMA" \
    --trino-user "$TRINO_USER"
fi

if [[ ! -f "$BENCHMARK_SQL" ]]; then
  echo "benchmark SQL file not found: $BENCHMARK_SQL" >&2
  exit 2
fi

echo "Running benchmark (per-statement wall-clock timing)..."
echo "  sql: $BENCHMARK_SQL"
echo "  out: $OUT_DIR"
echo "  warmup: $WARMUP_RUNS"
echo "  runs: $RUNS"
echo "  server: $TRINO_SERVER"
echo "  catalog.schema: ${TRINO_CATALOG}.${TRINO_SCHEMA}"
echo "  user: $TRINO_USER"
echo "  runner: python-http"
if [[ -n "$SEL_R" || -n "$SEL_B" || -n "$SEL_M" ]]; then
  echo "  selectivities: R=${SEL_R:-unset} B=${SEL_B:-unset} M=${SEL_M:-unset}"
fi
echo "  workload-profile: $WORKLOAD_PROFILE"
echo "  workload-seed: $WORKLOAD_SEED"
echo "  query-spec: $QUERY_SPEC"
if [[ -n "$QUERY_SPEC_FILE" ]]; then
  echo "  query-spec-file: $QUERY_SPEC_FILE"
fi
if [[ -n "$STRATEGIES" ]]; then
  echo "  strategies: $STRATEGIES"
fi
echo "  dump-qep: $DUMP_QEP"
echo "  dump-explain-analyze: $DUMP_EXPLAIN_ANALYZE"
if [[ "$COST_MODEL_VALIDATION" -eq 1 ]]; then
  echo "  cost-model-validation: 1"
  echo "  selectivities-file: $SELECTIVITIES"
  if [[ -n "$SELECTIVITY_MODE" ]]; then
    echo "  selectivity-mode: $SELECTIVITY_MODE"
  fi
fi
echo "  trino-join-reordering: $TRINO_JOIN_REORDERING"
echo "  trino-task-concurrency: $TRINO_TASK_CONCURRENCY"

export TRINO_SERVER TRINO_CATALOG TRINO_SCHEMA TRINO_USER TRINO_HTTP_SOURCE TRINO_HTTP_TIMEOUT_SECONDS
export DUMP_QEP
export DUMP_EXPLAIN_ANALYZE
export EIMER_COST_MODEL_VALIDATION="$COST_MODEL_VALIDATION"
export EIMER_SELECTIVITIES="$SELECTIVITIES"
export EIMER_BENCHMARK_DIR="$SCRIPT_DIR"
export EIMER_TRINO_JOIN_REORDERING="$TRINO_JOIN_REORDERING"
export EIMER_TRINO_TASK_CONCURRENCY="$TRINO_TASK_CONCURRENCY"

python3 - "$BENCHMARK_SQL" "$OUT_DIR" "$RUNS" "$WARMUP_RUNS" <<'PY'
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

benchmark_sql = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
runs = int(sys.argv[3])
warmup_runs = int(sys.argv[4])
dump_qep = os.environ.get("DUMP_QEP", "0").strip() == "1"
dump_explain_analyze = os.environ.get("DUMP_EXPLAIN_ANALYZE", "0").strip() == "1"
test_mode = os.environ.get("EIMER_COST_MODEL_VALIDATION", "0").strip() == "1"
trino_join_reordering = os.environ.get("EIMER_TRINO_JOIN_REORDERING", "default").strip().lower()
trino_task_concurrency = os.environ.get("EIMER_TRINO_TASK_CONCURRENCY", "1").strip().lower()
benchmark_dir = Path(os.environ["EIMER_BENCHMARK_DIR"]).resolve()
if str(benchmark_dir) not in sys.path:
    sys.path.insert(0, str(benchmark_dir))

from sql_statement_context import iter_statements_with_context  # noqa: E402


class TrinoHttpRunner:
    def __init__(self) -> None:
        server = os.environ.get("TRINO_SERVER", "localhost:8080").strip()
        if server.startswith("http://") or server.startswith("https://"):
            self.base_url = server.rstrip("/")
        else:
            self.base_url = f"http://{server}".rstrip("/")

        self.user = os.environ.get("TRINO_USER", "trino")
        self.catalog = os.environ.get("TRINO_CATALOG", "memory")
        self.schema = os.environ.get("TRINO_SCHEMA", "default")
        self.source = os.environ.get("TRINO_HTTP_SOURCE", "benchmark_runner")
        self.timeout = float(os.environ.get("TRINO_HTTP_TIMEOUT_SECONDS", "3600"))
        self.session: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        headers = {
            "X-Trino-User": self.user,
            "X-Trino-Catalog": self.catalog,
            "X-Trino-Schema": self.schema,
            "X-Trino-Source": self.source,
        }
        if self.session:
            headers["X-Trino-Session"] = ",".join(f"{k}={v}" for k, v in sorted(self.session.items()))
        return headers

    def _apply_response_headers(self, resp_headers) -> None:
        for raw in resp_headers.get_all("X-Trino-Set-Session", []):
            for item in raw.split(","):
                item = item.strip()
                if not item or "=" not in item:
                    continue
                key, value = item.split("=", 1)
                self.session[key.strip()] = value.strip()
        for raw in resp_headers.get_all("X-Trino-Clear-Session", []):
            for key in raw.split(","):
                key = key.strip()
                if key:
                    self.session.pop(key, None)

    def _request_json(self, *, url: str, method: str, body: str | None = None) -> tuple[dict, str]:
        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url=url,
            data=data,
            method=method,
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload_raw = resp.read().decode("utf-8")
                payload = json.loads(payload_raw) if payload_raw else {}
                self._apply_response_headers(resp.headers)
                return payload, ""
        except urllib.error.HTTPError as exc:
            payload_raw = exc.read().decode("utf-8", errors="replace")
            self._apply_response_headers(exc.headers)
            if payload_raw:
                try:
                    return json.loads(payload_raw), ""
                except json.JSONDecodeError:
                    pass
            return {}, f"http_error={exc.code} body={payload_raw.strip()}"
        except urllib.error.URLError as exc:
            return {}, f"transport_error={exc.reason}"
        except TimeoutError:
            return {}, "transport_error=timeout"

    @staticmethod
    def _extract_columns(payload: dict) -> list[str]:
        cols = payload.get("columns")
        if not isinstance(cols, list):
            return []
        names: list[str] = []
        for idx, col in enumerate(cols, start=1):
            if isinstance(col, dict) and col.get("name"):
                names.append(str(col["name"]))
            else:
                names.append(f"col_{idx}")
        return names

    @staticmethod
    def _extract_data_rows(payload: dict) -> list[list]:
        data = payload.get("data")
        if isinstance(data, list):
            return [row if isinstance(row, list) else [row] for row in data]
        return []

    def execute(self, statement: str) -> tuple[bool, str, dict, str, list[str], list[list]]:
        first, err = self._request_json(
            url=f"{self.base_url}/v1/statement",
            method="POST",
            body=statement,
        )
        if err:
            return False, "", {}, err, [], []

        payload = first
        query_id = str(first.get("id", ""))
        max_peaks = extract_peak_memory(first)
        columns = self._extract_columns(first)
        result_rows = self._extract_data_rows(first)

        while payload.get("nextUri"):
            payload, err = self._request_json(url=str(payload["nextUri"]), method="GET")
            if err:
                peaks = extract_peak_memory(payload)
                max_peaks = {
                    "peak_user_memory_bytes": max(max_peaks["peak_user_memory_bytes"], peaks["peak_user_memory_bytes"]),
                    "peak_total_memory_bytes": max(max_peaks["peak_total_memory_bytes"], peaks["peak_total_memory_bytes"]),
                    "peak_task_total_memory_bytes": max(
                        max_peaks["peak_task_total_memory_bytes"], peaks["peak_task_total_memory_bytes"]
                    ),
                }
                stats_obj = payload.get("stats") if isinstance(payload, dict) else None
                if not isinstance(stats_obj, dict) and isinstance(payload, dict):
                    payload["stats"] = {}
                    stats_obj = payload["stats"]
                if isinstance(stats_obj, dict):
                    stats_obj["peakUserMemoryBytes"] = max_peaks["peak_user_memory_bytes"]
                    stats_obj["peakTotalMemoryBytes"] = max_peaks["peak_total_memory_bytes"]
                    stats_obj["peakTaskTotalMemoryBytes"] = max_peaks["peak_task_total_memory_bytes"]
                return False, query_id, payload, err, columns, result_rows
            peaks = extract_peak_memory(payload)
            max_peaks = {
                "peak_user_memory_bytes": max(max_peaks["peak_user_memory_bytes"], peaks["peak_user_memory_bytes"]),
                "peak_total_memory_bytes": max(max_peaks["peak_total_memory_bytes"], peaks["peak_total_memory_bytes"]),
                "peak_task_total_memory_bytes": max(
                    max_peaks["peak_task_total_memory_bytes"], peaks["peak_task_total_memory_bytes"]
                ),
            }
            if not columns:
                columns = self._extract_columns(payload)
            result_rows.extend(self._extract_data_rows(payload))

        if query_id:
            info_payload, _ = self._request_json(url=f"{self.base_url}/v1/query/{query_id}", method="GET")
            peaks = extract_peak_memory(info_payload)
            max_peaks = {
                "peak_user_memory_bytes": max(max_peaks["peak_user_memory_bytes"], peaks["peak_user_memory_bytes"]),
                "peak_total_memory_bytes": max(max_peaks["peak_total_memory_bytes"], peaks["peak_total_memory_bytes"]),
                "peak_task_total_memory_bytes": max(
                    max_peaks["peak_task_total_memory_bytes"], peaks["peak_task_total_memory_bytes"]
                ),
            }

        stats_obj = payload.get("stats") if isinstance(payload, dict) else None
        if not isinstance(stats_obj, dict) and isinstance(payload, dict):
            payload["stats"] = {}
            stats_obj = payload["stats"]
        if isinstance(stats_obj, dict):
            stats_obj["peakUserMemoryBytes"] = max_peaks["peak_user_memory_bytes"]
            stats_obj["peakTotalMemoryBytes"] = max_peaks["peak_total_memory_bytes"]
            stats_obj["peakTaskTotalMemoryBytes"] = max_peaks["peak_task_total_memory_bytes"]

        error = payload.get("error")
        if error:
            msg = str(error.get("message") or error.get("errorName") or "query_error")
            return False, query_id, payload, msg, columns, result_rows

        return True, query_id, payload, "", columns, result_rows


runner_mode = f"http:{os.environ.get('TRINO_SERVER', 'localhost:8080')}"
print(f"Runner mode: {runner_mode}")

statements = list(iter_statements_with_context(benchmark_sql))
if not statements:
    print("No SQL statements found.", file=sys.stderr)
    raise SystemExit(2)


def configure_runner_session(runner: TrinoHttpRunner) -> None:
    # Runaway-statement guard: EIMER_QUERY_MAX_RUN_TIME (e.g. '5m') makes Trino kill any
    # statement that exceeds it; the runner then exits non-zero and the sweep records the
    # candidate as failed and moves on (the fast plans still get measured).
    _qmrt = os.environ.get("EIMER_QUERY_MAX_RUN_TIME", "").strip()
    if _qmrt:
        runner.execute(f"SET SESSION query_max_run_time = '{_qmrt}'")
    # Intra-query parallelism pin: task_concurrency=1 disables intra-query operator
    # parallelism; "default" leaves the engine default.
    if trino_task_concurrency not in {"", "default"}:
        ok, _query_id, _payload, error, _cols, _rows = runner.execute(
            f"SET SESSION task_concurrency = {int(trino_task_concurrency)}"
        )
        if not ok:
            raise SystemExit(f"Failed to set task_concurrency={trino_task_concurrency}: {error}")
    if trino_join_reordering in {"", "default"}:
        return
    if trino_join_reordering != "none":
        raise SystemExit(f"Unsupported EIMER_TRINO_JOIN_REORDERING={trino_join_reordering!r}")
    ok, _query_id, _payload, error, _cols, _rows = runner.execute(
        "SET SESSION join_reordering_strategy = 'NONE'"
    )
    if not ok:
        raise SystemExit(f"Failed to set join_reordering_strategy=NONE: {error}")

max_batch = 0
for meta in statements:
    strategy = str(meta["strategy"])
    batch = int(meta["batch"])
    if strategy != "DATA" and batch > 0:
        max_batch = max(max_batch, batch)

validation_dep_graph = None
if test_mode:
    from query_registry import CANONICAL_QUERY_SPEC_NAME, get_query_spec  # noqa: E402
    from eimer.query.query_spec import query_spec_from_dict, query_spec_to_dependency_graph  # noqa: E402
    from eimer.sql.sql_render import render_expression_for_base_scan  # noqa: E402

    catalog_path = out_dir / "strategy_catalog.json"
    if not catalog_path.exists():
        print(
            f"Cost-model validation requires strategy_catalog.json in {out_dir}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    catalog_payload = json.loads(catalog_path.read_text(encoding="utf-8"))
    if isinstance(catalog_payload.get("query_spec_payload"), dict):
        validation_query_spec = query_spec_from_dict(catalog_payload["query_spec_payload"])
    else:
        validation_query_spec_name = str(catalog_payload.get("query_spec") or CANONICAL_QUERY_SPEC_NAME)
        validation_query_spec = get_query_spec(validation_query_spec_name)
    validation_dep_graph = query_spec_to_dependency_graph(validation_query_spec)


def as_int(value) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


_BYTE_UNITS = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "pb": 1000**5,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "pib": 1024**5,
}


def parse_bytes_value(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return 0
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)?$", text)
    if not m:
        return as_int(value)
    number = float(m.group(1))
    unit = (m.group(2) or "B").strip().lower()
    if unit.endswith("s"):
        unit = unit[:-1]
    multiplier = _BYTE_UNITS.get(unit)
    if multiplier is None:
        return as_int(value)
    return int(number * multiplier)


def extract_peak_memory(payload: dict) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {
            "peak_user_memory_bytes": 0,
            "peak_total_memory_bytes": 0,
            "peak_task_total_memory_bytes": 0,
        }

    stats = payload.get("stats")
    query_stats = payload.get("queryStats")
    sources = [src for src in (stats, query_stats, payload) if isinstance(src, dict)]

    user_keys = (
        "peakUserMemoryBytes",
        "peak_user_memory_bytes",
        "peakUserMemoryReservationBytes",
        "peakUserMemoryReservation",
        "userMemoryReservation",
    )
    total_keys = (
        "peakTotalMemoryBytes",
        "peak_total_memory_bytes",
        "peakMemoryBytes",
        "peakMemoryReservationBytes",
        "peakTotalMemoryReservationBytes",
        "peakTotalMemoryReservation",
        "totalMemoryReservation",
    )
    task_keys = (
        "peakTaskTotalMemoryBytes",
        "peak_task_total_memory_bytes",
        "peakTaskUserMemoryReservationBytes",
        "peakTaskTotalMemoryReservationBytes",
        "peakTaskTotalMemoryReservation",
        "peakTaskUserMemory",
    )

    user = 0
    total = 0
    task = 0
    for src in sources:
        for key in user_keys:
            if key in src and src[key] is not None:
                user = max(user, parse_bytes_value(src[key]))
        for key in total_keys:
            if key in src and src[key] is not None:
                total = max(total, parse_bytes_value(src[key]))
        for key in task_keys:
            if key in src and src[key] is not None:
                task = max(task, parse_bytes_value(src[key]))

    return {
        "peak_user_memory_bytes": user,
        "peak_total_memory_bytes": total,
        "peak_task_total_memory_bytes": task,
    }


def split_top_level_csv(text: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    in_single = False
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if ch == "'":
            if in_single and nxt == "'":
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue

        if not in_single:
            if ch == "(":
                depth += 1
            elif ch == ")" and depth > 0:
                depth -= 1
            elif ch == "," and depth == 0:
                part = text[start:i].strip()
                if part:
                    items.append(part)
                start = i + 1
        i += 1

    tail = text[start:].strip()
    if tail:
        items.append(tail)
    return items


def parse_cache_layout_from_create_statement(statement: str) -> tuple[str, int] | None:
    match = re.match(
        r"(?is)^\s*create\s+table\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*)\)\s*$",
        statement.strip(),
    )
    if not match:
        return None

    table_name = match.group(1)
    if not table_name.lower().startswith("cache_"):
        return None

    body = match.group(2).strip()
    if not body:
        return (table_name, 16)

    column_defs = split_top_level_csv(body)
    column_count = 0
    for definition in column_defs:
        lowered = definition.strip().lower()
        if not lowered:
            continue
        if lowered.startswith(("primary key", "constraint", "unique", "index", "check", "foreign key")):
            continue
        column_count += 1

    if column_count <= 0:
        column_count = 2
    # Approximate logical bytes as 8 bytes per projected column.
    row_width_bytes = column_count * 8
    return (table_name, row_width_bytes)


def build_state_probes_from_statements(parsed_statements) -> dict[str, tuple[tuple[str, int], ...]]:
    probes: dict[str, list[tuple[str, int]]] = {}
    seen_tables: dict[str, set[str]] = {}
    for meta in parsed_statements:
        strategy = str(meta["strategy"])
        if strategy == "DATA":
            continue
        parsed = parse_cache_layout_from_create_statement(str(meta["stmt"]))
        if not parsed:
            continue

        table_name, row_width_bytes = parsed
        probes.setdefault(strategy, [])
        seen_tables.setdefault(strategy, set())
        if table_name in seen_tables[strategy]:
            continue

        probes[strategy].append((table_name, row_width_bytes))
        seen_tables[strategy].add(table_name)

    return {strategy: tuple(entries) for strategy, entries in probes.items()}


STATE_PROBES = build_state_probes_from_statements(statements)


def build_state_probe_sql(strategy: str) -> str:
    probes = STATE_PROBES.get(strategy, ())
    if not probes:
        return ""
    parts = []
    for table_name, row_width_bytes in probes:
        logical_bytes_expr = f"CAST(count(*) * {row_width_bytes} AS BIGINT)"
        parts.append(
            "SELECT "
            f"'{table_name}' AS table_name, "
            "CAST(count(*) AS BIGINT) AS row_count, "
            f"{logical_bytes_expr} AS logical_bytes "
            f"FROM {table_name}"
        )
    return "\nUNION ALL\n".join(parts)


def is_end_of_strategy_batch(statement_index_1based: int, meta: dict) -> bool:
    strategy = str(meta["strategy"])
    batch = int(meta["batch"])
    if strategy not in STATE_PROBES or batch <= 0:
        return False
    if statement_index_1based >= len(statements):
        return True
    nxt = statements[statement_index_1based]
    return not (str(nxt["strategy"]) == strategy and int(nxt["batch"]) == batch)


def snapshot_strategy_state(*, runner: TrinoHttpRunner, strategy: str, batch: int) -> list[dict]:
    probe_sql = build_state_probe_sql(strategy)
    if not probe_sql:
        return []
    t0 = time.monotonic_ns()
    ok, query_id, _payload, error, cols, rows_data = runner.execute(probe_sql)
    t1 = time.monotonic_ns()
    wall_ms = int(round((t1 - t0) / 1_000_000))

    if not ok:
        return [
            {
                "strategy": strategy,
                "batch": batch,
                "table_name": "__probe_error__",
                "row_count": -1,
                "logical_bytes": -1,
                "probe_wall_time_ms": wall_ms,
                "probe_query_id": query_id or "",
                "error": error,
            }
        ]

    idx = {str(c): i for i, c in enumerate(cols)}
    out: list[dict] = []
    for row in rows_data:
        if not isinstance(row, list):
            continue
        table_name = str(row[idx.get("table_name", 0)]) if row else ""
        row_count = as_int(row[idx["row_count"]]) if "row_count" in idx and idx["row_count"] < len(row) else 0
        logical_bytes = as_int(row[idx["logical_bytes"]]) if "logical_bytes" in idx and idx["logical_bytes"] < len(row) else 0
        out.append(
            {
                "strategy": strategy,
                "batch": batch,
                "table_name": table_name,
                "row_count": row_count,
                "logical_bytes": logical_bytes,
                "probe_wall_time_ms": wall_ms,
                "probe_query_id": query_id or "",
                "error": "",
            }
        )
    return out


def op_kind_from_id(op_id: str | None, phase: str) -> str:
    if op_id:
        if op_id.startswith("upd:"):
            return "update"
        if op_id == "compose":
            return "compose"
        if op_id == "post_filter":
            return "post_filter"
    return phase


def extract_target_table_from_statement(statement: str) -> str | None:
    match = re.match(
        r"(?is)^\s*(?:insert\s+into|delete\s+from)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        statement.strip(),
    )
    if not match:
        return None
    return match.group(1)


def execute_count_probe(*, runner: TrinoHttpRunner, sql: str) -> dict:
    t0 = time.monotonic_ns()
    ok, query_id, _payload, error, cols, rows_data = runner.execute(sql)
    t1 = time.monotonic_ns()
    wall_ms = int(round((t1 - t0) / 1_000_000))

    row_count = None
    if ok and rows_data:
        first = rows_data[0]
        if isinstance(first, list) and first:
            row_count = as_int(first[0])

    return {
        "ok": ok,
        "query_id": query_id or "",
        "row_count": row_count,
        "wall_ms": wall_ms,
        "error": error,
        "columns": cols,
    }


def render_variable_probe_sql(*, table_name: str, variable: str) -> str:
    assert validation_dep_graph is not None
    predicates = [
        render_expression_for_base_scan(condition, variable, "probe_src")
        for condition in validation_dep_graph.independent_conditions.get(variable, [])
    ]
    where_sql = ""
    if predicates:
        where_sql = " WHERE " + " AND ".join(predicates)
    return f"SELECT CAST(count(*) AS BIGINT) FROM {table_name} probe_src{where_sql}"


def run_pre_batch_validation_probes(
    *,
    runner: TrinoHttpRunner,
    strategy: str,
    batch: int,
) -> tuple[list[dict], int, int]:
    if validation_dep_graph is None:
        return ([], 0, 0)

    rows: list[dict] = []
    total_wall_ms = 0
    num_probes = 0
    for variable in validation_dep_graph.variables:
        for table_name in ("events", "events_batch"):
            probe = execute_count_probe(
                runner=runner,
                sql=render_variable_probe_sql(table_name=table_name, variable=variable),
            )
            rows.append(
                {
                    "strategy": strategy,
                    "batch": batch,
                    "probe_point": "pre_batch",
                    "table_name": f"{table_name}:{variable}",
                    "row_count": probe["row_count"],
                    "probe_wall_time_ms": probe["wall_ms"],
                    "probe_query_id": probe["query_id"],
                    "error": probe["error"],
                }
            )
            total_wall_ms += probe["wall_ms"]
            num_probes += 1

    return (rows, total_wall_ms, num_probes)


def wrap_snapshot_rows(*, probe_rows: list[dict], probe_point: str) -> list[dict]:
    out: list[dict] = []
    for row in probe_rows:
        out.append(
            {
                "strategy": row["strategy"],
                "batch": row["batch"],
                "probe_point": probe_point,
                "table_name": row["table_name"],
                "row_count": row["row_count"],
                "probe_wall_time_ms": row["probe_wall_time_ms"],
                "probe_query_id": row["probe_query_id"],
                "error": row.get("error", ""),
            }
        )
    return out


def run_named_table_probe(
    *,
    runner: TrinoHttpRunner,
    strategy: str,
    batch: int,
    table_name: str,
    probe_point: str,
) -> tuple[dict, int, int]:
    probe = execute_count_probe(runner=runner, sql=f"SELECT CAST(count(*) AS BIGINT) FROM {table_name}")
    row = {
        "strategy": strategy,
        "batch": batch,
        "probe_point": probe_point,
        "table_name": table_name,
        "row_count": probe["row_count"],
        "probe_wall_time_ms": probe["wall_ms"],
        "probe_query_id": probe["query_id"],
        "error": probe["error"],
    }
    return (row, probe["wall_ms"], 1)


def sanitize_for_path(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text))
    cleaned = cleaned.strip("_")
    return cleaned or "x"


def extract_explain_plan_cell(rows_data: list[list]) -> str:
    if not rows_data:
        return ""
    first = rows_data[0]
    if not isinstance(first, list) or not first:
        return ""
    value = first[0]
    if value is None:
        return ""
    return str(value)


def extract_read_only_explain_target(statement: str, phase: str) -> tuple[str, str]:
    text = statement.strip()
    if phase == "compose":
        match = re.match(
            r"(?is)^\s*create\s+table\s+[A-Za-z_][A-Za-z0-9_]*\s+as\s+(with\b.*|select\b.*)$",
            text,
        )
        if match:
            return (match.group(1).strip(), "compose_select")
        return ("", "")

    if phase == "update":
        match = re.match(
            r"(?is)^\s*insert\s+into\s+[A-Za-z_][A-Za-z0-9_]*\s+(with\b.*|select\b.*)$",
            text,
        )
        if match:
            return (match.group(1).strip(), "update_select")
        return ("", "")

    return ("", "")


def dump_statement_qep(
    *,
    runner: TrinoHttpRunner,
    run_idx: int,
    stmt_index: int,
    meta: dict,
    out_dir: Path,
):
    strategy = str(meta["strategy"])
    batch = int(meta["batch"])
    phase = str(meta["phase"])
    stmt = str(meta["stmt"]).strip()

    if not stmt or strategy == "DATA" or batch <= 0:
        return None
    if phase not in ("update", "compose"):
        return None

    explain_target_sql, explain_target_kind = extract_read_only_explain_target(stmt, phase)
    qep_dir = out_dir / f"run{run_idx}_qep" / f"strategy_{sanitize_for_path(strategy)}" / f"batch_{batch:03d}"
    qep_dir.mkdir(parents=True, exist_ok=True)
    base = f"stmt_{stmt_index:04d}_{phase}"

    if not explain_target_sql:
        out_path = qep_dir / f"{base}.skip.json"
        out_path.write_text(
            json.dumps(
                {
                    "strategy": strategy,
                    "batch": batch,
                    "phase": phase,
                    "stmt_index": stmt_index,
                    "stmt_head": str(meta.get("stmt_head", "")),
                    "ok": False,
                    "skipped": True,
                    "reason": "Could not extract read-only SELECT/WITH target for EXPLAIN",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return {
            "ok": False,
            "skipped": True,
            "path": str(out_path),
            "wall_ms": 0,
            "query_id": "",
            "error": "skip: no explain target",
        }

    explain_sql = f"EXPLAIN (TYPE DISTRIBUTED, FORMAT JSON) {explain_target_sql}"
    t0 = time.monotonic_ns()
    ok, query_id, payload, error, cols, rows_data = runner.execute(explain_sql)
    t1 = time.monotonic_ns()
    wall_ms = int(round((t1 - t0) / 1_000_000))

    record = {
        "strategy": strategy,
        "batch": batch,
        "phase": phase,
        "stmt_index": stmt_index,
        "stmt_head": str(meta.get("stmt_head", "")),
        "explain_target_kind": explain_target_kind,
        "explain_target_head": re.sub(r"\s+", " ", explain_target_sql)[:220],
        "explain_query_id": query_id or "",
        "explain_wall_time_ms": wall_ms,
        "ok": bool(ok),
        "columns": cols,
    }

    if ok:
        plan_raw = extract_explain_plan_cell(rows_data)
        record["plan_raw"] = plan_raw
        if plan_raw:
            try:
                record["plan_json"] = json.loads(plan_raw)
            except json.JSONDecodeError:
                pass
        out_path = qep_dir / f"{base}.json"
        out_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return {"ok": True, "path": str(out_path), "wall_ms": wall_ms, "query_id": query_id or ""}

    error_record = dict(record)
    error_record["error"] = error
    error_record["payload"] = payload if isinstance(payload, dict) else {}
    out_path = qep_dir / f"{base}.error.json"
    out_path.write_text(json.dumps(error_record, indent=2), encoding="utf-8")
    return {"ok": False, "path": str(out_path), "wall_ms": wall_ms, "query_id": query_id or "", "error": error}


def dump_statement_explain_analyze(
    *,
    runner: TrinoHttpRunner,
    run_idx: int,
    stmt_index: int,
    meta: dict,
    out_dir: Path,
):
    """Capture ``EXPLAIN ANALYZE`` for a compose/update statement: re-runs the read-only
    SELECT and records Trino's actual per-operator rows/time/CPU/memory. Output is the raw
    text plus a JSON sidecar under ``run{idx}_explain_analyze/``."""
    strategy = str(meta["strategy"])
    batch = int(meta["batch"])
    phase = str(meta["phase"])
    stmt = str(meta["stmt"]).strip()

    if not stmt or strategy == "DATA" or batch <= 0:
        return None
    if phase not in ("update", "compose"):
        return None

    explain_target_sql, explain_target_kind = extract_read_only_explain_target(stmt, phase)
    ea_dir = out_dir / f"run{run_idx}_explain_analyze" / f"strategy_{sanitize_for_path(strategy)}" / f"batch_{batch:03d}"
    ea_dir.mkdir(parents=True, exist_ok=True)
    base = f"stmt_{stmt_index:04d}_{phase}"

    if not explain_target_sql:
        return {"ok": False, "skipped": True, "path": "", "wall_ms": 0, "query_id": "", "error": "skip: no explain target"}

    explain_sql = f"EXPLAIN ANALYZE {explain_target_sql}"
    t0 = time.monotonic_ns()
    ok, query_id, payload, error, cols, rows_data = runner.execute(explain_sql)
    t1 = time.monotonic_ns()
    wall_ms = int(round((t1 - t0) / 1_000_000))

    record = {
        "strategy": strategy,
        "batch": batch,
        "phase": phase,
        "stmt_index": stmt_index,
        "op_id": str(meta.get("op_id", "")),
        "stmt_head": str(meta.get("stmt_head", "")),
        "explain_target_kind": explain_target_kind,
        "explain_query_id": query_id or "",
        "explain_wall_time_ms": wall_ms,
        "ok": bool(ok),
    }
    if ok:
        plan_text = "\n".join(str(row[0]) if row else "" for row in rows_data)
        (ea_dir / f"{base}.txt").write_text(plan_text, encoding="utf-8")
        record["plan_text_path"] = str(ea_dir / f"{base}.txt")
        (ea_dir / f"{base}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return {"ok": True, "path": str(ea_dir / f"{base}.txt"), "wall_ms": wall_ms, "query_id": query_id or ""}

    record["error"] = error
    (ea_dir / f"{base}.error.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return {"ok": False, "path": str(ea_dir / f"{base}.error.json"), "wall_ms": wall_ms, "query_id": query_id or "", "error": error}


def dump_statement_query_stats(
    *,
    runner: "TrinoHttpRunner",
    run_idx: int,
    stmt_index: int,
    meta: dict,
    query_id: str,
    out_dir: Path,
):
    """Preserve an UPDATE statement's per-operator query stats (Trino /v1/query/{id}): the
    TableWriter/TableFinish sink that EXPLAIN ANALYZE of the read-only SELECT cannot show
    (the INSERT is stripped, and EXPLAIN ANALYZE of the INSERT would double-write). Saves
    the raw QueryInfo JSON."""
    if not query_id:
        return None
    strategy = str(meta["strategy"])
    batch = int(meta["batch"])
    try:
        info, _err = runner._request_json(url=f"{runner.base_url}/v1/query/{query_id}", method="GET")
    except Exception as exc:  # never fail the run on a stats fetch
        return {"ok": False, "error": str(exc)}
    qs_dir = out_dir / f"run{run_idx}_query_stats" / f"strategy_{sanitize_for_path(strategy)}" / f"batch_{batch:03d}"
    qs_dir.mkdir(parents=True, exist_ok=True)
    op_id = sanitize_for_path(str(meta.get("op_id", "")))
    out_path = qs_dir / f"stmt_{stmt_index:04d}_{op_id}.json"
    out_path.write_text(json.dumps(info if isinstance(info, dict) else {"raw": info}, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(out_path)}


def write_summary(rows, path: Path):
    totals = defaultdict(int)
    for r in rows:
        totals[(r["strategy"], r["batch"], r["phase"])] += int(r["wall_time_ms"])
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "batch", "phase", "total_time_ms"])
        for (strategy, batch, phase), total_ms in sorted(totals.items()):
            w.writerow([strategy, batch, phase, total_ms])
    return totals


def write_memory_query_summary(rows, path: Path):
    peaks = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        key = (r["strategy"], r["batch"], r["phase"])
        peaks[key][0] = max(peaks[key][0], as_int(r.get("peak_user_memory_bytes", 0)))
        peaks[key][1] = max(peaks[key][1], as_int(r.get("peak_total_memory_bytes", 0)))
        peaks[key][2] = max(peaks[key][2], as_int(r.get("peak_task_total_memory_bytes", 0)))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "strategy",
                "batch",
                "phase",
                "peak_user_memory_bytes",
                "peak_total_memory_bytes",
                "peak_task_total_memory_bytes",
            ]
        )
        for (strategy, batch, phase), values in sorted(peaks.items()):
            w.writerow([strategy, batch, phase, values[0], values[1], values[2]])
    return {k: tuple(v) for k, v in peaks.items()}


def write_memory_state_summary(rows, path: Path):
    totals = defaultdict(lambda: [0, 0])
    for r in rows:
        strategy = str(r.get("strategy", ""))
        batch = as_int(r.get("batch", 0))
        row_count = as_int(r.get("row_count", 0))
        logical_bytes = as_int(r.get("logical_bytes", 0))
        if row_count < 0 or logical_bytes < 0:
            continue
        totals[(strategy, batch)][0] += row_count
        totals[(strategy, batch)][1] += logical_bytes
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["strategy", "batch", "total_state_rows", "total_state_logical_bytes"])
        for (strategy, batch), values in sorted(totals.items()):
            w.writerow([strategy, batch, values[0], values[1]])
    return {k: tuple(v) for k, v in totals.items()}


def log_statement_result(*, lf, run_label: str, i: int, meta: dict, query_id: str, payload: dict, wall_ms: int, ok: bool, error: str, returned_rows: int):
    stats = payload.get("stats", {}) if isinstance(payload, dict) else {}
    state = str(stats.get("state", ""))
    completed_splits = stats.get("completedSplits")
    lf.write(
        f"[{run_label}] stmt={i} strategy={meta['strategy']} batch={meta['batch']} "
        f"phase={meta['phase']} query_id={query_id or '-'} wall_ms={wall_ms} "
        f"state={state or '-'} completed_splits={completed_splits} returned_rows={returned_rows}\n"
    )
    if not ok:
        lf.write(f"[{run_label}] error={error}\n")
    # Flush after each statement log entry so tail -f reflects long-running progress.
    lf.flush()


if warmup_runs > 0:
    for warm_idx in range(1, warmup_runs + 1):
        runner = TrinoHttpRunner()
        configure_runner_session(runner)
        log_file = out_dir / f"warmup{warm_idx}_trino.log"
        log_file.write_text("", encoding="utf-8")
        with log_file.open("a", encoding="utf-8") as lf:
            for i, meta in enumerate(statements, start=1):
                stmt = meta["stmt"].strip()
                if not stmt:
                    continue
                t0 = time.monotonic_ns()
                ok, query_id, payload, error, _cols, rows_data = runner.execute(stmt)
                t1 = time.monotonic_ns()
                wall_ms = int(round((t1 - t0) / 1_000_000))
                log_statement_result(
                    lf=lf,
                    run_label=f"warmup{warm_idx}",
                    i=i,
                    meta=meta,
                    query_id=query_id,
                    payload=payload,
                    wall_ms=wall_ms,
                    ok=ok,
                    error=error,
                    returned_rows=len(rows_data),
                )
                if not ok:
                    print(f"Warmup {warm_idx}: statement {i} failed (exit 1)", file=sys.stderr)
                    print(f"strategy={meta['strategy']} batch={meta['batch']} phase={meta['phase']}", file=sys.stderr)
                    print(f"stmt_head={meta['stmt_head']}", file=sys.stderr)
                    print(f"error={error}", file=sys.stderr)
                    raise SystemExit(1)
        print(f"Wrote {log_file} (warmup; discarded)")


run_totals = []
run_memory_query = []
run_memory_state = []
for run_idx in range(1, runs + 1):
    runner = TrinoHttpRunner()
    configure_runner_session(runner)
    log_file = out_dir / f"run{run_idx}_trino.log"
    timings_csv = out_dir / f"run{run_idx}_timings.csv"
    summary_csv = out_dir / f"run{run_idx}_timings_summary.csv"
    memory_query_summary_csv = out_dir / f"run{run_idx}_memory_query_summary.csv"
    memory_state_csv = out_dir / f"run{run_idx}_memory_state.csv"
    memory_state_summary_csv = out_dir / f"run{run_idx}_memory_state_summary.csv"
    bench_results_csv = out_dir / f"run{run_idx}_bench_results.csv"
    correctness_txt = out_dir / f"run{run_idx}_correctness_check.txt"
    correctness_by_batch_csv = out_dir / f"run{run_idx}_correctness_by_batch.csv"
    validation_measurements_csv = out_dir / f"run{run_idx}_cost_validation_measurements.csv"
    validation_state_csv = out_dir / f"run{run_idx}_cost_validation_state.csv"
    validation_probe_overhead_csv = out_dir / f"run{run_idx}_cost_validation_probe_overhead.csv"

    log_file.write_text("", encoding="utf-8")
    rows = []
    memory_state_rows = []
    validation_measurement_rows = []
    validation_state_rows = []
    probe_overhead = defaultdict(lambda: {"total_probe_wall_ms": 0, "num_probes": 0, "total_measured_wall_ms": 0})
    bench_result_columns: list[str] = []
    bench_result_rows: list[list] = []
    current_strategy = None
    current_cache_sizes: dict[str, int | None] = {}
    with log_file.open("a", encoding="utf-8") as lf:
        for i, meta in enumerate(statements, start=1):
            strategy = str(meta["strategy"])
            batch = int(meta["batch"])
            if strategy != current_strategy:
                current_strategy = strategy
                current_cache_sizes = {
                    table_name: 0
                    for table_name, _row_width_bytes in STATE_PROBES.get(strategy, ())
                }

            validation_event = meta.get("validation_event")
            if test_mode and validation_event == "pre_batch_probes":
                pre_rows, probe_wall_ms, num_probes = run_pre_batch_validation_probes(
                    runner=runner,
                    strategy=strategy,
                    batch=batch,
                )
                validation_state_rows.extend(pre_rows)
                key = (strategy, batch)
                probe_overhead[key]["total_probe_wall_ms"] += probe_wall_ms
                probe_overhead[key]["num_probes"] += num_probes
                continue

            stmt = meta["stmt"].strip()
            if not stmt:
                continue
            if dump_qep:
                qep = dump_statement_qep(
                    runner=runner,
                    run_idx=run_idx,
                    stmt_index=i,
                    meta=meta,
                    out_dir=out_dir,
                )
                if qep:
                    lf.write(
                        f"[run{run_idx}] qep stmt={i} strategy={meta['strategy']} batch={meta['batch']} "
                        f"phase={meta['phase']} ok={qep['ok']} wall_ms={qep['wall_ms']} "
                        f"query_id={qep.get('query_id', '-') or '-'} path={qep['path']}\n"
                    )
                    if not qep["ok"] and not qep.get("skipped") and qep.get("error"):
                        lf.write(f"[run{run_idx}] qep_error={qep['error']}\n")
                    lf.flush()
            t0 = time.monotonic_ns()
            ok, query_id, payload, error, cols, rows_data = runner.execute(stmt)
            t1 = time.monotonic_ns()
            wall_ms = int(round((t1 - t0) / 1_000_000))
            peak_memory = extract_peak_memory(payload)
            # EXPLAIN ANALYZE capture runs AFTER the timed execute so it never warms the
            # source tables for the timed statement (keeping timing cold); the read-only
            # SELECT it re-runs reads the same state, so one DUMP_EXPLAIN_ANALYZE=1 pass
            # yields clean timing and the per-operator QEP together.
            if dump_explain_analyze and ok and str(meta.get("phase")) in ("update", "compose"):
                ea = dump_statement_explain_analyze(
                    runner=runner,
                    run_idx=run_idx,
                    stmt_index=i,
                    meta=meta,
                    out_dir=out_dir,
                )
                if ea and not ea.get("skipped"):
                    lf.write(
                        f"[run{run_idx}] explain_analyze stmt={i} strategy={meta['strategy']} "
                        f"batch={meta['batch']} ok={ea['ok']} wall_ms={ea['wall_ms']} "
                        f"query_id={ea.get('query_id', '-') or '-'} path={ea.get('path', '-')}\n"
                    )
                    if not ea["ok"] and ea.get("error"):
                        lf.write(f"[run{run_idx}] explain_analyze_error={ea['error']}\n")
                    lf.flush()
            # Preserve the update INSERT's per-operator stats (the sink the EXPLAIN ANALYZE
            # SELECT cannot show); update phase only.
            if dump_explain_analyze and ok and str(meta.get("phase")) == "update" and query_id:
                qs = dump_statement_query_stats(
                    runner=runner, run_idx=run_idx, stmt_index=i, meta=meta,
                    query_id=query_id, out_dir=out_dir,
                )
                if qs and not qs.get("ok"):
                    lf.write(f"[run{run_idx}] query_stats_error stmt={i} {qs.get('error', '')}\n")
                    lf.flush()
            exit_code = 0 if ok else 1
            log_statement_result(
                lf=lf,
                run_label=f"run{run_idx}",
                i=i,
                meta=meta,
                query_id=query_id,
                payload=payload,
                wall_ms=wall_ms,
                ok=ok,
                error=error,
                returned_rows=len(rows_data),
                )
            if ok and meta["phase"] == "report" and cols:
                bench_result_columns = cols
                bench_result_rows.extend(rows_data)
            if test_mode and strategy != "DATA" and batch > 0:
                probe_overhead[(strategy, batch)]["total_measured_wall_ms"] += wall_ms
            rows.append(
                {
                    "i": i,
                    "strategy": meta["strategy"],
                    "batch": meta["batch"],
                    "phase": meta["phase"],
                    "wall_time_ms": wall_ms,
                    "peak_user_memory_bytes": peak_memory["peak_user_memory_bytes"],
                    "peak_total_memory_bytes": peak_memory["peak_total_memory_bytes"],
                    "peak_task_total_memory_bytes": peak_memory["peak_task_total_memory_bytes"],
                    "exit_code": exit_code,
                    "stmt_head": meta["stmt_head"],
                }
            )
            if test_mode and ok and meta.get("op_id"):
                op_id = str(meta["op_id"])
                op_kind = op_kind_from_id(op_id, str(meta["phase"]))
                target_table = extract_target_table_from_statement(stmt)
                pre_target_count = current_cache_sizes.get(target_table) if target_table else None
                probe_point = f"after:{op_id}"

                snapshot_rows = snapshot_strategy_state(
                    runner=runner,
                    strategy=strategy,
                    batch=batch,
                )
                validation_state_rows.extend(wrap_snapshot_rows(probe_rows=snapshot_rows, probe_point=probe_point))
                key = (strategy, batch)
                if snapshot_rows:
                    probe_overhead[key]["total_probe_wall_ms"] += as_int(
                        snapshot_rows[0].get("probe_wall_time_ms", 0)
                    )
                    probe_overhead[key]["num_probes"] += 1
                for row in snapshot_rows:
                    table_name = str(row.get("table_name", ""))
                    row_count = row.get("row_count")
                    if table_name.startswith("cache_") and row_count is not None and as_int(row_count) >= 0:
                        current_cache_sizes[table_name] = as_int(row_count)

                extra_probe_row = None
                extra_probe_wall_ms = 0
                extra_probe_count = 0
                if op_kind == "compose":
                    extra_probe_row, extra_probe_wall_ms, extra_probe_count = run_named_table_probe(
                        runner=runner,
                        strategy=strategy,
                        batch=batch,
                        table_name="composed",
                        probe_point=probe_point,
                    )
                elif op_kind == "post_filter":
                    extra_probe_row, extra_probe_wall_ms, extra_probe_count = run_named_table_probe(
                        runner=runner,
                        strategy=strategy,
                        batch=batch,
                        table_name="result",
                        probe_point=probe_point,
                    )

                if extra_probe_row is not None:
                    validation_state_rows.append(extra_probe_row)
                    probe_overhead[key]["total_probe_wall_ms"] += extra_probe_wall_ms
                    probe_overhead[key]["num_probes"] += extra_probe_count

                post_target_count = current_cache_sizes.get(target_table) if target_table else None
                measured_output = None
                if op_kind == "update" and pre_target_count is not None and post_target_count is not None:
                    measured_output = as_int(post_target_count) - as_int(pre_target_count)
                elif extra_probe_row is not None:
                    measured_output = extra_probe_row.get("row_count")

                validation_measurement_rows.append(
                    {
                        "run_index": run_idx,
                        "strategy": strategy,
                        "batch_index": batch,
                        "op_id": op_id,
                        "op_kind": op_kind,
                        "statement_wall_ms": wall_ms,
                        "query_id": query_id or "",
                        "measured_output": measured_output,
                        "pre_probe_count": pre_target_count if op_kind == "update" else None,
                        "post_probe_count": post_target_count if op_kind == "update" else None,
                    }
                )
            if not ok:
                print(f"Run {run_idx}: statement {i} failed (exit 1)", file=sys.stderr)
                print(f"strategy={meta['strategy']} batch={meta['batch']} phase={meta['phase']}", file=sys.stderr)
                print(f"stmt_head={meta['stmt_head']}", file=sys.stderr)
                print(f"error={error}", file=sys.stderr)
                raise SystemExit(1)
            if is_end_of_strategy_batch(i, meta):
                memory_state_rows.extend(
                    snapshot_strategy_state(
                        runner=runner,
                        strategy=str(meta["strategy"]),
                        batch=int(meta["batch"]),
                    )
                )

    with timings_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "i",
                "strategy",
                "batch",
                "phase",
                "wall_time_ms",
                "peak_user_memory_bytes",
                "peak_total_memory_bytes",
                "peak_task_total_memory_bytes",
                "exit_code",
                "stmt_head",
            ],
        )
        w.writeheader()
        w.writerows(rows)

    totals = write_summary(rows, summary_csv)
    memory_query = write_memory_query_summary(rows, memory_query_summary_csv)
    run_memory_query.append(memory_query)
    run_totals.append(totals)
    print(f"Wrote {timings_csv}")
    print(f"Wrote {summary_csv}")
    print(f"Wrote {memory_query_summary_csv}")

    with memory_state_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "strategy",
                "batch",
                "table_name",
                "row_count",
                "logical_bytes",
                "probe_wall_time_ms",
                "probe_query_id",
                "error",
            ],
        )
        w.writeheader()
        w.writerows(memory_state_rows)
    memory_state = write_memory_state_summary(memory_state_rows, memory_state_summary_csv)
    run_memory_state.append(memory_state)
    print(f"Wrote {memory_state_csv}")
    print(f"Wrote {memory_state_summary_csv}")

    if test_mode:
        with validation_measurements_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "run_index",
                    "strategy",
                    "batch_index",
                    "op_id",
                    "op_kind",
                    "statement_wall_ms",
                    "query_id",
                    "measured_output",
                    "pre_probe_count",
                    "post_probe_count",
                ],
            )
            w.writeheader()
            w.writerows(validation_measurement_rows)
        print(f"Wrote {validation_measurements_csv}")

        with validation_state_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "run_index",
                    "strategy",
                    "batch_index",
                    "probe_point",
                    "table_name",
                    "row_count",
                    "probe_wall_ms",
                    "query_id",
                    "error",
                ],
            )
            w.writeheader()
            for row in validation_state_rows:
                w.writerow(
                    {
                        "run_index": run_idx,
                        "strategy": row["strategy"],
                        "batch_index": row["batch"],
                        "probe_point": row["probe_point"],
                        "table_name": row["table_name"],
                        "row_count": row["row_count"],
                        "probe_wall_ms": row["probe_wall_time_ms"],
                        "query_id": row["probe_query_id"],
                        "error": row.get("error", ""),
                    }
                )
        print(f"Wrote {validation_state_csv}")

        with validation_probe_overhead_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "run_index",
                    "strategy",
                    "batch_index",
                    "total_probe_wall_ms",
                    "num_probes",
                    "total_measured_wall_ms",
                ],
            )
            w.writeheader()
            for (strategy, batch), payload in sorted(probe_overhead.items()):
                w.writerow(
                    {
                        "run_index": run_idx,
                        "strategy": strategy,
                        "batch_index": batch,
                        "total_probe_wall_ms": payload["total_probe_wall_ms"],
                        "num_probes": payload["num_probes"],
                        "total_measured_wall_ms": payload["total_measured_wall_ms"],
                    }
                )
        print(f"Wrote {validation_probe_overhead_csv}")

    correctness_status = "NOT_RUN"
    correctness_message = "No correctness diff statements were executed."
    correctness_by_batch_rows: list[dict[str, object]] = []

    if bench_result_columns and bench_result_rows:
        with bench_results_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(bench_result_columns)
            w.writerows(bench_result_rows)
        print(f"Wrote {bench_results_csv}")

        col_index = {name: idx for idx, name in enumerate(bench_result_columns)}
        required = {"strategy", "batch", "phase", "rows"}
        if required.issubset(col_index):
            diff_rows = []
            mismatches: list[tuple[str, str, str]] = []
            per_batch: dict[tuple[str, str], dict[str, int | str]] = {}
            for row in bench_result_rows:
                phase = str(row[col_index["phase"]])
                if phase not in (
                    "eimer_row_count",
                    "baseline_row_count",
                    "diff_strat_minus_base",
                    "diff_base_minus_strat",
                ):
                    continue
                strategy = str(row[col_index["strategy"]])
                batch = str(row[col_index["batch"]])
                rows_val = int(row[col_index["rows"]])
                bucket = per_batch.setdefault(
                    (strategy, batch),
                    {
                        "strategy": strategy,
                        "batch_index": batch,
                        "eimer_row_count": 0,
                        "baseline_row_count": 0,
                        "diff_strat_minus_base": 0,
                        "diff_base_minus_strat": 0,
                    },
                )
                bucket[phase] = rows_val
                if phase not in ("diff_strat_minus_base", "diff_base_minus_strat"):
                    continue
                diff_rows.append(row)
                if rows_val != 0:
                    mismatches.append(
                        (
                            strategy,
                            batch,
                            str(rows_val),
                        )
                    )
            for (_strategy, _batch), payload in sorted(per_batch.items(), key=lambda item: (item[0][0], int(item[0][1]))):
                diff_a = int(payload.get("diff_strat_minus_base", 0))
                diff_b = int(payload.get("diff_base_minus_strat", 0))
                correctness_by_batch_rows.append(
                    {
                        "batch_index": payload["batch_index"],
                        "eimer_row_count": payload.get("eimer_row_count", 0),
                        "baseline_row_count": payload.get("baseline_row_count", 0),
                        "diff_strat_minus_base": diff_a,
                        "diff_base_minus_strat": diff_b,
                        "status": "PASSED" if diff_a == 0 and diff_b == 0 else "FAILED",
                    }
                )
            if diff_rows:
                if mismatches:
                    correctness_status = "FAILED"
                    lines = ["Found non-zero correctness diffs:"]
                    for strategy, batch, rows_val in mismatches:
                        lines.append(f"strategy={strategy} batch={batch} rows={rows_val}")
                    correctness_message = "\n".join(lines)
                else:
                    correctness_status = "PASSED"
                    correctness_message = "All diff_strat_minus_base and diff_base_minus_strat rows are zero."

    with correctness_txt.open("w", encoding="utf-8") as f:
        f.write(correctness_status + "\n")
        f.write(correctness_message + "\n")
        if correctness_by_batch_rows:
            f.write("\nPer-query-batch correctness:\n")
            f.write(
                "batch_index,eimer_row_count,baseline_row_count,"
                "diff_strat_minus_base,diff_base_minus_strat,status\n"
            )
            for row in correctness_by_batch_rows:
                f.write(
                    f"{row['batch_index']},{row['eimer_row_count']},{row['baseline_row_count']},"
                    f"{row['diff_strat_minus_base']},{row['diff_base_minus_strat']},{row['status']}\n"
                )
    print(f"Wrote {correctness_txt}")
    if correctness_by_batch_rows:
        with correctness_by_batch_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "batch_index",
                    "eimer_row_count",
                    "baseline_row_count",
                    "diff_strat_minus_base",
                    "diff_base_minus_strat",
                    "status",
                ],
            )
            w.writeheader()
            w.writerows(correctness_by_batch_rows)
        print(f"Wrote {correctness_by_batch_csv}")


# Average per (strategy,batch,phase)
avg_summary_csv = out_dir / "timings_summary_avg.csv"
keys = sorted(set().union(*[set(t.keys()) for t in run_totals]))
with avg_summary_csv.open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["strategy", "batch", "phase", "avg_total_time_ms"])
    for key in keys:
        vals = [t.get(key, 0) for t in run_totals]
        avg = sum(vals) / len(vals)
        w.writerow([key[0], key[1], key[2], int(round(avg))])
print(f"Wrote {avg_summary_csv}")


# Average peak query memory per (strategy,batch,phase)
avg_memory_query_csv = out_dir / "memory_query_summary_avg.csv"
memory_query_keys = sorted(set().union(*[set(d.keys()) for d in run_memory_query])) if run_memory_query else []
with avg_memory_query_csv.open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(
        [
            "strategy",
            "batch",
            "phase",
            "avg_peak_user_memory_bytes",
            "avg_peak_total_memory_bytes",
            "avg_peak_task_total_memory_bytes",
        ]
    )
    for key in memory_query_keys:
        vals = [d.get(key, (0, 0, 0)) for d in run_memory_query]
        avg_user = int(round(sum(v[0] for v in vals) / len(vals)))
        avg_total = int(round(sum(v[1] for v in vals) / len(vals)))
        avg_task = int(round(sum(v[2] for v in vals) / len(vals)))
        w.writerow([key[0], key[1], key[2], avg_user, avg_total, avg_task])
print(f"Wrote {avg_memory_query_csv}")


# Average approach state footprint per (strategy,batch)
avg_memory_state_csv = out_dir / "memory_state_summary_avg.csv"
memory_state_keys = sorted(set().union(*[set(d.keys()) for d in run_memory_state])) if run_memory_state else []
with avg_memory_state_csv.open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["strategy", "batch", "avg_total_state_rows", "avg_total_state_logical_bytes"])
    for key in memory_state_keys:
        vals = [d.get(key, (0, 0)) for d in run_memory_state]
        avg_rows = int(round(sum(v[0] for v in vals) / len(vals)))
        avg_bytes = int(round(sum(v[1] for v in vals) / len(vals)))
        w.writerow([key[0], key[1], avg_rows, avg_bytes])
print(f"Wrote {avg_memory_state_csv}")


# Per-strategy totals: sum over all benchmarked batches and phases
# (update/compose/post_filter/query, plus load/watermark).
totals_avg_csv = out_dir / "timings_totals_avg.csv"
perf_phases = ["load", "update", "compose", "post_filter", "query", "watermark"]
extra_phases = ["correctness"]


def per_run_strategy_totals(totals_map):
    out = defaultdict(int)
    for (strategy, batch, phase), ms in totals_map.items():
        if str(strategy) == "DATA":
            continue
        if not (1 <= int(batch) <= max_batch):
            continue
        if phase in perf_phases:
            out[(strategy, phase)] += ms
            out[(strategy, "total_perf")] += ms
            out[(strategy, "total_all")] += ms
        elif phase in extra_phases:
            out[(strategy, phase)] += ms
            out[(strategy, "total_all")] += ms
    return out


per_run = [per_run_strategy_totals(t) for t in run_totals]
all_keys = sorted(set().union(*[set(d.keys()) for d in per_run]))

with totals_avg_csv.open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["strategy", "phase", "avg_total_time_ms"])
    for k in all_keys:
        vals = [d.get(k, 0) for d in per_run]
        w.writerow([k[0], k[1], int(round(sum(vals) / len(vals)))])
print(f"Wrote {totals_avg_csv}")
PY

if [[ "$COST_MODEL_VALIDATION" -eq 1 ]]; then
  POST_ARGS=(
    --run-dir "$OUT_DIR"
    --selectivities "$SELECTIVITIES"
  )
  if [[ -n "$QUERY_SPEC_FILE" ]]; then
    POST_ARGS+=(--query-spec-file "$QUERY_SPEC_FILE")
  else
    POST_ARGS+=(--query-spec "$QUERY_SPEC")
  fi
  if [[ -n "$SELECTIVITY_MODE" ]]; then
    POST_ARGS+=(--selectivity-mode "$SELECTIVITY_MODE")
  fi
  python3 "$SCRIPT_DIR/cost_model_validation.py" "${POST_ARGS[@]}"
fi

echo "Done."
echo "Averages: $OUT_DIR/timings_summary_avg.csv"
echo "Memory peaks: $OUT_DIR/memory_query_summary_avg.csv"
echo "State footprint: $OUT_DIR/memory_state_summary_avg.csv"
echo "Totals (batches 1..$((UPDATES + 1))): $OUT_DIR/timings_totals_avg.csv"
if [[ "$COST_MODEL_VALIDATION" -eq 1 ]]; then
  echo "Validation CSVs: $OUT_DIR/run*_cost_validation.csv"
fi
if [[ "$DUMP_QEP" -eq 1 ]]; then
  echo "QEP dumps: $OUT_DIR/run*_qep/"
fi
if [[ "$DUMP_EXPLAIN_ANALYZE" -eq 1 ]]; then
  echo "EXPLAIN ANALYZE dumps: $OUT_DIR/run*_explain_analyze/"
fi
