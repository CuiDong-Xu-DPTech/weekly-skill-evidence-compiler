---
name: weekly-skill
description: >-
  Experimental result interpretation, raw-data anomaly analysis and experiment design. Use for research spreadsheets, multi-batch readings, force/bending curves, MAT/MATLAB files, scientific PDF plus workbook comparisons, or a requested train/test estimator experiment. Recompute trends and paired effects, locate candidate anomalies, rank falsifiable causes, produce executable rechecks and follow-up plans, and preserve case_id, original output_contract fields, sources, uncertainty, status and change records in /app/output/result.json.
---

# Evidence compiler for experimental result interpretation

Turn **the supplied original files** into traceable scientific findings. The program performs bounded, deterministic reading and calculation; the Agent supplies semantic mapping and cautious scientific reasoning. Never substitute fluent prose, general field knowledge, or a label inferred from a filename for actual data.

## Required invocation (do this first)

Run the following before any lengthy manual extraction, optionally substituting the loaded Skill's actual root if it differs from `/skills/weekly-skill`:

```bash
python3 /skills/weekly-skill/scripts/pipeline.py bootstrap \
  --case /input/case.json --task /input/task.md \
  --work /app/work --output /app/output
```

The stdlib entry point immediately creates a partial `result.json`, then inventories native inputs, computes facts, and writes a compact packet. A reader failure is not permission to invent results: inspect `work/errors/bootstrap.log` and `work/extracted.json`, repair only the failed stage if feasible, and rerun the same entry point. Preserve already verified results.

Read, in order:

1. `/app/work/BOOTSTRAP_NEXT.json` — the exact finalizer command.
2. `/input/case.json` and `/input/task.md` — original goals, constraints and the **complete output_contract**.
3. The `semantic_seed` location in `BOOTSTRAP_NEXT.json`, normally `/app/work/reasoning.seed.json`, **before** writing your own patch. This is a deterministic partial draft of actual results and unresolved evidence, not a finished scientific interpretation. Inspect its content with file-backed scripts or focused field queries rather than pasting the whole object into a model response.
4. `/app/work/analysis_packet.json` — computed facts, case-specific run results, source locations and coverage gaps.
5. `/skills/weekly-skill/references/reasoning-contract.md`, then only relevant parts of `references/methods.md`. If the source defines a specific model, replicate rule or nested contrast, use `source_declared_results` first and consult `references/declarative-analysis.md` for `expand`. If a needed declared operation is not present in the case, write its source-backed schema to `/app/work/analysis_rules.json` and run `expand` before interpreting the refreshed facts; never replace an unsupported model with a convenient one. Use `scripts/query_facts.py /app/work/analysis.json --metric ... --sheet ...` to select facts instead of repeatedly scanning huge tables.

For a targeted paper/method claim, read its cited **actual PDF-page text file**, not merely an extraction snippet. For important cell/range semantics, inspect its native workbook location and formulas. Any re-read must have a purpose; do not repeatedly extract the same file or use one model call per row/curve.

## Scientific reasoning and write-back

Use the real JSON location(s) in `BOOTSTRAP_NEXT.json`, normally `/app/work/reasoning.parts/` or `/app/work/reasoning.json`. Follow `references/reasoning-contract.md` for the file-backed refinement workflow and content. `/app/work/reasoning.example.json` shows only the required logical sections; the freshly generated `/app/work/reasoning.seed.json` supplies the actual partial evidence draft for this run.

Write five mutually consistent sections: **trends, anomalies, possible causes, rechecks, follow-up experiments**. Every numerical statement carries exact fact IDs and/or native locator(s). A negative finding must have explicit coverage and limits, not just `[]`.

Every conclusion must remain linked to its hypothesis and variables, applicable control, actual device/material/batch evidence, time/cost constraints, parameter range and risks. Missing aspects are explicit unknowns. Do not invent equipment calibrations, material batches, software state, independent repeats, randomization, costs, thresholds, causal explanations or additional observations.

Strictly distinguish:

- **observed**: a value in a supplied raw source;
- **source_reported**: a result stated by the supplied paper/log, not independently verified;
- **recomputed**: arithmetic on supplied values with the recorded formula;
- **reexecuted**: a new run performed by this Skill with saved indices, parameters and software versions;
- **hypothesis/proposed**: not an actual measurement or completed action.

Never silently reconcile paper, workbook and independently recomputed values. Show discrepancies and investigate range, units, rounding or identity before choosing an interpretation.

Use the `finalize_command` in `BOOTSTRAP_NEXT.json`. When a semantic seed exists it includes `--seed /app/work/reasoning.seed.json`; that option combines the computed seed with your compact review. Write only the required semantic fields, experiment design, cause comparison, rechecks, follow-up and original-field mappings: do **not** recreate fit tables, fold scores or hundreds of observations in model-authored prose/code. Use `source_artifacts` for real files generated under `work` and let the finalizer verify and copy them.

