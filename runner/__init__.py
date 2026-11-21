"""Config runner: experiment.yaml + environment.yaml
in, one validated, materialized, executed, recorded experiment out.

  schema.py     : experiment/environment schema (unknown keys rejected,
                   backend required).
  validate.py   : config-level constraints (selectivity_mode in {b_unified, c},
                   per-type <=100% / sum <=100%, feasibility floor, band-tightness
                   range, Kleene shape).
  materialize.py: derives both the query spec and the DatagenConfig from one config.
  run.py        : single-run orchestrator: validate -> materialize -> load ->
                   sigma -> select (clique_grow/edge_cover) -> execute the pick ->
                   collect (experiment_manifest.json + candidate_rows.csv).

Unsupported capture combinations raise an error.
"""
