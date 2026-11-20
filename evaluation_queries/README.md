# Queries of the evaluation

The queries used in the paper's evaluation: one directory per experiment and, inside it, one per query (in
`selection_time/` grouped by topology). For every query:

- `<name>.sql`: the `MATCH_RECOGNIZE` statement as EIMER renders it from the specification. The queries of the DuckDB
  and Esper experiments write a time window with `date_diff`, all others as a range on the timestamp.
- `<name>.json`: the query specification EIMER compiles (variables, pattern with gap quantifiers, independent
  and dependent conditions, measures, result keys). Load it with `eimer.query.query_spec.load_query_spec_file`.
- `<name>_graph.png`: the query graph. Nodes are pattern variables with their independent condition and
  Kleene quantifier; solid arrows are the sequence constraints of the pattern, labelled with the gap quantifier
  (`Z*` greedy, `Z*?` reluctant); dashed edges are the dependent conditions (blue band, green equality, pink
  mixed), dotted orange edges are time windows.

These three files state a query as written: a time window is one condition between the two variables it relates.
The experiments run every query that has a time window in its window-propagated form, which returns the same
result. Such a query has two more entries:

- `<name>_window_propagated.sql`: the statement with the window also stated on the gap variable
  (for example `Z AS Z.ts <= A.ts + W`), which bounds every match attempt; this is the form in which native
  `MATCH_RECOGNIZE` is run. On Esper, the native statement also states the derived bounds.
- `<name>_window_propagated_graph.png`: the query graph with the bounds that follow from the window and
  the pattern's order for the other pairs of variables (marked "derived"); EIMER's views and
  GDYN's pair conditions carry them. The bound on the gap rows is stated under the title.

For Fig. 8b, `<name>_transpiler_input.sql` is the statement given to the transpiler (the window on the gap
variable, the bands as two inequalities).

`kleene_patterns/kleene_single_variable_Cplus/` holds only a statement: the left panel of Fig. 11a maintains the
runs of one Kleene variable, a query without dependent conditions and therefore without a specification or graph.


| section | experiment | directory |
|---|---|---|
| §7.2 | Feasibility across engines (Fig. 8a) | `7.2_state_of_the_art/feasibility_across_engines/` |
| §7.2 | MR-to-SQL transpilation (Fig. 8b) | `7.2_state_of_the_art/mr_to_sql_transpilation/` |
| §7.2 | CEP engine (Fig. 8c) | `7.2_state_of_the_art/cep_engine/` |
| §7.2 | Prefilter acceleration (Fig. 9) | `7.2_state_of_the_art/prefilter_acceleration/` |
| §7.3 | Cost model, pick quality, ablation, robustness to statistics (Tables 2 and 3) | `7.3_plan_selection/cost_model_and_pick_quality/` |
| §7.3 | Selection time | `7.3_plan_selection/selection_time/` |
| §7.4 | Dynamic query evaluation (Fig. 10) | `7.4_sensitivity/dynamic_query_evaluation/` |
| §7.4 | Skewed type frequencies (the queries of Fig. 10 at k = 4; the type rates are set in the data) | `7.4_sensitivity/skewed_type_frequencies/` |
| §7.4 | Query topology (six structures at k = 4) | `7.4_sensitivity/query_topology/` |
| §7.4 | Kleene patterns (Fig. 11a) | `7.4_sensitivity/kleene_patterns/` |
| §7.4 | Storage budget (Fig. 11b) | `7.4_sensitivity/storage_budget/` |
| §7.4 | Memory footprint (Table 4) | `7.4_sensitivity/memory_footprint/` |
| §7.4 | Distribution shift (Fig. 11c) | `7.4_sensitivity/distribution_shift/` |
| §7.5 | Real-world case studies, Q1–Q5 per dataset (Fig. 12) | `7.5_real_world/{chicago,ecommerce,taxi}/` |

## Cyclic queries

The pattern orders its variables, so consecutive variables are always related. A query is therefore cyclic if it has a
dependent condition, a time window included, between two variables that are not adjacent in the pattern: together
with the order constraints it closes a cycle. The table names these conditions per query; all other queries are
acyclic.

