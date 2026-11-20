# Minimal synthetic interface example

**Example only.** This artificial two-condition computational dataset shows the interface and evidence chain. It contains no official case, paper, experimental object or gold standard and provides no quality/efficiency score evidence.

## Input

Place this JSON in an isolated demonstration directory as `case.json`; the adjacent `observations.csv` contains only the artificial values below. `task.md` is the sentence: “Compare the observed two conditions, identify a candidate deviation, and write an independently checkable follow-up.”

```json
{
  "case_id": "synthetic-interface-demo-1",
  "output_contract": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "experiment_design": {"type": "object", "properties": {"hypotheses": {"type": "array"}, "variables": {"type": "array"}, "controls": {"type": "array"}, "steps": {"type": "array"}, "parameters": {"type": "array"}, "resources": {"type": "array"}, "decision_rules": {"type": "array"}}},
      "trends": {"type": "array"},
      "anomalies": {"type": "array"},
      "possible_causes": {"type": "array"},
      "rechecks": {"type": "array"},
      "followup_experiments": {"type": "array"},
      "status": {"type": "string"},
      "sources": {"type": "array"},
      "uncertainty": {"type": "array"},
      "change_records": {"type": "array"}
    }
  }
}
```

```csv
unit_id,condition,run_index,signal_au
u1,A,1,10
u2,A,2,11
u3,A,3,9
u4,B,1,10
u5,B,2,12
u6,B,3,11
```

`signal_au` is explicitly an arbitrary unit. These six records are not claimed to be independent scientific replicates beyond the synthetic labels; there is no device log or claim of root cause.

## Invocation

```bash
python3 /skills/weekly-skill/scripts/pipeline.py bootstrap \
  --case /app/work/synthetic-demo/case.json \
  --task /app/work/synthetic-demo/task.md \
  --work /app/work/synthetic-demo/run \
  --output /app/work/synthetic-demo/output
```

Read `run/analysis_packet.json`. Create a small honest `run/reasoning.json` that maps all original fields through `fields` and fills the five explanation sections. Then run:

```bash
python3 /skills/weekly-skill/scripts/pipeline.py finalize \
  --case /app/work/synthetic-demo/case.json \
  --work /app/work/synthetic-demo/run \
  --output /app/work/synthetic-demo/output \
  --reasoning /app/work/synthetic-demo/run/reasoning.json
```

Expected runtime behavior: the first command creates an early partial result; the last prints `FINAL_OK` when the machine-level contract is valid, or `FINAL_PARTIAL` with items to fix. The real outputs are `output/result.json`, `output/audit.json`, `output/analysis_report.md`, `output/validation.json` and a real `commands.jsonl` recording the executed stages.

## Illustrative output shape only

The following is a deliberately partial example of structure, **not the precomputed result of this dataset**. In a genuine run descriptive counts and contrasts come from computed facts, a “candidate” or “cause_undetermined” category does not claim a device failure, and a future repeat is never marked completed.

```json
{
  "case_id": "synthetic-interface-demo-1",
  "status": "partial",
  "experiment_design": {"hypotheses": ["Computed contrasts are restricted to the two artificial condition labels."], "variables": [], "controls": [], "steps": [], "parameters": [], "resources": [], "decision_rules": []},
  "trends": ["Use the emitted count and median facts for groups A and B; no time trend is claimed."],
  "anomalies": ["No automatic quality-violation claim is made without a source-supported criterion."],
  "possible_causes": ["cause_undetermined: these synthetic labels alone provide no basis for a physical cause."],
  "rechecks": ["Re-read the supplied unit_id, condition and signal_au fields before interpreting groups."],
  "followup_experiments": ["Not applicable to the scientific world: this is only an interface demonstration."],
  "sources": ["Actual input path and SHA-256 from the input manifest."],
  "uncertainty": ["Example shape only; it is not a validated scientific conclusion."],
  "change_records": [{"stage": "template", "executed": false}]
}
```

The same procedure handles actual raw data by computing their content instead of inserting these words or numbers.

## Dependencies and actual local verification

Bootstrap, filtering and finalization use Python 3.9+ and the standard library. There is no network installation, author-private path or required shell profile. Readers are optional per installed module: `openpyxl` for XLSX, NumPy/SciPy for MAT and nonlinear fitting, PyMuPDF for PDF text, `h5py` for supported HDF5/MAT 7.3 datasets, Matplotlib for previews and scikit-learn only when a requested new estimator experiment is actually specified. A missing dependency produces an explicit runtime limitation and a valid partial where schema permits; it is not replaced with pretend parsing or fallback statistics.

The local real-data verification used Python 3.9.6, NumPy 2.0.2, SciPy 1.13.1, openpyxl 3.1.5, PyMuPDF 1.26.5, Matplotlib 3.9.4 and scikit-learn 1.6.1; these are compatibility references, not assertions of availability in another environment. HDF5 functionality was compile-checked without `h5py`, not execution-verified. Repeated numerical results carry the exact actual Python/package versions from each run. No `environment/resources.yaml` was supplied in the local development directory; consequently, platform availability beyond the verified local readers is not assumed.

PDF page text is not an automatically complete rendering or figure interpretation. Scans requiring OCR and paper-only curve images are material limitations unless the runtime actually supplies those capabilities. Formula cells whose cached results are absent remain unknown until a source-compatible calculation is performed; XLSX cache is not silently overwritten by a generic formula engine.
