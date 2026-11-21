#!/usr/bin/env python3
"""EIMER's selected plan rendered as an Esper EPL module.

Two statement kinds: per maintained view a continuous
`insert into V_<vars> select ... from DriveEvent as y unidirectional, <sources> where ...` (the batch's rows of the
view's last variable drive the update; history singletons read `Hist`, prefix views read their window; the
batch-window prefix prune bounds every non-batch source), and one `select ... from ComposeTrigger as t
unidirectional, <nodes> where ...` (the compose over the cover, one trigger per batch). The per-start selection
(SQL's ROW_NUMBER = 1) is applied by the Java program (`java/`) to the compose's candidate rows. The native
approach's statement is rendered here from the same predicates. Columns are prefixed `<Var>_<col>` as in EIMER's
SQL views; timestamps are long milliseconds.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

EIMER_REPO = Path(os.environ.get("EIMER_REPO", Path(__file__).resolve().parents[2]))
for _p in (str(EIMER_REPO), str(EIMER_REPO / "execution"), str(EIMER_REPO / "demo")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from eimer.query.temporal_propagation import implied_windows  # noqa: E402

_UNIT_MS = {"MILLISECOND": 1.0, "SECOND": 1000.0, "MINUTE": 60000.0, "HOUR": 3600000.0}
_ABS_DATE_DIFF = re.compile(r"abs\s*\(\s*date_diff\s*\(\s*'(?P<unit>\w+)'\s*,\s*(?P<v1>\w+)\.ts\s*,\s*(?P<v2>\w+)\.ts\s*\)\s*\)\s*<=\s*(?P<bound>\d+(?:\.\d+)?)", re.I)
_INTERVAL = re.compile(r"(?P<vl>\w+)\.ts\s*<=\s*(?P<vr>\w+)\.ts\s*\+\s*INTERVAL\s*'(?P<bound>\d+(?:\.\d+)?)'\s*(?P<unit>\w+)", re.I)
_QUALIFIED = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b")


def _ms(bound: str, unit: str) -> str:
    v = float(bound) * _UNIT_MS[unit.upper().rstrip("S")]
    return str(int(v)) if v.is_integer() else repr(v)


def render_condition(text: str, col, positions: Dict[str, int], form: str = "diff") -> str:
    """One SQL predicate of the dependency graph as EPL. `col(var, column)` renders a variable's column in the
        statement at hand. Window forms become a millisecond difference in pattern order (`form="diff"`,
        `later.ts - earlier.ts <= W`) or, with `form="range"`, `later.ts <= earlier.ts + W`. Every other
        predicate (bands as BETWEEN, equalities, comparisons) keeps its SQL surface with the qualified
        references re-rendered.
    """
    assert form in ("diff", "range", "range_both"), form

    def window(later, earlier, bound):
        if form == "range":
            return f"{col(later, 'ts')} <= {col(earlier, 'ts')} + {bound}"
        if form == "range_both":
            return f"{col(later, 'ts')} <= {col(earlier, 'ts')} + {bound} and {col(earlier, 'ts')} >= {col(later, 'ts')} - {bound}"
        return f"{col(later, 'ts')} - {col(earlier, 'ts')} <= {bound}"
    m = _ABS_DATE_DIFF.search(text)
    if m and text.strip() == m.group(0):
        v1, v2 = m.group("v1"), m.group("v2")
        earlier, later = sorted((v1, v2), key=lambda v: positions[v])
        return window(later, earlier, _ms(m.group('bound'), m.group('unit')))
    m = _INTERVAL.search(text)
    if m and text.strip() == m.group(0):
        return window(m.group('vl'), m.group('vr'), _ms(m.group('bound'), m.group('unit')))
    if "date_diff" in text.lower() or "interval" in text.lower():
        raise ValueError(f"unsupported temporal predicate form for EPL: {text!r}")
    return _QUALIFIED.sub(lambda q: col(q.group(1), q.group(2)) if q.group(1) in positions else q.group(0), text)


def _var_order(vars_, positions):
    return sorted(vars_, key=lambda v: positions[v])


def _schema_fields(columns: Dict[str, str], prefix: str = "") -> str:
    return ", ".join(f"{prefix}{c} {t}" for c, t in columns.items())


def _leaves(tree) -> list:
    return [tree.node] if tree.node is not None else _leaves(tree.left) + _leaves(tree.right)


def _linearize(tree, driver) -> list:
    """The source tree as a driver-first chain: at every internal node the subtree holding the driver comes first, the
        other subtree follows in its own order.
    """
    if tree is None:
        return []
    if tree.node is not None:
        return [tree.node]
    left, right = _leaves(tree.left), _leaves(tree.right)
    first, second = (tree.left, tree.right) if driver in left else (tree.right, tree.left)
    return _linearize(first, driver) + _linearize(second, driver)


def _chain_update(target, last_var, last_source, others, tree, tv, edges, bounds, vcols, types, positions, view_name, name, upd,
                  cform, sfilter) -> list:
    """The view's update as a chain of two-way unidirectional joins (see `render_plan`, `update_form="chain"`)."""
    order = [s for s in _linearize(tree, last_source) if s != last_source] if tree is not None else others
    assert sorted(map(sorted, order)) == sorted(map(sorted, others)), (order, others)
    lines = []
    have = {last_var}
    stream = "DriveEvent as y unidirectional"
    ystream = True
    for i, s in enumerate(order):
        last = i == len(order) - 1
        sv = _var_order(s, positions)
        if len(s) == 1:
            v = sv[0]; a = f"x_{v}"
            src = f"Hist(primary_type = '{types[v]}') as {a}" if sfilter else f"Hist as {a}"
            where = [] if sfilter else [f"{a}.primary_type = '{types[v]}'"]
            ncol = lambda v_, c, _a=a: f"{_a}.{c}"
        else:
            a = "s_" + "_".join(sv); src = f"{view_name(s)} as {a}"; where = []
            ncol = lambda v_, c, _a=a: f"{_a}.{v_}_{c}"
        if ystream:
            where.insert(0, f"y.primary_type = '{types[last_var]}'")
        lcol = (lambda v_, c: f"y.{c}") if ystream else (lambda v_, c: f"l.{v_}_{c}")
        bmin = "y.batch_min_ts" if ystream else "l.batch_min_ts"
        now = have | set(s)
        def col(v_, c, _n=s, _l=lcol, _c=ncol):
            return _c(v_, c) if v_ in _n else _l(v_, c)
        for a_, b_ in zip(tv, tv[1:]):
            if a_ in now and b_ in now and (a_ in s or b_ in s):
                where.append(f"{col(a_, 'ts')} < {col(b_, 'ts')}")
        for l_, r_, conds in edges:
            if l_ in now and r_ in now and (l_ in s or r_ in s) and l_ in tv and r_ in tv:
                where += [render_condition(c, col, positions, cform) for c in conds]
        for v in sv:
            if v != last_var and (v, last_var) in bounds:
                where.append(f"{col(v, 'ts')} >= {bmin} - {int(bounds[(v, last_var)] + 0.999999)}")
        carried = [f"{lcol(v, c)} as {v}_{c}" for v in _var_order(have, positions) for c in vcols[v]]
        new = [f"{ncol(v, c)} as {v}_{c}" for v in sv for c in vcols[v]]
        if last:
            proj = ", ".join(f"{col(v, c)} as {v}_{c}" for v in tv for c in vcols[v])
            lines.append(f"@name('{upd}') insert into {name} select {proj} from {stream}, {src} where {' and '.join(where)};")
        else:
            nxt = f"U_{'_'.join(tv)}_{i + 1}"
            fields = ", ".join(f"{v}_{c} {t}" for v in _var_order(now, positions) for c, t in vcols[v].items()) + ", batch_min_ts long"
            lines.append(f"@public @buseventtype create map schema {nxt}({fields});")
            lines.append(f"@name('{upd}_s{i + 1}') insert into {nxt} select {', '.join(carried + new)}, {bmin} as batch_min_ts "
                         f"from {stream}, {src} where {' and '.join(where)};")
            stream = f"{nxt} as l unidirectional"; ystream = False
        have = now
    return lines


