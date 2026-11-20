#!/usr/bin/env bash
# The DuckDB and Esper figures from the records of a run:  render_figures.sh duckdb|esper <out dir>
# Writes <out dir>/figs/duckdb_N_k.{pdf,png} (Fig. 8b) or <out dir>/figs/esper_N_k.{pdf,png} (Fig. 8c). With a LaTeX
# install the text is typeset as in the paper, without one matplotlib's own fonts are used.
set -euo pipefail
cd "$(dirname "$0")/../../.."
WHICH="$1"; OUT="$(cd "$2" && pwd)"; FIGS="$OUT/figs"; mkdir -p "$FIGS"
cd reproducibility/experiments/figures
# variables of the other figure / other variants must not leak in
unset DUCKDB_RESULTS ESPER_RESULTS ESPER_K_FROM ESPER_BOUNDED ESPER_UNBOUNDED ESPER_STRATEGIES MEM STATE_UNIT PEAKMEM_DIR \
      ESPER_PEAKMEM_DIR ESPER_UNBOUNDED_PEAKMEM_DIR ESPER_JVMBASE_DIR DUCKDB_N_AXIS ESPER_N_AXIS BAR_ARMS DUCKDB_MR_COLOR \
      ESPER_WBK_FROM ESPER_WBK_CENTER ESPER_WBK_UNBOUNDED ESPER_WBK_PEAKMEM_DIR ESPER_WBK_UNBOUNDED_PEAKMEM_DIR ESPER_WBK_JVMBASE_DIR \
      DUCK_SPLIT LEGEND_STRIP GROUP_INSIDE ESPER_GROUPED_LEGEND PANELS ROW_ONLY NAME_SUFFIX Y_TIGHT MEM_MINOR UNSHARE_Y

# FIG_LEGEND_ROOM=1 (set by the reproduce scripts for runs below the paper's scale): the upper part of the panels is kept
# free, so the in-panel legend does not cover the few points of a small run. Unset = the paper's figure, unchanged.
ROOM="${FIG_LEGEND_ROOM:-}"

mem_dir() { [ -d "$1/records" ] && echo "$1" || true; }   # a memory root only if that stage ran

case "$WHICH" in
  duckdb)
    PEAK="$(mem_dir "$OUT/peakmem")"
    env DUCK_SPLIT=pctonly PCT_LABEL='query time (\% of total)' DUCK_MR_LABEL='DuckDB MR Transpiler' \
        DUCKDB_RESULTS="$OUT/runtime" DUCKDB_N_AXIS=N1000 DUCKDB_MR_COLOR=yellow ESPER_STRATEGIES=1 STATE_UNIT=mb \
        ${PEAK:+PEAKMEM_DIR="$PEAK"} ROW_ONLY=duckdb PANELS=N,k \
        SPLIT_LEG_HL=2.0 SPLIT_LEG_TP=0.4 ROW_W=9.7 ROW_H=3.73 SPLIT_LEGEND_FONTSIZE=16 SPLIT_LEG_BP=0.3 SPLIT_LEG_AP=0.25 \
        PCT_FONTSIZE=14 PCT_DY=12 ${ROOM:+ROW_LEGEND_FRACTION=0.42} PLOT_OUT_DIR="$FIGS/all" python3 plot_paper.py
    for ext in pdf png; do cp "$(ls -t "$FIGS"/all/*_duckdb_panels*."$ext" | head -1)" "$FIGS/duckdb_N_k.$ext"; done
    ;;
  esper)
    N="$OUT/fig8_s005"; K="$OUT/fig8_s005_n25k"
    NM="$(mem_dir "${N}_mem")"; NB="$(mem_dir "${N}_jvmbase")"; KM="$(mem_dir "${K}_mem")"; KB="$(mem_dir "${K}_jvmbase")"
    env ESPER_RESULTS="$N" ESPER_N_AXIS=N200 ESPER_STRATEGIES=1 STATE_UNIT=mb \
        ${NM:+ESPER_PEAKMEM_DIR="$NM"} ${NB:+ESPER_JVMBASE_DIR="$NB"} ROW_ONLY=esper LEGEND_STRIP=1 \
        BAR_GROUP=0.8 BAR_FILL=0.85 MEM_TOP=60 ESPER_GROUPED_LEGEND=1 \
        ESPER_WBK_FROM="$K" ESPER_WBK_CENTER=fig8s005n25k ${KM:+ESPER_WBK_PEAKMEM_DIR="$KM"} ${KB:+ESPER_WBK_JVMBASE_DIR="$KB"} \
        PANELS=N,k GROUP_NCOL=1,2 Y_TIGHT=1 MEM_MINOR=1 ROW_W=9.7 ROW_H=3.73 GROUP_INSIDE=1 GROUP_TITLE_BASE='\textbf{Baseline:}' \
        GROUP_IN_SIZE=13.5 GROUP_IN_NCOL=2 GROUP_IN_GAP=0.03 GROUP_IN_PAD=0.25 ESPER_NATIVE_LABEL='Esper MR' ESPER_PREFIX_LABEL=Prefixes \
        ${ROOM:+ROW_LEGEND_FRACTION=0.52} PLOT_OUT_DIR="$FIGS/all" python3 plot_paper.py
    for ext in pdf png; do cp "$(ls -t "$FIGS"/all/*_esper_panels*."$ext" | head -1)" "$FIGS/esper_N_k.$ext"; done
    ;;
  *) echo "usage: render_figures.sh duckdb|esper <out dir>"; exit 2 ;;
esac
echo "wrote $FIGS/${WHICH}_N_k.{pdf,png}"
