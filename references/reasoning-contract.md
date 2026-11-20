# Reasoning patch and result contract

The Agent writes one JSON object or a directory of ordered, nonoverlapping JSON parts to the exact path in `BOOTSTRAP_NEXT.json`. `reasoning.example.json` is a template with instructions to replace, not findings to copy. `reasoning.seed.json` is separately generated from the current analysis and is always a **partial** draft, not precomputed case answers: review actual source meaning and required fields before selecting a final status.

Use deterministic, file-backed refinement. Instead of manually retyping the entire seed, write only a compact semantic review and pass it with `--seed /app/work/reasoning.seed.json` to the finalizer. That mode loads the real numbers and source IDs for you. Override only reviewed semantic sections or original-field mappings; do not manufacture a new program merely to rewrite every numeric table again. Generated reviews must be actual saved JSON; do not claim a computation happened because a script was described in chat. Large per-object outputs belong in the full result artifacts, with concise findings linking to them.

The merge enforces the exact original `case_id`, a partial generated seed and the matching analysis digest. Semantic sections are replaced when explicitly reviewed; source/evidence uncertainty and confirmed change records are retained by default. A changed analysis requires refreshed bootstrap/expand rather than mixing an old seed with new calculations. `source_artifacts` lists **actual ordinary files generated under work**; the finalizer rejects outsiders or absent files, verifies SHA-256, and copies them with their relative structure. Findings need actual available-source relationships, not decorative filenames. An explicit complete status is a scientific judgment by the reviewing Agent after semantic/source checks, not an automatic consequence of a valid seed or accepted JSON shape.

If using `reasoning.parts/`, split by nonoverlapping top-level sections. `001-fields.json` handles exact identity/status and original fields, `002-evidence.json` handles trends/anomalies/causes, and `003-actions.json` handles rechecks/followups/source/risk items. Merge them in that order. Never append two different versions of the same finding ID. To correct one, edit the existing part, then re-run the whole finalizer on the resulting object. Do not let a corrected number coexist with its stale duplicate. Each model response, including reasoning, remains below the actual budget; use files rather than a single enormous generated patch.

## Accepted shape

```json
{
  "case_id": "exact original value and type",
  "status": "complete | partial | failed",
  "fields": {
    "original_top_level_or_nested_field": "JSON value of its declared type",
    "/nested/original/pointer": "value"
  },
  "trends": [],
  "anomalies": [],
  "possible_causes": [],
  "rechecks": [],
  "followup_experiments": [],
  "experiment_design": {},
  "basis_and_risk": {},
  "confirmation_items": [],
  "uncertainty": [],
  "issues": [],
  "change_records": [],
  "result_text": "Optional concise narrative"
}
```

`fields` has priority for original output fields; use the exact original key or pointer. With `--seed`, either a normal mapping (`"experiment_design.resources": {...}`) or one assignment-shaped value (`{"path": "experiment_design.resources", "value": {...}}`) is accepted and normalized; do not wrap `fields` itself in another `fields`, or copy an assignment into `experiment_design`. If a case specifies `additionalProperties: false`, never introduce extra top-level result keys. The finalizer writes full audit details separately to `audit.json`. If no output contract or a permissive object is given, `skill_audit` can be embedded.

Preserve deterministic per-group structures such as `response_domain` and the `per_group_declared_model_response_domain` control unless a source-derived correction is needed. A below-zero observation is a subset of the below-bound observations in that group, not an additional independent violation: do not combine groups, sum categories, round negatives into nonnegative values, or replace the numeric record with an inconsistent narrative. Any change must keep the fact IDs, group labels, exact units and inclusion state consistent.

The original contract may instead require strings, arrays, objects or nested variants. Project the same evidence into that shape, without forcing the generic template onto the evaluator. `result.json`, not a Markdown code block, is the machine-readable deliverable.

## Finding schemas

Each item can be a concise narrative string when the original contract demands it, but structured objects are preferable for audit:

```json
{
  "id": "R1",
  "statement": "Direct scientific interpretation with its scope and source status",
  "entity": {"sample": "actual ID", "batch": "actual ID or null", "interval": "actual interval"},
  "evidence": ["factual IDs emitted by analyze, or paper.pdf#page=4"],
  "numerical_result_id": "the exact fact used",
  "method": "the emitted deterministic method",
  "control_id": "actual control or missing",
  "parameter_range": "observed range, not an invented permissible range",
  "uncertainty": "explicit residual uncertainty"
}
```

For an anomaly also include `observed`, `reference`, `deviation`, `unit`, `scale`, `threshold_basis`, and `inclusion: retained`. Retain censoring and unit flags. Candidate states are `quality_violation`, `outlier_supported`, `candidate_for_review`, `cause_undetermined`; do not call exploratory screening a confirmed causal error.

For each cause include:

- `candidate`, ordered by local evidence rather than narrative plausibility;
- `supporting_fact_ids`, `opposing_facts`, `unresolved_alternatives`;
- `association_or_causation` and `unmeasured_confounders`;
- `necessary_prediction` and a `discriminating_action` with distinct outcomes that would weaken it.

Rival mechanisms must not share a single unexamined explanation. If no mechanism is supported, an explicit `cause_undetermined` item with falsifiable candidates is required; that is not an empty anomalies section.

For each recheck include an actor, exact file/sheet/cell/row/MAT variable and one-based MATLAB index/PDF page/fold/sample ID, ordered operations, expected observation, readable acceptance range, status and affected finding. For a proposed new experiment include independent units, control, parameters/ranges, total number of fits or material/time model, decision rules, and required safety/equipment/expert gates. Never present a proposal as a completed experiment.

## Field resolution rules

The finalizer preserves the original contract exactly, including nested paths, arrays and types. `case_id` is overwritten with the exact original value **at the result root even when `additionalProperties` is false**; a patch that tries to change it is rejected. Do not place it only inside `experiment_design` or prose. Additional files (`audit.json`, `analysis_report.md`, `validation.json`, `commands.jsonl`) carry full details. Do not use NaN or Infinity.

`experiment_design` must record the actual task's hypotheses, variables, controls, steps, parameters, resource/time constraints and decision rules with evidence. `basis_and_risk` must tie actual sources to scientific assumptions, conflicts and limitations rather than repeat generic method prose. A proposed future action names the actor, resource/time basis, independent units, discriminating criterion and relevant safety/equipment/expert gate. Unknown inputs stay unknown; do not invent concrete costs or adverse-event rates. `confirmation_items` needs case-specific unresolved safety/ethics/device/expert decisions, not a generic list alone.

Store repeated numeric results in the complete computational/curve artifacts and cite their exact paths plus fact IDs. Do not reproduce every fact row in a single model-authored JSON response or copy the instruction template as findings. If the contract has only loose object/array types, use concise complete summaries with machine-readable bulk evidence retained in the output artifacts. If it explicitly requires per-object fields, enumerate all required objects concisely instead of truncating them to save tokens.

Use `complete` only after all original fields and five sections are defensibly populated. Run success and scientific certainty are orthogonal: `complete` can retain an explicitly unresolved cause; `partial` must identify what computation or contract requirement remains. `failed` is reserved for prevented essential execution, not an inconvenient result.

Validation checks case identity, complete original fields, strict finite JSON, five-section presence, and recognizable evidence IDs. A warning about an unknown `F…` ID means check the actual fact rather than renumbering it. A mechanical pass does not convert a citation into scientific proof; source-reported and derived evidence stay distinguishable.