def render_plan(dep, toks: Sequence[str], o, nodes, *, w_ms: int, types: Dict[str, str], columns: Dict[str, str],
                prune: bool = True, native_form: str = "stated", eimer_render: str = "diff", update_form: str = "single") -> dict:
    """The plan.json content for the Java program. `o`, `nodes` from `eimer_arm.build_cover_plan`.
        native_form: "stated" = the query as stated (the spec's declared predicates only, the wildcard gap
        unbounded); "bounded" = every propagated condition in the DEFINEs
        and the gap bounded by the window (`Z as Z.ts - first.ts <= W`).
    """
    assert native_form in ("stated", "bounded"), native_form
    assert eimer_render in ("diff", "range", "range_both", "range_both_filter"), eimer_render
    assert update_form in ("single", "chain"), update_form
    cform = {"diff": "diff", "range": "range", "range_both": "range_both", "range_both_filter": "range_both"}[eimer_render]
    sfilter = eimer_render == "range_both_filter"
    positions = dep.positions
    variables = _var_order(dep.variables, positions)
    up, cp = o.update_plans[0], o.composition_plans[0]
    for v in variables:
        assert dep.independent_conditions.get(v) == [f"{v}.primary_type = '{types[v]}'"], (v, dep.independent_conditions.get(v), types)
    used_cols = {v: {"id", "ts"} for v in variables}
    for e in dep.edges:
        for c in e.dependent_conditions:
            for q in _QUALIFIED.finditer(c):
                if q.group(1) in used_cols:
                    used_cols[q.group(1)].add(q.group(2))
    vcols = {v: {c: t for c, t in columns.items() if c in used_cols[v]} for v in variables}
    bounds = implied_windows(dep, conservative=True) if prune else {}
    indep = dep.independent_conditions
    edges = [(e.var_left, e.var_right, list(e.dependent_conditions)) for e in dep.edges]
    view_name = lambda v: "V_" + "_".join(_var_order(v, positions))
    lines: List[str] = [
        f"@public @buseventtype create map schema RowEvent({_schema_fields(columns)});",
        f"@public @buseventtype create map schema DriveEvent({_schema_fields(columns)}, batch_min_ts long);",
        "@public @buseventtype create map schema ComposeTrigger(b int);",
        "@public create window Hist#keepall as RowEvent;",
        "insert into Hist select * from RowEvent;",
    ]
    views = []
    for target in up.topological_order:
        if len(target) < 2:
            continue
        tv = _var_order(target, positions)
        last_source = up.last_variable_source[target]
        assert len(last_source) == 1, f"EPL update requires a singleton last-variable source, got {sorted(last_source)}"
        last_var = next(iter(last_source))
        sources = sorted(up.effective_sources[target], key=lambda s: (len(s), _var_order(s, positions)))
        alias_of: Dict[str, str] = {}
        prefixed: Dict[str, bool] = {}
        from_parts = ["DriveEvent as y unidirectional"]
        where: List[str] = [f"y.primary_type = '{types[last_var]}'"]
        alias_of[last_var] = "y"; prefixed[last_var] = False
        for s in sources:
            if s == last_source:
                continue
            if len(s) == 1:
                v = next(iter(s)); a = f"x_{v}"
                alias_of[v] = a; prefixed[v] = False
                if sfilter:
                    from_parts.append(f"Hist(primary_type = '{types[v]}') as {a}")
                else:
                    from_parts.append(f"Hist as {a}"); where.append(f"{a}.primary_type = '{types[v]}'")
            else:
                a = "s_" + "_".join(_var_order(s, positions))
                from_parts.append(f"{view_name(s)} as {a}")
                for v in s:
                    alias_of[v] = a; prefixed[v] = True

        def col(v, c, _a=alias_of, _p=prefixed):
            return f"{_a[v]}.{v}_{c}" if _p[v] else f"{_a[v]}.{c}"
        for a_, b_ in zip(tv, tv[1:]):
            where.append(f"{col(a_, 'ts')} < {col(b_, 'ts')}")
        for l, r, conds in edges:
            if l in target and r in target:
                where += [render_condition(c, col, positions, cform) for c in conds]
        for v in tv:
            if v != last_var and (v, last_var) in bounds:
                where.append(f"{col(v, 'ts')} >= y.batch_min_ts - {int(bounds[(v, last_var)] + 0.999999)}")
        proj = ", ".join(f"{col(v, c)} as {v}_{c}" for v in tv for c in vcols[v])
        schema = ", ".join(f"{v}_{c} {t}" for v in tv for c, t in vcols[v].items())
        name = view_name(target); upd = "upd_" + "_".join(tv)
        others = [s for s in sources if s != last_source]
        if update_form == "chain" and len(others) >= 2:
            lines += [f"@public create map schema {name}_row({schema});", f"@public create window {name}#keepall as {name}_row;"]
            lines += _chain_update(target, last_var, last_source, others, up.source_join_trees.get(target) if up.source_join_trees else None,
                                   tv, edges, bounds, vcols, types, positions, view_name, name, upd, cform, sfilter)
            views.append({"name": name, "vars": tv, "update_statement": upd, "update_form": "chain"})
            continue
        lines += [f"@public create map schema {name}_row({schema});",
                  f"@public create window {name}#keepall as {name}_row;",
                  f"@name('{upd}') insert into {name} select {proj} from {', '.join(from_parts)} where {' and '.join(where)};"]
        views.append({"name": name, "vars": tv, "update_statement": upd, "update_form": "single"})

    order: List = []
    visited = set()
    if cp.join_order:
        root = cp.join_order[0][0]; order.append(root); visited.add(root)
        for a_, b_ in cp.join_order:
            for n in (a_, b_):
                if n not in visited:
                    order.append(n); visited.add(n)
    for n in sorted(nodes, key=lambda n: _var_order(n, positions)):
        if n not in visited:
            order.append(n); visited.add(n)

    def node_alias(n):
        return ("n_" + "_".join(_var_order(n, positions))) if len(n) > 1 else f"n_{next(iter(n))}"

    def node_from(n):
        a = node_alias(n)
        if len(n) > 1:
            return f"{view_name(n)} as {a}", a
        return (f"Hist(primary_type = '{types[next(iter(n))]}') as {a}" if sfilter else f"Hist as {a}"), a

    def node_col(n, a, v, c):
        return f"{a}.{v}_{c}" if len(n) > 1 else f"{a}.{c}"

    applied = set()
    for n in nodes:
        for l, r, conds in edges:
            if l in n and r in n:
                applied.update(conds)
    have: Dict[str, Tuple[str, bool]] = {}
    stream = None
    step_names = []
    for i, n in enumerate(order):
        frm, a = node_from(n)
        where: List[str] = []
        if len(n) == 1 and not sfilter:
            v = next(iter(n)); where.append(f"{a}.primary_type = '{types[v]}'")
        if i == 0:
            left_from = "ComposeTrigger as t unidirectional"
            left_cols: List[str] = []
        else:
            left_from = f"{stream} as l unidirectional"
            left_cols = [f"l.{v}_{c} as {v}_{c}" for v in _var_order(have, positions) for c in vcols[v]]

        def ccol(v, c, _n=n, _a=a):
            return f"l.{v}_{c}" if v in have else node_col(_n, _a, v, c)
        for v in _var_order(n, positions):
            if v in have:
                where.append(f"{node_col(n, a, v, 'id')} = l.{v}_id")
        now = set(have) | set(n)
        for x, y in zip(variables, variables[1:]):
            if x in now and y in now and (x in n or y in n):
                where.append(f"{ccol(x, 'ts')} < {ccol(y, 'ts')}")
        for l_, r_, conds in edges:
            if l_ in now and r_ in now and (l_ in n or r_ in n):
                for c in conds:
                    if c not in applied:
                        where.append(render_condition(c, ccol, positions, cform)); applied.add(c)
        new_cols = [f"{node_col(n, a, v, c)} as {v}_{c}" for v in _var_order(n, positions) if v not in have for c in vcols[v]]
        select = ", ".join(left_cols + new_cols)
        last = i == len(order) - 1
        if last:
            lines.append(f"@name('compose') select {select} from {left_from}, {frm}" + (f" where {' and '.join(where)}" if where else "") + ";")
        else:
            stream = f"C{i + 1}"
            lines.append(f"@public @buseventtype create map schema {stream}({', '.join(f'{v}_{c} {t}' for v in _var_order(now, positions) for c, t in vcols[v].items())});")
            lines.append(f"@name('compose_step{i + 1}') insert into {stream} select {select} from {left_from}, {frm}" + (f" where {' and '.join(where)}" if where else "") + ";")
            step_names.append(f"compose_step{i + 1}")
        for v in n:
            have.setdefault(v, (a, len(n) > 1))
    keys = [f"{v}_id" for v in variables]

    declared = [(e.var_left, e.var_right, [p.text for p in e.dependent_predicates if native_form == "bounded" or p.source_variable])
                for e in dep.edges]
    defs = []
    first = variables[0]
    for v in variables:
        conds = [render_condition(c, lambda x, cc: f"{x}.{cc}", positions) for c in indep.get(v, [])]
        for l, r, cs in declared:
            later = max((l, r), key=lambda x: positions[x])
            if later == v:
                conds += [render_condition(c, lambda x, cc: f"{x}.{cc}", positions) for c in cs]
        defs.append(f"{v} as {' and '.join(conds) if conds else 'true'}")
    if native_form == "bounded":
        defs.append(f"Z as Z.ts - {first}.ts <= {w_ms}")
    pattern = " Z* ".join(variables)
    measures = ", ".join(f"{v}.id as {v}_id" for v in variables)
    native = [f"@public @buseventtype create map schema RowEvent({_schema_fields(columns)});",
              "@public create window Hist#keepall as RowEvent;", "insert into Hist select * from RowEvent;",
              f"@name('mr') @Hint('iterate_only') select * from Hist match_recognize (measures {measures} all matches "
              f"after match skip to next row pattern ({pattern}) define {', '.join(defs)});"]
    return {"variables": variables, "key_columns": keys, "columns": columns, "w_ms": w_ms, "types": types,
            "cover": list(toks),
            "eimer": {"module": "\n".join(lines), "views": views, "compose_statement": "compose", "render": eimer_render, "update_form": update_form},
            "native": {"module": "\n".join(native), "statement": "mr", "form": native_form}}


if __name__ == "__main__":
    import common, eimer_arm
    from eimer.query.query_spec import load_query_spec_file, query_spec_to_dependency_graph
    spec = load_query_spec_file(sys.argv[1]); dep = query_spec_to_dependency_graph(spec)
    toks = sys.argv[2].split("|"); wl = common.const_sigma_workload(dep, total_events=1000, batches=2, sigma=0.2)
    o, plan, nodes = eimer_arm.build_cover_plan(dep, toks, set(dep.variables), wl)
    types = {v: f"T{i % 5}" for i, v in enumerate(sorted(dep.variables, key=lambda v: dep.positions[v]))}
    out = render_plan(dep, toks, o, nodes, w_ms=int(sys.argv[3]), types=types, eimer_render=os.environ.get("EIMER_RENDER", "diff"),
                      update_form=os.environ.get("UPDATE_FORM", "single"),
                      columns={"id": "long", "ts": "long", "primary_type": "string", "lon": "double", "lat": "double"})
    print(json.dumps(out, indent=1) if len(sys.argv) < 5 else out["eimer"]["module"] + "\n\n" + out["native"]["module"])