| directory | cyclic | closed by | acyclic |
|---|---|---|---|
| `7.2_state_of_the_art/cep_engine/` | `chain_k3_W1000` | A–C (window) | - |
| | `chain_k4_W1000` | A–D (window) |  |
| | `chain_k5_W1000` | A–E (window) |  |
| | `chain_k6_W1000` | A–F (window) |  |
| | `chain_k7_W1000` | A–G (window) |  |
| | `chain_k8_W1000` | A–H (window) |  |
| `7.2_state_of_the_art/feasibility_across_engines/` | `chain_k3_W1000` | A–C (window) | - |
| | `chain_k4_W1000` | A–D (window) |  |
| | `chain_k5_W1000` | A–E (window) |  |
| | `chain_k6_W1000` | A–F (window) |  |
| | `chain_k7_W1000` | A–G (window) |  |
| | `chain_k8_W1000` | A–H (window) |  |
| `7.2_state_of_the_art/mr_to_sql_transpilation/` | `chain_k3_W60` | A–C (window) | - |
| | `chain_k4_W60` | A–D (window) |  |
| | `chain_k5_W60` | A–E (window) |  |
| | `chain_k6_W60` | A–F (window) |  |
| | `chain_k7_W60` | A–G (window) |  |
| | `chain_k8_W60` | A–H (window) |  |
| `7.2_state_of_the_art/prefilter_acceleration/` | `chain_k4_W200`, `chain_k4_W500`, `chain_k4_W1000`, `chain_k4_W2000` | V0–V3 (window) | - |
| `7.3_plan_selection/cost_model_and_pick_quality/` | `complete_k4` | A–C (condition), A–D (condition), B–D (condition) | `chain_k3`, `chain_k4` |
| | `cycle_k4` | A–D (condition) |  |
| | `diamond_k4`, `star_k4` | A–C (condition), A–D (condition) |  |
| | `star_k3`, `triangle_k3`, `triangle_k4` | A–C (condition) |  |
| `7.3_plan_selection/selection_time/chain/` | - | - | `chain_k3`, `chain_k4`, `chain_k5`, `chain_k6`, `chain_k7`, `chain_k8`, `chain_k9`, `chain_k10`, `chain_k11`, `chain_k12`, `chain_k13` |
| `7.3_plan_selection/selection_time/complete/` | `complete_k3` | A–C (condition) | - |
| | `complete_k4` | A–C (condition), A–D (condition), B–D (condition) |  |
| | `complete_k5` | A–C (condition), A–D (condition), A–E (condition), B–D (condition), B–E (condition), C–E (condition) |  |
| | `complete_k6` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), B–D (condition), B–E (condition), B–F (condition), C–E (condition), C–F (condition), D–F (condition) |  |
| `7.3_plan_selection/selection_time/cycle/` | `cycle_k3` | A–C (condition) | - |
| | `cycle_k4` | A–D (condition) |  |
| | `cycle_k5` | A–E (condition) |  |
| | `cycle_k6` | A–F (condition) |  |
| | `cycle_k7` | A–G (condition) |  |
| | `cycle_k8` | A–H (condition) |  |
| | `cycle_k9` | A–I (condition) |  |
| | `cycle_k10` | A–J (condition) |  |
| | `cycle_k11` | A–K (condition) |  |
| | `cycle_k12` | A–L (condition) |  |
| | `cycle_k13` | A–M (condition) |  |
| `7.3_plan_selection/selection_time/star/` | `star_k3` | A–C (condition) | - |
| | `star_k4` | A–C (condition), A–D (condition) |  |
| | `star_k5` | A–C (condition), A–D (condition), A–E (condition) |  |
| | `star_k6` | A–C (condition), A–D (condition), A–E (condition), A–F (condition) |  |
| | `star_k7` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), A–G (condition) |  |
| | `star_k8` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), A–G (condition), A–H (condition) |  |
| | `star_k9` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), A–G (condition), A–H (condition), A–I (condition) |  |
| | `star_k10` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), A–G (condition), A–H (condition), A–I (condition), A–J (condition) |  |
| | `star_k11` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), A–G (condition), A–H (condition), A–I (condition), A–J (condition), A–K (condition) |  |
| | `star_k12` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), A–G (condition), A–H (condition), A–I (condition), A–J (condition), A–K (condition), A–L (condition) |  |
| | `star_k13` | A–C (condition), A–D (condition), A–E (condition), A–F (condition), A–G (condition), A–H (condition), A–I (condition), A–J (condition), A–K (condition), A–L (condition), A–M (condition) |  |
| `7.4_sensitivity/distribution_shift/` | - | - | `chain_k4` |
| `7.4_sensitivity/dynamic_query_evaluation/` | `cyclic_chain_k3` | A–C (window) | `acyclic_chain_k3`, `acyclic_chain_k4`, `acyclic_chain_k5`, `acyclic_chain_k6`, `acyclic_chain_k7`, `acyclic_chain_k8` |
| | `cyclic_chain_k4` | A–D (window) |  |
| | `cyclic_chain_k5` | A–E (window) |  |
| | `cyclic_chain_k6` | A–F (window) |  |
| | `cyclic_chain_k7` | A–G (window) |  |
| | `cyclic_chain_k8` | A–H (window) |  |
| `7.4_sensitivity/kleene_patterns/` | `kleene4_triangle_sigma0.01` | A–D (condition), B–D (condition) | - |
| `7.4_sensitivity/memory_footprint/` | - | - | `chain_k3`, `chain_k4`, `chain_k5`, `chain_k6` |
| `7.4_sensitivity/query_topology/` | `complete_k4` | A–C (condition), A–D (condition), B–D (condition) | `chain_k4` |
| | `cycle_k4` | A–D (condition) |  |
| | `diamond_k4`, `star_k4` | A–C (condition), A–D (condition) |  |
| | `triangle_k4` | A–C (condition) |  |
| `7.4_sensitivity/skewed_type_frequencies/` | `cyclic_chain_k4` | A–D (window) | `acyclic_chain_k4` |
| `7.4_sensitivity/storage_budget/` | `complete_k4` | A–C (condition), A–D (condition), B–D (condition) | `chain_k4`, `chain_k5`, `chain_k6` |
| | `cycle_k5` | A–E (condition) |  |
| | `diamond_k4`, `star_k4` | A–C (condition), A–D (condition) |  |
| | `star_k5` | A–C (condition), A–D (condition), A–E (condition) |  |
| | `triangle_k4` | A–C (condition) |  |
| `7.5_real_world/chicago/` | `Q2` | A–C (condition) | `Q1`, `Q5` |
| | `Q3` | A–D (window) |  |
| | `Q4` | A–E (window) |  |
| `7.5_real_world/ecommerce/` | `Q2`, `Q4` | A–C (condition) | `Q1` |
| | `Q3` | A–D (window) |  |
| | `Q5` | A–E (window) |  |
| `7.5_real_world/taxi/` | `Q4` | A–C (condition) | `Q1`, `Q2`, `Q3` |
| | `Q5` | A–C (window) |  |
