# Queries of the evaluation

The queries used in the paper's evaluation, one directory per experiment. For every query:

- `<name>.sql`: the `MATCH_RECOGNIZE` statement exactly as EIMER's spec renderer produces it (SQL:2016 form).
- `<name>.json`: the query specification EIMER compiles (variables, pattern with gap quantifiers, independent
  and dependent conditions, measures, result keys). Load it with `eimer.query.query_spec.load_query_spec_file`.
- `<name>_graph.pdf` / `.png`: the query graph. Nodes are pattern variables with their independent condition and
  Kleene quantifier; solid arrows are the sequence constraints of the pattern, labelled with the gap quantifier
  (`Z*` greedy, `Z*?` reluctant); dashed edges are the dependent conditions (blue band, orange window, green
  equality, pink mixed).

Each directory's `README.md` lists the pattern, every condition in full, and the parameters the query ran with.

| section | experiment | directory |
|---|---|---|
| §7.2 | Feasibility across engines (fig:cross_engine) | `7.2_state_of_the_art/feasibility_across_engines/` |
| §7.2 | Prefilter acceleration (fig:prefilter) | `7.2_state_of_the_art/prefilter_acceleration/` |
| §7.5 | Real-world case studies, Q1–Q5 per dataset (fig:realworld) | `7.5_real_world/{chicago,ecommerce,taxi}/` |
