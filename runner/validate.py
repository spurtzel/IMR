"""Config validation, run before materialization.

Invalid config raises ConfigError naming the violated constraint. validate_environment
and validate_experiment check config-level legality: backend required and known,
selectivity_mode in {b_unified, c}, independent selectivity per-type <= 100% and sum
<= 100%, feasibility floor at the declared (N, band-tightness), Kleene shape, band-
tightness range, stream/batch sanity. Deliberately no gap-vs-COUNT rule: a greedy gap
before a measured Kleene is valid operator semantics, not an illegal query.

Structural spec constraints (unique vars, pattern order, predicate arity, quantifiers)
are enforced by the central ,,validate_query_spec'' on the materialized QuerySpec;
datagen-side feasibility additionally runs inside datagen at generation time.

Self-test (no DB, no generation): ,,python3 -m runner.validate --self-test''.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "execution"))

from runner.schema import (  # noqa: E402
    ConfigError, EnvironmentConfig, ExperimentConfig)

VALID_BACKENDS = {"trino", "memory"}
VALID_SELECTIVITY_MODES = {"b_unified", "c"}
VALID_TOPOLOGIES = {"chain", "star", "cycle", "clique", "complete", "long_edge", "independent",
                    "triangle", "diamond"}  # topologies the sweep supports
VALID_QUERY_FREQUENCIES = {"all", "every2nd", "every4th", "every8th", "last"}
VALID_JOIN_ORDERS = {"canonical", "dp", "bushy"}
VALID_QUANTIFIERS = {"PLUS", "RELUCTANT_PLUS"}
VALID_GAPS = {"reluctant", "greedy"}
# spatial is the only predicate class the materializer supports
VALID_PREDICATE_CLASSES = {"spatial"}
# must match DatagenConfig.min_expected_pair_hits
MIN_EXPECTED_PAIR_HITS = 100
# the only cap keys the runner consumes; anything else in execution.caps is a typo and is rejected
VALID_CAP_KEYS = {"EIMER_CURATED_MAX_STRATEGIES", "EIMER_CURATED_MAX_VIEWS"}


def validate_environment(env: EnvironmentConfig) -> None:
    if env.backend not in VALID_BACKENDS:
        raise ConfigError(f"environment.backend {env.backend!r} is not one of "
                          f"{sorted(VALID_BACKENDS)} (and there is no default)")
    if env.parallelism.mode != "serial":
        raise ConfigError(f"environment.parallelism.mode {env.parallelism.mode!r} "
                          "must be 'serial'")
    if env.parallelism.containers < 1:
        raise ConfigError("environment.parallelism.containers must be >= 1")


def validate_experiment(exp: ExperimentConfig) -> None:
    q, d, ex = exp.query, exp.data, exp.execution

    # --- query section ---
    if q.pattern_length < 1:
        raise ConfigError("query.pattern_length must be >= 1")
    if q.topology not in VALID_TOPOLOGIES:
        raise ConfigError(f"query.topology {q.topology!r} is not one of {sorted(VALID_TOPOLOGIES)}")
    if q.predicate_class not in VALID_PREDICATE_CLASSES:
        raise ConfigError(f"query.predicate_class {q.predicate_class!r}: only "
                          f"{sorted(VALID_PREDICATE_CLASSES)} until the Stage-1 "
                          "materializer extension lands (non-spatial families are "
                          "schema-supported; generator-side work)")
    if q.kleene is not None:
        if q.kleene.count < 1:
            raise ConfigError("query.kleene.count must be >= 1 when kleene is declared")
        if q.kleene.quantifier not in VALID_QUANTIFIERS:
            raise ConfigError(f"query.kleene.quantifier {q.kleene.quantifier!r} is not one "
                              f"of {sorted(VALID_QUANTIFIERS)}")
        if q.kleene.gap not in VALID_GAPS:
            raise ConfigError(f"query.kleene.gap {q.kleene.gap!r} is not one of "
                              f"{sorted(VALID_GAPS)}")
        # No gap-vs-COUNT rule on purpose: a greedy gap before a measured Kleene variable
        # (COUNT collapses to 1) is a valid query with the correct result. The only related
        # guard is the emitter's _assert_kleene_measures_run_capturing on the costing path.

    # --- data section: the two-layer selectivity model, Layer 1 ---
    if not d.stream or any(s <= 0 for s in d.stream):
        raise ConfigError("data.stream must be a non-empty list of positive batch sizes "
                          "([initial_table_size, batch1, ...])")
    if not d.independent_selectivity:
        raise ConfigError("data.independent_selectivity must declare at least one type")
    for name, rate in d.independent_selectivity.items():
        if not 0.0 < rate <= 1.0:
            raise ConfigError(f"data.independent_selectivity[{name!r}] = {rate}: each "
                              "per-type value must be in (0, 1] (<= 100%)")
    total = sum(d.independent_selectivity.values())
    if total > 1.0 + 1e-12:
        raise ConfigError(f"data.independent_selectivity sums to {total:.4f} > 1.0: the "
                          "types cannot occupy more than the whole table (the rates are "
                          "arbitrary per-type upper bounds, but their SUM is capped at 100%)")
    n_types = len(d.independent_selectivity)
    total_vars = q.pattern_length + (q.kleene.count if q.kleene else 0)
    if total_vars > n_types:
        raise ConfigError(f"{total_vars} variables (pattern_length {q.pattern_length}"
                          f"{f' + {q.kleene.count} Kleene' if q.kleene else ''}) > {n_types} "
                          "declared types: variables bind types positionally "
                          "(var i -> types[i]), so the type set must cover every variable")
    if d.rho <= 0:
        raise ConfigError("data.rho must be > 0")
    if d.cluster_count < 1:
        raise ConfigError("data.cluster_count must be >= 1")

    # --- band geometry override + query schedule ---
    if d.band_half_widths is not None:
        if len(d.band_half_widths) != 2 or any(not 0.0 < w <= 1.0 for w in d.band_half_widths):
            raise ConfigError(f"data.band_half_widths {d.band_half_widths}: must be "
                              "[lon, lat] with each in (0, 1] (degrees)")
    if ex.query_frequency not in VALID_QUERY_FREQUENCIES:
        raise ConfigError(f"execution.query_frequency {ex.query_frequency!r} must be one "
                          f"of {sorted(VALID_QUERY_FREQUENCIES)}")
    if q.topology == "diamond" and q.pattern_length != 4:
        raise ConfigError("query.topology 'diamond' is a 4-variable topology "
                          f"(pattern_length {q.pattern_length})")

    # --- band_tightness: Layer 2, only TIGHTENS ---
    if exp.band_tightness is not None:
        if not 0.0 < exp.band_tightness <= 1.0:
            raise ConfigError(f"band_tightness {exp.band_tightness}: must be in (0, 1] "
                              "(it is the mode-c pairwise band probability target)")
        if q.predicate_class != "spatial":
            raise ConfigError("band_tightness declared but query.predicate_class is not "
                              "'spatial': the band dual has no non-spatial consumer")
        # Feasibility floor at the declared (N, sigma): the conjunction with the dependent
        # condition must keep the resolvable pair count above the statistical floor (same
        # rule datagen enforces at generation, checked here so config fails before any run).
        n = sum(d.stream)
        rarest = min(d.independent_selectivity.values())
        expected = (rarest * n) ** 2 * exp.band_tightness
        if expected < MIN_EXPECTED_PAIR_HITS:
            raise ConfigError(
                f"feasibility floor: expected matching pairs for the rarest type is "
                f"{expected:.1f} < {MIN_EXPECTED_PAIR_HITS} at N={n}, rarest rate "
                f"{rarest}, band_tightness {exp.band_tightness}, the target selectivity "
                "is statistically unresolvable; raise N/rates/band_tightness")

    # --- execution section ---
    unknown_caps = set(ex.caps) - VALID_CAP_KEYS
    if unknown_caps:
        raise ConfigError(f"execution.caps: unknown cap key(s) {sorted(unknown_caps)} "
                          f"(allowed: {sorted(VALID_CAP_KEYS)}): caps are never "
                          "silently ignored")
    if ex.selectivity_mode not in VALID_SELECTIVITY_MODES:
        raise ConfigError(f"execution.selectivity_mode {ex.selectivity_mode!r} is not one "
                          f"of {sorted(VALID_SELECTIVITY_MODES)} (mode 'c' is the O(N^2) "
                          "ground-truth validation reference; 'b_unified' is the "
                          "deployable estimator)")
    if ex.join_order not in VALID_JOIN_ORDERS:
        raise ConfigError(f"execution.join_order {ex.join_order!r} is not one of "
                          f"{sorted(VALID_JOIN_ORDERS)}")


# --------------------------------------------------------------------------- #
# Self-test: the negative acceptance cases (rejection is a FEATURE to test)
# --------------------------------------------------------------------------- #

def _valid_experiment_dict() -> dict:
    return {
        "query": {"pattern_length": 3, "topology": "chain"},
        "data": {"stream": [5000, 1000, 1000],
                 "independent_selectivity": {"ROBBERY": 0.10, "BATTERY": 0.10,
                                             "MOTOR VEHICLE THEFT": 0.15},
                 "seed": 4242},
        "band_tightness": 0.10,
    }


def _self_test() -> int:
    from runner.schema import EnvironmentConfig, ExperimentConfig
    failures = 0

    def expect_ok(label, fn):
        nonlocal failures
        try:
            fn()
            print(f"  [PASS] {label}")
        except ConfigError as exc:
            failures += 1
            print(f"  [FAIL] {label}: unexpectedly rejected: {exc}")

    def expect_reject(label, fn, needle):
        nonlocal failures
        try:
            fn()
            failures += 1
            print(f"  [FAIL] {label}: accepted, expected rejection mentioning {needle!r}")
        except ConfigError as exc:
            if needle in str(exc):
                print(f"  [PASS] {label} -> rejected: {str(exc)[:84]}")
            else:
                failures += 1
                print(f"  [FAIL] {label}: rejected but message lacks {needle!r}: {exc}")

    def exp(mutate=None):
        data = _valid_experiment_dict()
        if mutate:
            mutate(data)
        validate_experiment(ExperimentConfig.from_dict(data))

    def env(d):
        validate_environment(EnvironmentConfig.from_dict(d))

    # positives
    expect_ok("valid experiment (R:10% B:10% M:15%, sum 35%)", exp)
    expect_ok("valid environment (explicit trino)",
              lambda: env({"backend": "trino"}))
    # Kleene variables are named positionally after the conjunctive ones (here:
    # pattern A B + Kleene C, binding the 3 declared types).
    expect_ok("kleene reluctant + COUNT measure",
              lambda: exp(lambda d: d["query"].update(
                  pattern_length=2,
                  kleene={"count": 1, "quantifier": "PLUS", "gap": "reluctant"},
                  measures=["COUNT(C.id) AS c_count"])))
    # Greedy gap + COUNT is a valid query (COUNT collapses to 1): must be accepted.
    expect_ok("kleene greedy gap + COUNT measure (valid: run-eating semantics)",
              lambda: exp(lambda d: d["query"].update(
                  pattern_length=2,
                  kleene={"count": 1, "quantifier": "PLUS", "gap": "greedy"},
                  measures=["COUNT(C.id) AS c_count"])))

    # negatives: each must be rejected with the constraint NAMED
    expect_reject("missing backend",
                  lambda: env({}), "backend is REQUIRED")
    expect_reject("unknown backend",
                  lambda: env({"backend": "sqlite"}), "not one of")
    expect_reject("fleet on trino",
                  lambda: env({"backend": "trino",
                               "parallelism": {"mode": "fleet", "containers": 12}}),
                  "must be 'serial'")
    expect_reject("illegal selectivity mode",
                  lambda: exp(lambda d: d.setdefault("execution", {}).update(
                      selectivity_mode="a")), "selectivity_mode")
    expect_reject("independent selectivity > 100%",
                  lambda: exp(lambda d: d["data"]["independent_selectivity"].update(
                      ROBBERY=1.5)), "(0, 1]")
    expect_reject("independent selectivities summing > 100%",
                  lambda: exp(lambda d: d["data"]["independent_selectivity"].update(
                      ROBBERY=0.5, BATTERY=0.4, **{"MOTOR VEHICLE THEFT": 0.3})),
                  "SUM is capped")
    expect_reject("feasibility floor breach (tiny N x tight band)",
                  lambda: exp(lambda d: (d["data"].update(stream=[500]),
                                         d.update(band_tightness=0.001))),
                  "feasibility floor")
    expect_reject("band_tightness out of range",
                  lambda: exp(lambda d: d.update(band_tightness=1.5)), "(0, 1]")
    expect_reject("unknown key (typo'd knob)",
                  lambda: exp(lambda d: d["data"].update(cluster_ct=3)), "unknown key")
    expect_reject("pattern longer than type set",
                  lambda: exp(lambda d: d["query"].update(pattern_length=4)),
                  "positionally")
    expect_reject("bad topology",
                  lambda: exp(lambda d: d["query"].update(topology="ring")), "topology")

    print(f"\nself-test: {'ALL PASS' if failures == 0 else f'{failures} FAILURES'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(_self_test())
    print(__doc__)