```bash
python3 /skills/weekly-skill/scripts/pipeline.py finalize \
  --case /input/case.json --work /app/work --output /app/output \
  --reasoning /app/work/reasoning.json --seed /app/work/reasoning.seed.json
```

The seed is always a **partial draft awaiting review**, not permission to promote it mechanically to complete. Preserve its source-supported numerical fields by default. If a computed result is wrong, revise its actual source mapping or declared rule and rerun `bootstrap`/`expand`; do not hand-patch better-looking numbers. A stale analysis digest, changed identity, unsupported root field or absent source artifact is rejected without replacing the previous valid result. Old `finalize` calls without `--seed` still accept a complete independent reasoning object when needed. Use the exact emitted command for the current case.

`FINAL_OK` means the JSON and dynamic fields passed mechanical checks; it does **not** prove scientific correctness. `FINAL_OK, unchanged=true` confirms an identical patch/analysis snapshot was already saved without duplicating changes. `FINAL_REJECTED_PREVIOUS_RESULT_PRESERVED` protects a valid earlier result: read `/app/work/rejected-final-validation.json`, fix the actual issue and rerun. If `FINAL_PARTIAL` remains, explain its limits honestly. Never end with answer text instead of a saved `/app/output/result.json`. Empty arrays in `sources`, `issues`, `confirmation_items`, `uncertainty` and `change_records` do not erase existing safety/evidence records. Read relevant large computations through the indexed shards under `/app/work/packets/`, never replacing all facts with the first few samples.

## Time and failure policy

Work toward **at most 14 model requests**, including reasoning/review responses; tool calls and model requests are different counts, and both must be tracked by the Harness. The 64-request allowance is a hard ceiling, not a target. Keep final facts in files rather than repeatedly sending long arrays to the model. By request 14, stop discretionary generation, save the best defensibly reviewed patch and use the exact finalizer. Never silently continue to 64 because another rewrite looks attractive.

- Immediately: run bootstrap, then read the seed, actual case and required contract.
- By 120 seconds: bootstrap and a valid partial result must exist.
- By 300 seconds: make one compact semantic review in a single file or nonoverlapping parts. Preserve computed numerical/group summaries instead of replacing them with unsupported prose.
- By 420 seconds: run one batched, genuinely necessary independent check and the exact finalizer. Do not serialize a custom script through a dozen small tool calls.
- By 540 seconds: finish contract and semantic review. Do not begin another analysis branch; retain the last valid partial rather than overrun.
- The shared `work/execution-budget.json` wall clock persists across pipeline processes. It is a cooperative backstop from the first pipeline observation, not a replacement for the Harness's full-session timer. An externally supplied `WEEKLY_SKILL_WALL_STARTED_EPOCH` may anchor it earlier. A late finalize preserves the previous valid output instead of overwriting it; it can still save a valid partial when no prior valid result exists.

If a format/library fails, continue readable inputs and write a `runtime_failure`. If material is absent, write `missing_material`; if evidence conflicts or supports multiple causes, write `cannot_determine`. These states are not interchangeable, and scientific uncertainty does not by itself make a completed run failed.

All wet-lab/device work is `proposed_not_executed`. State its hazards, responsible expert, required permissions, equipment readiness, resources and stop conditions; no experimental conduct is simulated. For purely computational follow-up, ethics may be `not_applicable` **with a task-specific reason**.

## Domain gates

### Curves, sequences, workbook and MATLAB

Keep raw acquisition order. Establish trace–sample mapping and array shape from real identifiers/scripts; do not match by similar names or attractive curve shapes. A 0-based Python index and its 1-based MATLAB location must be distinguished. Do not transpose, normalize, detrend, smooth, sort, remove outliers, impute saturation or align by DTW without a source-supported rule and an explicit sensitivity comparison.

An operational peak/transition is not automatically strength, yield, elasticity, fracture or a physical mechanism. Do not turn displacement into strain or curve area into fracture energy without geometry, calibration, method and source definitions. Functional depth flags whole-curve candidates; it does not establish causes.

### Small-sample estimator comparisons

Honor the estimator/split definitions actually requested and record the actual source/library/version. Distinguish five folds of ordinary CV, five repeated holdouts, and Dietterich 5×2cv. Two estimators must use identical sample IDs per split. Training preprocessing stays inside the fit pipeline. Record complete estimator parameters, splitter seeds, estimator seeds, train/test indices and actual warning/run states.

Report all split–estimator results, correct counts, per-class errors when available, paired differences in percentage points, wins/ties/losses and split sensitivity. CV training overlap defeats naïve independent-sample intervals/significance claims. Do not choose the best seed or overwrite reported results with a newly executed variant.

