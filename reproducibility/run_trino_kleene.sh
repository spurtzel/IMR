#!/usr/bin/env bash
# Fig. 11 on Trino, Kleene panels: one Kleene view maintained by rescan vs. by the incremental stitch, and native
# MATCH_RECOGNIZE vs. EIMER without and with the stitch over the run length L. Runs on Trino's in-memory tables.
#   bash reproducibility/run_trino_kleene.sh                                        the small default
#   KN=20000 KB=20 LS=5,10,25,50,100 bash reproducibility/run_trino_kleene.sh       own axis values (SCALE=paper: those of our runs)
# Results: reproducibility/out/trino_kleene/ (records, summary.md, figs/trino_fig11_kleene.pdf). The exit code is 0 if every check passed. Parameters: reproducibility/README.md.
export EXPERIMENT_CATALOG=memory CATALOG="${CATALOG:-memory}"
exec "$(dirname "$0")/docker/run_trino_experiment.sh" kleene
