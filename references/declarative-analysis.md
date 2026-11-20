# Source-declared analyses

Use this layer only when the supplied source/task explicitly defines an experiment model or comparison. `analysis_rules.json` is a reproducible, agent-extracted work configuration; it is **not** a hidden answer table or a license to choose the best-looking fit. Do not modify `/input/case.json`.

Before expanding, inspect original PDF-method pages/workbook column paths and initial `source_declared_results` in `analysis_packet.json`. When the original case already supplies `analysis_rules_v1`, bootstrap runs those declared analyses automatically. Otherwise write a small `/app/work/analysis_rules.json` only for operations actually supported by the source, then run:

```bash
python3 /skills/weekly-skill/scripts/pipeline.py expand \
  --case /input/case.json --work /app/work --output /app/output \
  --rules /app/work/analysis_rules.json
```

The command reuses `work/extracted.json`; it does not repopulate the input or infer an outcome from a filename. It removes only previously declared computation results, recalculates from current actual data and attaches new fact IDs. Its declared configuration, duration, metrics, coverage and failures are recorded. Do not treat a model-extracted design as prior expert approval; cite the original source and distinguish proposal from original protocol.

## Supported operation contracts

Each rule has an `operation`, enough explicit column definitions, a source `basis` (actual page/method/source anchor), and necessary `sheet_selector`/header criteria. Unknown operations, mismatched identities, ambiguous values or unsupported models produce `cannot_determine`/partial rather than a guess.

### `fit_declared_model`

For source-defined models. Currently supported model ID: `binding_unbound_fraction_one_to_one_eq11`.

Required: one-to-one unbound-ligand binding fraction `R` versus target total `T0`, blockwise fixed `L0`, corresponding actual headers/uncertainties and sheet choice. The allowed declared function is `R = -(Kd+T0-L0)/(2L0) + sqrt(((Kd+T0-L0)/(2L0))^2 + Kd/L0)`. It is used only after the source explicitly calls for this equation, not because another curve resembles it.

The ordinary unweighted single-positive-parameter nonlinear fit gives Kd, residual-by-source row and a local-covariance `Kd±one-SE` interval when identifiable. This 68.3%-style parameter interval is not a bootstrap interval or accuracy-confidence interval. Output separates source `error in R` (not automatically a weight or residual), source error fractions, this regression, and unavailable systematic-error/ACI calculations. At a bound or a singular covariance/small residual DoF, uncertainty remains unresolved.

The fit has one free positive parameter. Retain `free_parameter_count=1` and local residual `dof = n_included_rows - 1` explicitly; fixed L0 and a fit-invariant `T0=0` anchor do not reduce the parameter count or justify `n-2`. Correct covariance arithmetic does not prove independent residuals or accuracy, and another profile/bootstrap must not silently replace the declared confidence form.

For this explicit unbound-fraction model, `R` and its residuals are **dimensionless (unit 1)**, never µM or nM; only L0, T0 and Kd use their source concentration units. Independently check `max(0,1-T0/L0)` against each observed R. A smaller observation is a **conditional algebraic incompatibility with the declared model and reported L0**, not a source-protocol QC breach, a p-value, proven instrument fault, or a reason to delete/clip a row. Record each row's bound, deficit, R_ref and retained status, along with above-one and below-zero observations. Unlike a material-spike ratio of textual percentages, this constraint arises from the declared binding identity itself. Retain each group's below-bound/above-one/below-zero counts separately: negative observations are already included among its below-bound observations, never an additional category to add. The seed preserves these same computed numbers in per-group `response_domain` records and a `per_group_declared_model_response_domain` control; any replacement narrative must remain consistent with them. Do not merge two isotherms into one count or claim each block has the other block's count. Do not apply that ligand-specific bound to an arbitrary normalized force or generic intensity.

### `paired_replicate_range`

A **source-declared** technical replicate grouping and threshold:

```json
{
  "operation": "paired_replicate_range",
  "sheet_selector": "actual sheet name",
  "group_by": ["unit", "measurement_target", "channel"],
  "value_column": "actual numeric reading",
  "replicate_column": "repeat_number",
  "pair_values": [1, 2],
  "metric": "absolute_difference",
  "threshold": 0.05,
  "rule": "strictly_greater_than_threshold_flags_source_QC_candidate",
  "basis": "actual original protocol page/section defining this threshold and its units"
}
```

Here `0.05` illustrates schema syntax in an arbitrary-unit synthetic schema only; it is not a laboratory cutoff. No threshold should be copied without a source. A complete pair uses actual replicate IDs; the emitted difference is second minus first in acquisition order. Missing pairs, extra repeats, missing values and duplicate identities are separate unresolved states, not reasons to average away the record or infer biological abnormality. Raw records stay visible even when the source requires exclusion.

### `derived_ratio_audit`

Supplies numerator, denominator and (optionally) a reported ratio column and real range. Each ratio is recomputed from raw integer counts where possible. A zero denominator yields undefined/missing, **not zero**; a zero numerator with a nonzero denominator is a valid zero. Source rounding and display differences are retained separately from exact arithmetic escapes such as floating roundoff. Source `rounding_digits`/tolerance is needed to claim declared rounding agreement rather than merely numeric reconciliation.

### `group_summary_nested_treatment_contrast`

For explicit hierarchy in the source: read row identity, independent treatment-unit columns, pairing/block identity, condition level and each metric's role. Available numeric roles are `count`, `numeric_continuous`, and `ratio_from_counts`. With `left_condition` and `right_condition`, the exact source-prescribed order of calculation is:

1. Inner arithmetic means of available matched observations per independent unit. Record present and missing counts.
2. For a ratio metric with numerator and denominator, compute the ratio from those raw aggregates, not the mean of a rounded display column.
3. Pair units at the stated family/block/experiment identity only if both conditions exist and are unique; compute `left - right` in unit terms for every available metric. Preserve both unit IDs, their inner observations and mean/SD-free details.
4. Summarize mean, median and range at the outer family level with count coverage. Leave an entire lineage group (e.g., sire/dam) out only as an explicitly requested sensitivity analysis; do not call it a fresh biological replicate or significant test.

No bootstrap of technical wells or nested vials, no default independence, no minimum group sample size of three magic, and no missing-to-zero fill. If ids are nonunique or necessary fields are missing, the valid results are coverage and explicit unresolved state, not forced splicing.

## Fact selection and inspection

Read `source_declared_results` and `fact_examples` first. To access the full computed records corresponding to a particular group/metric rather than loading the entire analysis:

```bash
python3 /skills/weekly-skill/scripts/query_facts.py /app/work/analysis.json \
  --metric KEYWORD --sheet ACTUAL_SHEET --limit 20
```

Use `--kind derived` for local recomputation, `--kind source_reported` for paper/source claims, and exact IDs via `--ids F...` after reading a fact example. Filtering is read-only and always reports `candidate_facts`, `returned_facts` and `truncated`. Validate scientific coverage against the applicable unfiltered `analysis.json`/source shards; a query page is for inspection, not for silently reducing the analysis scope.

## Pending interpretation

After `EXPANDED_SOURCE_RULES`, read the refreshed packet and any validation issues. The schema and arguments came from actual source evidence, which does not establish that the model is appropriate, that rows are scientifically independent, or that all measurements are valid. Normal completion still requires credible source/version history, exact unit and locator checks, contradictory/uncertain explanation ranking and discriminating proposed rechecks. Never invent scores from your own QC counts or from a successful CLI exit code.
