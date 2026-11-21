#!/usr/bin/env python3
"""Transpiled baseline SQL for a windowed chain spec.

The baseline is the SQL that the MATCH_RECOGNIZE-to-SQL transpiler of Lambrecht et al. ("Democratize MATCH_RECOGNIZE!",
PVLDB 18(12), 2025) generates for the query, obtained through the Compiler-Explorer API of the authors' public demo
(https://db.cs.uni-tuebingen.de/matchrecognize). It is shipped under queries/<spec>.duckdb.sql and needs no network
access. <spec>.mr.sql is the query EIMER runs, <spec>.transpiler_input.sql the text sent to the transpiler.

The text sent to the transpiler is a semantics-preserving respelling of the spec's MATCH_RECOGNIZE:
  * `X.c BETWEEN Y.c - w AND Y.c + w`  ->  `X.c >= Y.c - w AND X.c <= Y.c + w`
  * `Z AS TRUE`  ->  `Z AS date_diff('millisecond', A.ts, Z.ts) <= W`  (the gap bounded by the pattern's window)
  * after transpiling, the lower-cased literals are restored to the spec's spelling.
The input reads the relation `events_window` (duckdb_exec.baseline_streaming re-points it per batch).

A spec without a shipped file is fetched from the demo if EIMER_REPRO_FETCH_TRANSPILER=1 is set.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = "https://db.cs.uni-tuebingen.de/matchrecognize/api/compiler/sql/compile"
_BETWEEN = re.compile(r"(\w+\.\w+)\s+BETWEEN\s+(\w+\.\w+)\s*-\s*([0-9.]+)\s+AND\s+\2\s*\+\s*\3")


def respell_for_transpiler(mr_sql: str, first_var: str, w_ms: int) -> str:
    sql = _BETWEEN.sub(r"\1 >= \2 - \3 AND \1 <= \2 + \3", mr_sql)
    sql = re.sub(r"\bZ AS TRUE\b", f"Z AS date_diff('millisecond', {first_var}.ts, Z.ts) <= {w_ms}", sql)
    assert "BETWEEN" not in sql and "Z AS TRUE" not in sql, sql
    return sql.rstrip().rstrip(";") + ";\n"


def restore_literal_case(transpiled: str, original: str) -> str:
    """map each lower-cased string literal of the transpiler output back to the original spelling (T0, T1, ...)."""
    lits = set(re.findall(r"'([^']*)'", original))
    lower = {}
    for lit in lits:
        assert lit.lower() not in lower or lower[lit.lower()] == lit, "ambiguous literal case"
        lower[lit.lower()] = lit
    return re.sub(r"'([^']*)'", lambda m: f"'{lower.get(m.group(1), m.group(1))}'", transpiled)


def transpile_text(src: str) -> str:
    body = json.dumps({"source": src, "compiler": "sql", "lang": "sql",
                       "options": {"userArguments": "", "compilerOptions": {"executorRequest": False},
                                   "filters": {"binary": False, "execute": False, "labels": False, "directives": False,
                                               "commentOnly": False, "trim": False, "intel": False, "demangle": False},
                                   "tools": [], "libraries": []}, "allowStoreCodeDebug": False}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    d = json.loads(urllib.request.urlopen(req, timeout=180).read())
    out = "\n".join(x.get("text", "") for x in d.get("asm", []))
    if d.get("code") != 0 or out.strip() == "<Compilation failed>":
        raise RuntimeError("TRANSPILE FAILED: " + " | ".join(x.get("text", "") for x in d.get("stdout", []) + d.get("stderr", []))[:800])
    return out


def transpiled_sql_for(spec, first_var: str, w_ms: int) -> tuple[str, Path]:
    """(sql, path) of the spec's transpiled baseline."""
    from eimer.query.query_spec import query_spec_to_match_recognize_sql
    qdir = HERE / "queries"; qdir.mkdir(exist_ok=True)
    out = qdir / f"{spec.name}.duckdb.sql"
    if out.exists():
        return out.read_text(), out
    if os.environ.get("EIMER_REPRO_FETCH_TRANSPILER") != "1":
        raise FileNotFoundError(f"no shipped transpiler output for {spec.name} ({out}); set EIMER_REPRO_FETCH_TRANSPILER=1 to "
                                f"fetch it from the transpiler's public demo")
    mr = query_spec_to_match_recognize_sql(spec, events_table="events_window")
    src = respell_for_transpiler(mr, first_var, w_ms)
    (qdir / f"{spec.name}.transpiler_input.sql").write_text(src)
    (qdir / f"{spec.name}.mr.sql").write_text(mr)
    sql = restore_literal_case(transpile_text(src), src)
    out.write_text(sql + "\n")
    return sql, out


def bounded_gap_in_input(spec_name: str) -> bool | None:
    """whether the transpiler input states the window on the gap variable."""
    p = HERE / "queries" / f"{spec_name}.transpiler_input.sql"
    if not p.exists():
        return None
    return bool(re.search(r"\bZ AS date_diff\('millisecond', \w+\.ts, Z\.ts\) <= \d+", p.read_text()))


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    import cells
    for k in cells.AXES["k"][1]:
        spec, dep, _ = cells.load_spec(k, cells.CENTER["w_ms"])
        _sql, path = transpiled_sql_for(spec, spec.variable_names[0], cells.CENTER["w_ms"])
        print(path.name, len(_sql.splitlines()), "lines; gap bounded by the window in the input:", bounded_gap_in_input(spec.name))