### Scientific PDF + multi-sheet workbook

Enumerate every sheet, formula/cache state, unit and repeated-measurement definition; map it to the actual paper's objective/method/table/page. Do not assume a sheet meaning from its tab name or arbitrarily join distinct experiments.

For a declared single-positive-parameter binding model, preserve the local `dof = n_included_rows - 1`; `T0=0` anchors and fixed L0 do not reduce the parameter count or justify `n-2`. For this unbound fraction, `R` and residuals/residual-domain deviations are unit 1, not concentration units. Retain an explicit max(0,1-T0/L0) feasibility check with cell-by-cell deficit and inclusion evidence. Treat below-bound observations as conditional model/data incompatibilities and undetermined cause, not automatic protocol QC or invented physical faults; no percent-area statistic or ligand-specific constraint is generalized to other response types. Preserve each group's `response_domain` and `per_group_declared_model_response_domain` counts, units and fact IDs rather than replacing them with an unchecked aggregate: one block's one-violation count is not another block's two; negative observations are a subset of that same group's below-bound set, not extra violations.
Use the paper's regression axes, range, weighting, precision and accuracy definitions. Do not apply universal `RSD≤2%`, `r≥0.999`, `98–102% recovery`, LOD/LOQ, pharmacopoeial or bioequivalence thresholds. `alternative method` needs identity and intended use; agreement/correlation is not equivalence, and assay data cannot establish a wet chemical or clinical mechanism.

## Coverage and budget-critical patterns

Interpret the actual task and materials; the following are coverage checks, not precomputed answers.

- For numerous raw force/bending traces, compare CSV observations, MAT variables/indices and workbook sample identities explicitly. A source-declared script mapping or `source_curve_mappings` entry is required to join them; identical curve shapes or similar filenames are not identity evidence. Report raw/routed/mapped/eligible counts and every unmatched source identity. A first-column displacement can be abscissa, not a second response curve. Insufficient genuine mapping is an unresolved scientific limit, not permission to merge groups.
- For a newly requested Iris or estimator experiment, recover protocol from both original `task.md` and `case.json`, including constructor strings. Honor the requested split, seeds, model parameters and order. Do not silently skip execution because an estimator is supplied as a string or defaults were requested. Save all split×estimator indices, predictions, parameters, warnings and versions in `artifacts/computational_results.json`, with pooled paired contrasts where defined. Never submit the ten fold scores as independent laboratory repeats.
- For paper-guided multi-sheet analytical work, establish actual title/header rows and method/alternative identities from cells and paper context. Inventory every original sheet with its semantic role, native range, paper page, comparison/reference, observed/recomputed/source-reported/unresolved state and reason. Use source-specific axes, weighting, precision, accuracy and replicate definitions. Do not invent a universal OLS or validation threshold for a 12-sheet workbook merely to make every sheet look computed.
- Preserve complete computed evidence through the supplied unabridged shards or `query_facts.py` pagination. Count returned records against eligibility. Batch all trace raw-feature inspection rather than using one model request per row. Where per-item completeness is required, cite complete generated CSV/JSON artifacts; do not aggregate away unexamined curves, sheets or splits.

Keep model-authored JSON concise and case-specific. Do not copy the generic instructions or expand entire fact arrays into `reasoning.json`/`fields`. One model response should stay comfortably below 12,000 output tokens including its reasoning; split semantic patches under 8,000 tokens and use real generated artifacts for bulky per-item results. The formal allowance is 16,384 tokens per response, 64 model requests and 900 seconds total, not a target. Keep the last verified finalizer result and never trade a defensible partial for a truncated manual patch.

## Output and minimal example

`/app/output/result.json` always contains the exact input `case_id` and every original output-contract field. Extra audit details are saved in `audit.json` when the original contract forbids extra top-level fields. Real generated plots/previews are copied into `/app/output/artifacts`; do not create editable Office or slide artifacts unless actually requested. Such artifacts, when made, must have real rendered PDF/PNG previews.

The Skill resource itself contains only text. Its entry point resolves scripts relative to `__file__`, performs no network installation, and requires no author-private files. Python is required for bootstrap/finalize; scientific readers are selected only after their installed imports succeed, with finite-JSON partial failure behavior.

See `references/minimal-example.md` for a small **synthetic interface-only example**. It is not one of the official cases, a gold standard or a benchmark result.

Before finishing, confirm that sources, uncertainty, run status and change records are truthful, all original fields are present, and each follow-up proposal says which competing explanation it discriminates and what result would weaken the current conclusion. Any unused extra time should go to checking the source evidence, not inventing benchmark scores or quality/efficiency gains.
