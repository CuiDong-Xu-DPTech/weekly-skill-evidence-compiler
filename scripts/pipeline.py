#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from collections.abc import Mapping
import copy
import datetime as dt
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import time
import traceback

from contract import (
    assemble,
    canonical_patch,
    dump_json,
    empty_audit,
    initial_result,
    load_json,
    merge,
    merge_audit,
    render_markdown,
    validate_result,
)


METHOD_REFERENCES = [
    {"claim": "Technical repeats are not independent experimental units.", "source": "Vaux et al., 2012", "url": "https://doi.org/10.1038/embor.2012.36"},
    {"claim": "Robust scores and masking limits for exploratory outlier detection.", "source": "Rousseeuw & Hubert, 2011", "url": "https://wis.kuleuven.be/stat/robust/papers/2011/rousseeuwhubert-robuststatisticsforoutlierdetectio.pdf"},
    {"claim": "Modified band depth and exploratory functional boxplot.", "source": "Lopez-Pintado & Romo, 2009; Sun & Genton, 2011", "url": "https://doi.org/10.1198/jasa.2009.0108"},
    {"claim": "Cross-validation fold scores are not independent replicates.", "source": "Bengio & Grandvalet, 2004", "url": "https://www.jmlr.org/papers/v5/grandvalet04a.html"},
    {"claim": "Competing hypotheses need discriminating observations.", "source": "Platt, 1964", "url": "https://doi.org/10.1126/science.146.3642.347"},
    {"claim": "Method validation criteria must match intended use and source definitions.", "source": "ICH Q2(R2), corrected 2025", "url": "https://database.ich.org/sites/default/files/ICH_Q2%28R2%29_Guideline_2023_1130_ErrorCorrection_2025.pdf"},
]


def timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def compact(value, depth=0):
    if depth > 10:
        return "<max-depth>"
    if isinstance(value, dict):
        return {str(key): compact(val, depth + 1) for key, val in value.items()}
    if isinstance(value, list):
        if len(value) > 40:
            return [compact(item, depth + 1) for item in value[:40]] + [{"omitted_items": len(value) - 40, "available_in": "work/extracted.json"}]
        return [compact(item, depth + 1) for item in value]
    if isinstance(value, str) and len(value) > 6000:
        return value[:6000] + "\n<truncated; read original source>"
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def environment_record() -> dict:
    packages = {}
    for package in ("numpy", "scipy", "pandas", "openpyxl", "pymupdf", "h5py", "matplotlib", "scikit-learn"):
        try:
            packages[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            packages[package] = None
    root = Path(__file__).resolve().parent.parent
    code = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or "__pycache__" in path.parts or path.suffix.lower() not in (".py", ".md", ".txt", ".json"):
            continue
        code.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"python": sys.version, "platform": platform.platform(), "packages": packages,
            "skill_files": code, "skill_bytes": sum(item["bytes"] for item in code), "skill_file_count": len(code)}


def append_log(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, allow_nan=False)
        handle.write("\n")


def run_stage(command: str, work: Path, output: Path, case_id, started, fn, *args):
    stage_start = time.monotonic()
    entry = {"seq": int(time.time() * 1000), "command": command, "case_id": case_id,
             "cwd": str(work), "started_at": timestamp(), "argv": sys.argv,
             "status": "failed"}
    log_path = output / "commands.jsonl"
    previous_commands = 0
    if log_path.is_file():
        with log_path.open("r", encoding="utf-8") as handle:
            previous_commands = sum(1 for line in handle if line.strip())
    entry["pipeline_command_invocation"] = previous_commands + 1
    try:
        value = fn(*args)
        entry["status"] = "ok"
        entry["return_code"] = value if isinstance(value, int) else None
        return value
    except Exception as exc:
        entry["error"] = type(exc).__name__ + ": " + str(exc)
        entry["traceback_file"] = str(work / "errors" / (command + ".log"))
        entry["traceback_file"] = str(Path(entry["traceback_file"]).name)
        (work / "errors").mkdir(parents=True, exist_ok=True)
        (work / "errors" / (command + ".log")).write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        entry["duration_seconds"] = round(time.monotonic() - stage_start, 3)
        entry["native_wall_elapsed_seconds"] = round(max(0.0, time.monotonic() - started), 3)
        entry["native_wall_budget_seconds"] = getattr(args[0], "wall_budget_seconds", _NATIVE_WALL_LIMIT_SECONDS) if args else _NATIVE_WALL_LIMIT_SECONDS
        if args:
            args[0]._native_work = work
            snapshot = _budget_snapshot(args[0])
            entry["native_wall_elapsed_seconds"] = snapshot["elapsed_seconds"]
            entry["native_wall_clock_origin"] = snapshot["clock_origin"]
            entry["native_wall_remaining_seconds"] = snapshot["remaining_seconds"]
            entry["local_stage_completed_after_wall_budget"] = snapshot["exhausted"]
        append_log(log_path, entry)


def case_load(path: Path) -> dict:
    case = load_json(path)
    if not isinstance(case, dict):
        raise ValueError("case.json root must be an object")
    return case


_MODEL_RESPONSE_TOKEN_LIMIT = 12000
_NATIVE_REQUEST_LIMIT = 64
_NATIVE_WALL_LIMIT_SECONDS = 900
_PACKET_PAGE_SIZE = 40


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_file(path: Path, identifier: str, kind: str, parser: str) -> dict:
    resolved = path.resolve()
    return {"id": identifier, "path": str(resolved), "name": resolved.name, "kind": kind,
            "sha256": _sha256(resolved), "size": resolved.stat().st_size, "parser": parser}


def _deduplicate_sources(*groups) -> list:
    combined = []
    positions = {}
    for group in groups:
        for source in group or []:
            if not isinstance(source, Mapping) or not source.get("path"):
                continue
            item = copy.deepcopy(dict(source))
            identity = (str(item.get("path")), str(item.get("sha256") or ""))
            if identity in positions:
                existing = combined[positions[identity]]
                for key, value in item.items():
                    if existing.get(key) in (None, "") and value not in (None, ""):
                        existing[key] = value
                continue
            positions[identity] = len(combined)
            combined.append(item)
    return combined


def _invoke_analysis(function, data: dict, case: dict, work: Path, task_text: str):
    try:
        signature = inspect.signature(function)
        parameters = signature.parameters
        accepts_task = "task_text" in parameters and parameters["task_text"].kind != inspect.Parameter.POSITIONAL_ONLY
        accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    except (TypeError, ValueError):
        signature, accepts_task, accepts_kwargs = None, False, False
    forwarded = accepts_task or accepts_kwargs
    if forwarded:
        result = function(data, case, work, task_text=task_text)
        mode = "task_text_keyword"
    else:
        result = function(data, case, work)
        mode = "legacy_three_argument_fallback"
    interface = {
        "callable_signature": str(signature) if signature is not None else "unavailable",
        "task_text_forwarded": forwarded,
        "mode": mode,
        "task_text_sha256": hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
        "task_text_characters": len(task_text),
        "limitation": None if forwarded else "Installed analysis accepts only analyze(data, case, work); the original task.md content is not forwarded into that legacy call and is not claimed to participate in calculations.",
    }
    return result, interface


def _model_execution_policy() -> dict:
    return {
        "model_request_limit": _NATIVE_REQUEST_LIMIT,
        "response_output_tokens_including_reasoning_must_be_strictly_less_than": _MODEL_RESPONSE_TOKEN_LIMIT,
        "recommended_planning_ceiling": 11000,
        "recommended_total_model_requests_before_finalizing": 14,
        "native_request_measurement_required": "Track actual native model requests independently of tool invocations. Stop discretionary work by 14 requests; do not infer compliance from a successful process exit.",
        "measured_model_usage": {"requests": None, "output_tokens_including_reasoning": None,
                                 "source": "unavailable_inside_pipeline; read actual provider/harness usage, never estimate from characters"},
        "provider_enforcement_available_here": False,
        "response_partitioning": "Write one reasoning.parts/*.json patch per response. Keep each response, including reasoning/content fields, strictly below 12000 actual provider-reported tokens. Do not combine or print all patches in one oversized model response.",
        "tool_accounting": "commands.jsonl contains one real record per pipeline command invocation with measured monotonic durations. Model request/token counts remain null unless measured by the provider/harness.",
    }


def _shared_wall_start(work: Path) -> tuple:
    external = os.environ.get("WEEKLY_SKILL_WALL_STARTED_EPOCH")
    if external is not None:
        origin = float(external)
        source = "external_epoch_environment"
    else:
        state_path = work / "execution-budget.json"
        if state_path.is_file():
            state = load_json(state_path)
            origin = float(state["wall_start_epoch"])
            source = "first_pipeline_wall_clock_checkpoint"
        else:
            origin = time.time() - max(0.0, time.monotonic() - getattr(_shared_wall_start, "monotonic_start", time.monotonic()))
            source = "first_pipeline_wall_clock_checkpoint"
            dump_json(state_path, {"wall_start_epoch": origin, "wall_budget_seconds": _NATIVE_WALL_LIMIT_SECONDS})
    if not math.isfinite(origin) or origin <= 0 or origin > time.time() + 1:
        raise ValueError("Shared wall-clock origin must be a finite past Unix epoch")
    return origin, source


def _budget_snapshot(args) -> dict:
    work = getattr(args, "_native_work", None)
    if work is not None:
        origin, source = _shared_wall_start(Path(work))
        elapsed = max(0.0, time.time() - origin)
    else:
        started = getattr(args, "_native_monotonic_start", time.monotonic())
        source = "current_process_monotonic_only"
        elapsed = max(0.0, time.monotonic() - started)
    limit = float(getattr(args, "wall_budget_seconds", _NATIVE_WALL_LIMIT_SECONDS))
    return {"budget_seconds": limit, "elapsed_seconds": round(elapsed, 6),
            "remaining_seconds": round(limit - elapsed, 6), "exhausted": elapsed > limit,
            "clock_origin": source,
            "measurement": "Shared wall-clock checkpoints at cooperative stage boundaries; model requests and time before the first pipeline invocation require harness measurement. Synchronous library calls are not claimed to be interruptible."}


def _cooperative_budget_guard(args, work: Path, audit: dict, checkpoint, phase: str) -> bool:
    snapshot = _budget_snapshot(args)
    audit.setdefault("run", {})["native_wall_budget"] = snapshot
    if not snapshot["exhausted"]:
        return True
    audit["run"].update({"status": "partial", "complete": False, "local_timeout": True, "stage": phase + "_timeout"})
    issue = {"kind": "stage_timeout", "stage": phase,
             "detail": "The measured native wall budget was exhausted at a cooperative stage boundary. No later stage was run; already saved results remain partial.", "budget": snapshot}
    audit.setdefault("issues", []).append(issue)
    audit.setdefault("change_records", []).append({"stage": phase, "status": "partial_timeout", "budget": snapshot})
    checkpoint(audit)
    state = {"state": "STAGE_TIMEOUT_PARTIAL", "phase": phase, "budget": snapshot,
             "work": str(work), "reason": issue["detail"]}
    dump_json(work / "STAGE_PARTIAL.json", state)
    print(json.dumps(state, ensure_ascii=False))
    return False


def scientific_audit(case: dict, data: dict, analysis: dict, environment: dict) -> dict:
    audit = empty_audit(case)
    audit["environment"] = environment
    audit["run"].update({"stage": "facts_ready", "status": "partial"})
    audit["experiment_design"] = {
        "objective": [{"statement": "From the supplied original records, explain all relevant observed contrasts, quantify candidate departures, and distinguish evidence from hypotheses.",
                       "evidence": ["case.json:objective/task requirements and actual attached source files"]}],
        "hypotheses": [{"id": "H1", "statement": "Quantitative contrasts are valid only within compatible sample, method, unit and acquisition contexts.",
                        "falsifier": "A demonstrated ID/unit/mapping incompatibility or reversed effect under a source-supported alternative grouping.",
                        "status": "analysis_constraint"}],
        "variables": [{"role": "observed_or_derived", "definition": "Use the actual task and source field names; do not infer a manipulated treatment from filenames.",
                       "required": ["name", "unit", "range", "independent unit", "measured versus assigned"]}],
        "experimental_units": [{"definition": "Independent specimen, preparation, run or study unit as supported by the original design.",
                                "prohibition": "Curve points, repeated scans, wells and cross-validation folds do not automatically increase independent n."}],
        "controls": [{"status": "identify_from_actual_materials", "kinds": ["blank/negative", "reference", "paired method", "computational baseline"],
                      "rule": "Future controls are proposals, never observations of the original experiment."}],
        "steps": [{"step": 1, "action": "Inventory and hash all supplied files; isolate the original case contract and task."},
                  {"step": 2, "action": "Read native workbook formulas/caches, CSV records, MAT arrays/scripts and actual PDF pages."},
                  {"step": 3, "action": "Calculate per-object descriptive statistics, paired effects and conditional robust anomaly candidates; retain all rows in primary analysis."},
                  {"step": 4, "action": "Review source-supported trends, conflicting evidence and alternative mechanisms before proposing discriminating actions."},
                  {"step": 5, "action": "Validate original output fields, JSON values, evidence references, unresolved limits and change history."}],
        "parameters": analysis.get("methods", []),
        "resources": [{"input_sources": len(data.get("inputs", [])), "analysis_environment": environment,
                       "device_or_material_batches": "Extract only from supplied raw records; unknown values remain unknown."}],
        "time_cost_constraints": [{"calculation": "One bounded batch analysis; intermediate checkpoints before model interpretation.",
                                    "measured_runtime": "See commands.jsonl for real elapsed time; no unmeasured cost claim.",
                                    "future_work": "List new repeated fits, staff time, materials and equipment only as estimates with assumptions."}],
        "decision_rules": [{"family": "anomaly", "rule": "quality_violation/outlier_supported/candidate_for_review/cause_undetermined are distinct states; deviations are not deleted."},
                            {"family": "trend", "rule": "Report entity, interval, units, comparator, magnitude, method and sensitivity; non-significance is not equivalence or saturation."},
                            {"family": "cause", "rule": "Association or source co-location alone cannot establish a wet-lab mechanism."}],
        "completion_criteria": ["Every original contract field is populated from actual evidence or an explicit unresolved reason.",
                                 "All five interpretation sections and six design/evidence/risk aspects are reviewed.",
                                 "Numerical conclusions point to exact input locations, formulas and computation versions."]
    }
    audit["basis_and_risk"] = {
        "basis": METHOD_REFERENCES + [{"basis": "Case source material", "location": "Original input manifests and document pages; no external document supersedes local data."}],
        "risks": [{"risk": "Pseudo-replication, confounded batches and ambiguous units or denominators can invalidate a numeric conclusion."},
                   {"risk": "MAD scales at zero and threshold scans must not be turned into false p-values or universal cutoffs."},
                   {"risk": "Original paper claims, workbook cached calculations and independent recomputation may disagree; retain and explain the three layers."}],
        "exceptions": data.get("issues", []) + analysis.get("issues", [])
    }
    audit["confirmation_items"] = [
        {"kind": "safety", "state": "unknown", "owner_role": "responsible scientist/laboratory safety officer",
         "action": "Any future experimental work requires case-specific hazards, local SOPs, authorized personnel and stop criteria.", "blocks": "physical execution only"},
        {"kind": "ethics", "state": "unknown", "owner_role": "appropriate local governance body",
         "action": "Only claim non-applicability or approval from actual study records; no ethical approval is inferred.", "blocks": "regulated or human/animal work"},
        {"kind": "equipment", "state": "unknown", "owner_role": "instrument custodian",
         "action": "Check calibration, range, censoring, maintenance, device compatibility and access in source records before future measurements.", "blocks": "device operation"},
        {"kind": "domain_expert", "state": "pending", "owner_role": "study-domain expert/statistician",
         "action": "Confirm source thresholds, experimental units, scientific interpretation of operational transitions and causes.", "blocks": "causal or validated-method claims"},
    ]
    audit["uncertainty"] = [
        {"scope": "causal interpretation", "statement": "Statistical departures and explanatory hypotheses are separate; no root cause is established solely by this analysis."},
        {"scope": "independent comparison", "statement": "No official benchmark score, efficiency gain or causal improvement is inferred from this runtime artifact."},
    ]
    audit["sources"] = [
        {"id": "S{:03d}".format(index), "path": item.get("path"), "name": item.get("name"),
         "sha256": item.get("sha256"), "size": item.get("size"), "kind": item.get("kind"),
         "status": item.get("status"), "parser": item.get("parser")}
        for index, item in enumerate(data.get("inputs", []), 1)
    ]
    audit["facts"] = analysis.get("facts", [])
    audit["outlier_candidates"] = analysis.get("outlier_candidates", [])
    audit["trend_candidates"] = analysis.get("trend_candidates", [])
    audit["anomalies"] = analysis.get("source_anomalies", [])
    audit["issues"] = data.get("issues", []) + analysis.get("issues", [])
    audit["coverage"] = analysis.get("coverage", {})
    audit["inputs"] = data.get("inputs", [])
    audit["change_records"] = [{"stage": "bootstrap", "state": "facts_ready", "timestamp": timestamp(),
                                "change": "Created deterministic evidence/state files from actual inputs; no original input modified.",
                                "output": "work/extracted.json; work/analysis.json"}]
    return audit


def write_reasoning_template(work: Path, case: dict, analysis: dict) -> None:
    template = {
        "case_id": case.get("case_id"),
        "status": "partial",
        "fields": {},
        "trends": [{"statement": "Replace with each observed trend/contrast, interval, effect magnitude and unit, model/control, and source fact IDs.", "evidence": ["F..."]}],
        "anomalies": [{"entity": "exact source sample/batch/channel/split/time/trace", "statement": "Quantify deviation versus a concrete reference, declare scale and threshold basis.",
                        "status": "quality_violation|outlier_supported|candidate_for_review|cause_undetermined", "evidence": ["F..."], "inclusion": "retained"}],
        "possible_causes": [{"candidate": "Evidence-ranked alternative, not a guessed mechanism", "supporting_fact_ids": ["F..."], "opposing_or_missing_evidence": [],
                              "association_or_causation": "association unless a controlled manipulation is provided", "discriminating_action": "What distinguishes this candidate from its rival?"}],
        "rechecks": [{"target": "exact source locator", "steps": ["specific operation another person can perform"], "expected_result": "",
                       "pass_fail_criteria": "Given observed and acceptable ranges", "state": "proposed"}],
        "followup_experiments": [{"hypothesis": "competing cause or unresolved evidence gap", "variables": [], "controls": [], "independent_experimental_units": [],
                                   "parameters_range": "Only a domain-supported proposal; not a measurement", "resources_time_cost": "Specify source or unknown",
                                   "decision_rule": "if X is observed, candidate A is supported/weakened; if Y ...", "state": "proposed_not_executed", "confirmations": ["safety", "ethics", "equipment", "expert as applicable"]}],
        "uncertainty": analysis.get("issues", []), "confirmation_items": [], "change_records": [],
        "result_text": "Optional honest narrative summary constrained to supplied facts."
    }
    dump_json(work / "reasoning.example.json", template)
    parts = {
        "001-fields.json": {"case_id": case.get("case_id"), "status": "partial", "fields": template["fields"]},
        "002-evidence.json": {"trends": template["trends"], "anomalies": template["anomalies"], "possible_causes": template["possible_causes"]},
        "003-actions.json": {"rechecks": template["rechecks"], "followup_experiments": template["followup_experiments"],
                            "uncertainty": [], "confirmation_items": [], "change_records": [], "result_text": template["result_text"]},
    }
    for name, part in parts.items():
        dump_json(work / "reasoning.parts" / name, part)


def pdf_review(data: dict) -> list:
    findings = []
    keywords = re.compile(r"method|calibrat|precis|accuracy|specific|limit of detection|limit of quantitation|\bLOD\b|\bLOQ\b|recovery|linearity|range|experimental|result|RSD|standard curve|材料|方法|结果|标准曲线|精密度|回收率|检测限|定量限", re.I)
    for document in data.get("documents", []):
        for page in document.get("pages", []):
            text = page.get("text", "")
            if not keywords.search(text):
                continue
            snippets = []
            for match in list(keywords.finditer(text))[:5]:
                left = max(0, match.start() - 200)
                right = min(len(text), match.end() + 650)
                snippets.append(text[left:right])
            if snippets:
                page_id = next((item.get("id") for item in data.get("documents", []) if item.get("path") == document.get("path")), "d001")
                findings.append({"document_path": document.get("path"), "pdf_page_1based": page.get("page"),
                                 "full_page_text": "pdf_text/%s-p%03d.txt" % (page_id, page.get("page", 1)),
                                 "snippets": snippets[:3], "usage": "Read the cited original page text before deriving a method or threshold."})
    return findings


def reasoning_packet(case: dict, task_text: str, data: dict, analysis: dict, audit: dict) -> dict:
    paper_locations = pdf_review(data)
    packet = {
        "case_id": case.get("case_id"), "actual_case": copy.deepcopy(case), "actual_task": task_text,
        "output_contract_original": case.get("output_contract"),
        "fact_file": "analysis.json", "full_input_snapshot": "extracted.json",
        "computational_results": {"count": "object", "available_in": "packets/computation.json", "lossless": True},
        "cursor_index": {},
        "table_inventory": [{"table_id": item.get("table_id"), "name": item.get("name", item.get("table_id")),
                             "shape": item.get("shape"), "columns": [column.get("name") for column in item.get("columns", []) if isinstance(column, Mapping)]}
                            for item in analysis.get("table_summaries", [])],
        "source_coverage": analysis.get("coverage", {}),
        "source_declared_results": {"count": len(analysis.get("declared_rule_results", [])), "available_in": "packets/declared-rules.json", "lossless": True},
        "fact_query": "python3 <skill-root>/scripts/query_facts.py /app/work/analysis.json --metric KEYWORD or --ids F0001,F0002 or --sheet SHEET_NAME [--kind derived] [--limit 20] [--skip 20]; iterate until returned_facts is zero",
        "trend_candidates": {"count": len(analysis.get("trend_candidates", [])), "available_in": "packets/trends.json", "lossless": True},
        "outlier_candidates": {"count": len(analysis.get("outlier_candidates", [])), "available_in": "packets/outliers.json", "lossless": True},
        "source_anomalies": {"count": len(analysis.get("source_anomalies", [])), "available_in": "packets/source-anomalies.json", "lossless": True},
        "number_of_computed_facts": len(analysis.get("facts", [])),
        "computed_fact_sample": compact([item for item in analysis.get("facts", []) if item.get("declared_rule")][:15] + analysis.get("facts", [])[:35]),
        "computed_fact_sample_status": "Explicit preview only; every fact is available without compaction in packets/facts.json and its listed pages.",
        "paper_method_and_result_locations": {"count": len(paper_locations), "available_in": "packets/paper-locations.json", "lossless": True},
        "methods": {"count": len(analysis.get("methods", [])), "available_in": "packets/methods.json", "lossless": True},
        "issues": compact_issues(audit.get("issues", [])),
        "model_execution_policy": _model_execution_policy(),
        "required_work": [
            "Read actual_task and original output_contract before mapping conclusions. Interpret source_declared_results in its experimental context before unconditioned global anomaly candidates.",
            "Use exact computed values and fact IDs via source-declared results or the fact_query helper; read cited original PDF pages and workbook ranges for source claims not already computed.",
            "For every required collection read its complete JSON shard or every listed page in cursor_index. A small chat preview is never the complete collection.",
            "For native curves follow native_trace_csv.instructions: use csv.DictReader over every row in analysis_curve_traces.csv, including all rows beyond any 40/50-item preview; cross-check the actual CSV count with coverage.",
            "Construct trend, anomaly, ranked competing causes, executable rechecks, and proposed follow-up experiments. Never fabricate measurements or p-values.",
            "Distinguish source-reported, recomputed and reexecuted results; retain conflicts rather than choosing a convenient version.",
            "Split model work into reasoning.parts/*.json patches. Each provider-measured response including reasoning must be <12000 tokens; do not use character counts as tokens.",
            "Map all output fields through the fields object, then finalize with the exact command in BOOTSTRAP_NEXT.json. Set status complete only when defensibly finished."
        ]
    }
    packet["issues"]["full_records"] = {"available_in": "packets/issues.json", "lossless": True}
    return packet


def _write_lossless_collection(work: Path, filename: str, collection: str, value) -> dict:
    relative = "packets/" + filename + ".json"
    dump_json(work / relative, value)
    index = {"path": relative, "file": "analysis.json", "collection": collection,
             "sha256": _sha256(work / relative), "lossless": True, "compacted": False,
             "count": len(value) if isinstance(value, (list, dict)) else 1,
             "instruction": "This shard contains the complete unmodified collection. Arrays also have ordered 40-item pages; read every page or this full shard."}
    if isinstance(value, list):
        pages = []
        for page_number, start in enumerate(range(0, len(value), _PACKET_PAGE_SIZE), 1):
            items = value[start:start + _PACKET_PAGE_SIZE]
            page_relative = "packets/" + filename + "-pages/page-%06d.json" % page_number
            stop = start + len(items)
            dump_json(work / page_relative, {"collection": collection, "page": page_number,
                        "page_size": _PACKET_PAGE_SIZE, "first_index_zero_based": start,
                        "stop_index_exclusive": stop, "total_items": len(value), "items": items})
            pages.append({"path": page_relative, "page": page_number, "first_index_zero_based": start,
                          "stop_index_exclusive": stop, "count": len(items), "sha256": _sha256(work / page_relative)})
        index["pages"] = pages
        index["page_count"] = len(pages)
        index["all_pages_required"] = True
    return index


def _native_trace_csv_access(work: Path, analysis: dict) -> dict:
    path = (work / "analysis_curve_traces.csv").resolve()
    if not path.is_file():
        return {"status": "unavailable", "reason": "No analysis_curve_traces.csv was generated. Do not substitute a preview or invent trace detail.",
                "curve_coverage": analysis.get("curve_analysis", {}).get("coverage", {})}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = sum(1 for _ in csv.DictReader(handle))
    covered = analysis.get("curve_analysis", {}).get("coverage", {}).get("csv_trace_rows")
    return {
        "status": "complete_index" if covered == rows else "count_mismatch_requires_review",
        "path": str(path), "work_relative_path": path.relative_to(work.resolve()).as_posix(),
        "record_count": rows, "csv_trace_rows_in_coverage": covered, "coverage_count_matches": covered == rows,
        "sha256": _sha256(path), "bytes": path.stat().st_size,
        "lossless_export": True,
        "instructions": "This CSV, not the 40/50-item JSON preview, is the full exported native trace table. Read it with csv.DictReader (or pandas only if already permitted) and process ALL record_count rows in deterministic slices such as [0:40], [40:80], ... until EOF; use the measured record_count rather than an expected label count, and do not stop at any page limit. Join to curve/fact IDs as needed.",
        "example_stdlib": "import csv; f=open(r'%s', encoding='utf-8-sig', newline=''); rows=list(csv.DictReader(f)); assert len(rows)==%d" % (str(path), rows),
    }


def write_reasoning_packet(work: Path, case: dict, task_text: str, data: dict, analysis: dict, audit: dict) -> None:
    packet = reasoning_packet(case, task_text, data, analysis, audit)
    names = {
        "table_summaries": "tables", "curve_analysis": "curve-analysis", "computational_results": "computation",
        "facts": "facts", "trend_candidates": "trends", "outlier_candidates": "outliers", "methods": "methods",
        "source_anomalies": "source-anomalies", "declared_rule_results": "declared-rules",
    }
    for key, filename in names.items():
        if key not in analysis:
            continue
        value = analysis.get(key)
        index = _write_lossless_collection(work, filename, key, value)
        packet["cursor_index"][filename] = index
        if key not in packet:
            packet[key] = {"available_in": index["path"], "full_record": "analysis.json",
                           "count": len(value) if isinstance(value, list) else "object", "lossless": True}
    for filename, collection, value in (("issues", "audit.issues", audit.get("issues", [])),
                                        ("paper-locations", "paper_method_and_result_locations", pdf_review(data))):
        packet["cursor_index"][filename] = _write_lossless_collection(work, filename, collection, value)
    packet["native_trace_csv"] = _native_trace_csv_access(work, analysis)
    packet["lossless_access_contract"] = {
        "preview_is_not_data_source": True,
        "complete_policy": "For each relevant collection, read packets/<name>.json or every page listed in cursor_index[<name>].pages; verify count and page intervals. Only source-level reader bounds recorded in analysis may still limit the original analysis, and those limits must be reported rather than hidden.",
        "native_trace_policy": "Read every actual row in native_trace_csv.path using native_trace_csv.instructions; this exposes the complete exported CSV rather than truncating it to a chat preview.",
    }
    serialized = json.dumps(packet, ensure_ascii=False, allow_nan=False)
    packet["packet_bytes_before_json_newline"] = len(serialized.encode("utf-8"))
    dump_json(work / "analysis_packet.json", packet)


def _declared_operations(rules):
    operations = rules.get("operations", []) if isinstance(rules, Mapping) else rules
    if isinstance(operations, Mapping):
        operations = [operations]
    return [item for item in operations or [] if isinstance(item, Mapping)] if isinstance(operations, list) else []


def apply_declared_rules(analysis, data, rules, work):
    operations = _declared_operations(rules)
    if not operations:
        return
    analysis["declared_rule_results"] = []
    analysis["rule_execution_log"] = []
    for operation in operations:
        name = operation.get("operation", "")
        if name in ("fit_declared_model", "binding_model", "nonlinear_declared_model"):
            module_name, function_name = "scientific_models", "analyze_models"
        elif name in ("paired_replicate_range", "derived_ratio_audit", "group_summary_nested_treatment_contrast"):
            module_name, function_name = "rule_engine", "analyze_rules"
        else:
            issue = {"kind": "cannot_determine", "stage": "declared_analysis", "declared_rule": name,
                     "detail": "Unsupported operation: " + str(name), "action": "Write an explicit compatible analysis rule or explain the missing implementation."}
            analysis.setdefault("issues", []).append(issue)
            analysis["declared_rule_results"].append({"operation": name, "status": "unsupported", "issues": [issue]})
            continue
        started = time.monotonic()
        execution = {"operation": name, "module": module_name, "rules": copy.deepcopy(operation), "started_at": timestamp()}
        import_error = None
        try:
            module = __import__(module_name)
            result = getattr(module, function_name)(data, [operation])
        except Exception as exc:
            import_error = exc
            result = {"facts": [], "issues": [{"kind": "runtime_failure", "stage": name,
                                               "detail": type(exc).__name__ + ": " + str(exc)}]}
        next_fact = 1 + max([int(str(item.get("id", "F0"))[1:]) for item in analysis.get("facts", [])
                             if str(item.get("id", "")).startswith("F") and str(item.get("id", ""))[1:].isdigit()] or [0])
        ids = []
        examples = []
        for offset, fact in enumerate(result.get("facts", [])):
            fact_id = "F%04d" % (next_fact + offset)
            fact["id"] = fact_id
            fact["declared_rule"] = name
            fact["rule_source"] = copy.deepcopy(operation.get("basis"))
            analysis.setdefault("facts", []).append(fact)
            ids.append(fact_id)
            if len(examples) < 5:
                examples.append(fact)
            flagged = fact.get("kind") == "quality_violation" or (isinstance(fact.get("value"), Mapping) and fact.get("value", {}).get("status") == "quality_violation")
            if flagged:
                dedupe_key = json.dumps([fact.get("metric"), fact.get("entity")], sort_keys=True, ensure_ascii=False, default=str)
                if dedupe_key not in analysis.setdefault("_source_anomaly_keys", set()):
                    analysis["_source_anomaly_keys"].add(dedupe_key)
                    analysis.setdefault("source_anomalies", []).append(fact)
        analysis.setdefault("issues", []).extend([dict(item, declared_rule=name) if isinstance(item, Mapping) else {"detail": str(item), "declared_rule": name} for item in result.get("issues", [])])
        analysis.setdefault("methods", []).extend([dict(item, declared_rule=name) if isinstance(item, Mapping) else {"detail": str(item), "declared_rule": name} for item in result.get("methods", [])])
        coverage = result.get("coverage", {})
        if coverage:
            analysis.setdefault("coverage", {})["declared_" + name] = coverage
        execution.update({"duration_seconds": round(time.monotonic() - started, 3), "fact_ids": ids,
                          "issues": result.get("issues", []), "coverage": coverage})
        analysis["rule_execution_log"].append(execution)
        analysis["declared_rule_results"].append({"operation": name, "status": "computed" if not import_error else "partial",
                                                  "coverage": coverage, "fact_ids": ids, "fact_examples": examples,
                                                  "issues": result.get("issues", [])})
    analysis["source_anomaly_dedupe_keys"] = sorted(analysis.pop("_source_anomaly_keys", []))
    if analysis.get("declared_rule_results"):
        try:
            render_binding_diagnostics(analysis, work)
        except Exception as exc:
            analysis.setdefault("issues", []).append({"kind": "runtime_failure", "stage": "diagnostic_render", "detail": type(exc).__name__ + ": " + str(exc)})


def render_binding_diagnostics(analysis, work):
    groups = {}
    for fact in analysis.get("facts", []):
        if fact.get("declared_rule") != "fit_declared_model":
            continue
        label = fact.get("entity", {}).get("group_label")
        metric = fact.get("metric", "")
        if not label:
            continue
        group = groups.setdefault(label, {"fit": None, "rows": [], "unit": fact.get("unit"), "fact_id": fact.get("id")})
        if metric == "binding_unbound_fraction_one_to_one_eq11_Kd_fit":
            group["fit"] = fact["value"]
            group["unit"] = fact.get("unit")
        elif metric == "binding_unbound_fraction_one_to_one_eq11_residuals":
            group["rows"] = fact.get("value", {}).get("rows_ordered_as_source", [])
    if not groups or not any(group["fit"] is not None and group["rows"] for group in groups.values()):
        return
    import matplotlib
    matplotlib.use("Agg", force=True)
    from matplotlib import pyplot
    import numpy as np
    destination = work.resolve() / "diagnostics"
    destination.mkdir(parents=True, exist_ok=True)
    figure, axes = pyplot.subplots(2, 3, figsize=(14, 8), constrained_layout=False)
    for axis, (label, group) in zip(axes.flat, sorted(groups.items())):
        try:
            if group["fit"] is None or not group["rows"]:
                axis.text(0.5, 0.5, "Fit or residuals unavailable", ha="center", va="center")
                axis.set_axis_off()
                continue
            records = [row for row in group["rows"] if all(isinstance(row.get(key), (int, float)) and not isinstance(row.get(key), bool) and math.isfinite(row[key]) for key in ("T0", "observed_R", "fitted_R", "residual_observed_minus_fitted"))]
            if not records:
                axis.text(0.5, 0.5, "No complete residual records", ha="center", va="center")
                axis.set_axis_off()
                continue
            records.sort(key=lambda row: row["T0"])
            x = np.array([row["T0"] for row in records], dtype=float)
            observed = np.array([row["observed_R"] for row in records], dtype=float)
            fitted = np.array([row["fitted_R"] for row in records], dtype=float)
            residual = np.array([row["residual_observed_minus_fitted"] for row in records], dtype=float)
            fit = group["fit"]
            l0 = float(fit["L0"]) if "L0" in fit else float(label.split("L0 =", 1)[1].split()[0])
            kd = float(fit["Kd_est"])
            grid = np.unique(np.concatenate((x, np.linspace(float(np.min(x)), float(np.max(x)), 201))))
            term = (kd + grid - l0) / (2.0 * l0)
            theoretical = -term + np.sqrt(term * term + kd / l0)
            axis.plot(grid, theoretical, color="#777777", linewidth=1.2, label="Eq. 11 reconstruction" if axis is axes.flat[0] else None)
            axis.scatter(x, observed, color="#0072B2", zorder=3, s=23, label="Observed" if axis is axes.flat[0] else None)
            axis.scatter(x, residual, color="#D55E00", zorder=4, s=18, label="Observed minus fitted" if axis is axes.flat[0] else None)
            axis.axhline(0.0, color="#999999", linewidth=0.6, linestyle=":")
            axis.set_xscale("linear" if np.min(x) == 0 else "log")
            axis.set_title(label + "  n=" + str(len(records)) + "\nKd=" + format(kd, ".6g") + " " + str(group["unit"] or "") + " (local, not ACI)", fontsize=9)
            axis.set_xlabel("T0 (source units)", fontsize=8)
            axis.set_ylabel("R / residual", fontsize=8)
            axis.tick_params(labelsize=7)
            axis.grid(alpha=0.15)
            axis.margins(x=0.07, y=0.16)
        except (TypeError, ValueError, KeyError, OverflowError) as exc:
            axis.set_axis_off()
            analysis.setdefault("issues", []).append({"kind": "cannot_determine", "stage": "diagnostic_render", "detail": "Group %s could not be visualized: %s" % (label, str(exc))})
    figure.suptitle("Observed points, source-declared model and residuals (reconstruction is not new measurement)", fontsize=11)
    if any(axis.lines or axis.collections for axis in axes.flat):
        axes.flat[0].legend(fontsize=7, loc="best")
    figure.tight_layout(rect=(0, 0.03, 1, 0.95))
    path = destination / "binding-isotherms.png"
    figure.savefig(path, dpi=160, metadata={"Description": "Actual source observations and declared Eq. 11 reconstruction; no added measurements, bootstrap or ACI."})
    pyplot.close(figure)
    relative = path.relative_to(work.resolve()).as_posix()
    if relative not in analysis.setdefault("artifacts", []):
        analysis["artifacts"].append(relative)
    analysis["diagnostic_previews"] = [{"path": relative, "groups": len(groups), "data_kind": "source_observation_and_declared_reconstruction", "not_new_measurements": True, "not_ACI": True}]


def compact_issues(issues):
    priority = {"quality_violation": 0, "runtime_failure": 1, "cannot_determine": 2}
    groups = {}
    for issue in issues if isinstance(issues, list) else []:
        if not isinstance(issue, Mapping):
            continue
        kind = str(issue.get("kind", "unclassified"))
        key = (priority.get(kind, 3), kind, str(issue.get("message", issue.get("detail", issue.get("stage", "")))))
        entry = groups.setdefault(key, {"kind": kind, "count": 0, "examples": [], "severity": "action_required" if kind in priority else "bounded_coverage"})
        entry["count"] += 1
        encoded = json.dumps(issue, sort_keys=True, ensure_ascii=False, default=str)
        if len(entry["examples"]) < 3 and all(json.dumps(item, sort_keys=True, ensure_ascii=False, default=str) != encoded for item in entry["examples"]):
            entry["examples"].append(issue)
    ordered = sorted(groups.values(), key=lambda group: (priority.get(group["kind"], 3), group["kind"]))
    return {"total_issue_records": len(issues) if isinstance(issues, list) else 0, "categories": ordered,
            "note": "This is an aggregate view for context efficiency, not an error count or a claim that bounded analysis failed. Full issue records and source locators remain in analysis.json and audit.json."}


def bootstrap(args) -> int:
    case_path = Path(args.case).resolve()
    task_path = Path(args.task).resolve()
    work = Path(args.work).resolve()
    output = Path(args.output).resolve()
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    case = case_load(case_path)
    task_text = task_path.read_text(encoding="utf-8-sig") if task_path.exists() else ""
    if (output / "result.json").exists():
        previous = load_json(output / "result.json")
        if isinstance(previous, Mapping) and previous.get("case_id") == case.get("case_id") and validate_result(previous, case)["valid"]:
            dump_json(work / "last-good-result.json", previous)
            if (output / "audit.json").exists():
                dump_json(work / "last-good-audit.json", load_json(output / "audit.json"))

    def checkpoint(audit):
        audit["validation"] = None
        result = assemble(case, {"status": "partial"}, audit)
        check = validate_result(result, case, audit)
        audit["validation"] = check
        if isinstance(result.get("skill_audit"), dict):
            result["skill_audit"] = audit
        dump_json(work / "audit.json", audit)
        dump_json(output / "result.json", result)
        dump_json(output / "audit.json", audit)
        dump_json(output / "validation.json", check)
        return result

    audit = empty_audit(case)
    audit["run"].update({"stage": "input_registered", "status": "partial", "complete": False})
    task_sources = [_source_file(task_path, "STASK", "task", "stdlib.utf8-sig")] if task_path.is_file() else []
    audit["sources"] = _deduplicate_sources([_source_file(case_path, "SCASE", "json", "stdlib.json")], task_sources)
    audit["issues"] = [{"kind": "cannot_determine", "stage": "input_registered", "detail": "Original inputs registered; analysis and interpretation are not yet complete."}]
    audit["coverage"] = {"state": "pending_readers", "case_id_preserved": True}
    audit["run"]["model_execution_policy"] = _model_execution_policy()
    checkpoint(audit)
    if not _cooperative_budget_guard(args, work, audit, checkpoint, "before_readers"):
        return 2
    dump_json(work / "case.json", case)
    environment = environment_record()
    dump_json(work / "environment.json", environment)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from readers import discover_and_read
        data = discover_and_read(case_path, task_path, case_path.parent, work)
    except Exception as exc:
        data = {"inputs": [], "tables": [], "documents": [], "arrays": [], "other_files": [],
                "issues": [{"kind": "runtime_failure", "stage": "reader", "detail": type(exc).__name__ + ": " + str(exc),
                            "recovery": "Partial contract saved; inspect errors/bootstrap.log and use a source-compatible reader."}]}
    dump_json(work / "extracted.json", data)
    audit["run"]["stage"] = "sources_read"
    input_sources = [dict(item, id="S%03d" % index) for index, item in enumerate(data.get("inputs", []), 1)]
    audit["sources"] = _deduplicate_sources(audit.get("sources", []), input_sources)
    audit["issues"] = data.get("issues", [])
    audit["coverage"] = {"state": "sources_read", "inputs": len(data.get("inputs", [])), "tables": len(data.get("tables", [])),
                         "documents": len(data.get("documents", [])), "analysis_pending": True}
    checkpoint(audit)
    if not _cooperative_budget_guard(args, work, audit, checkpoint, "before_analysis"):
        return 2
    analysis_interface = {"task_text_forwarded": False, "mode": "analysis_not_started", "limitation": "Analysis was not started."}
    try:
        from analysis import analyze
        analysis, analysis_interface = _invoke_analysis(analyze, data, case, work, task_text)
    except Exception as exc:
        analysis_interface["runtime_failure"] = True
        (work / "errors").mkdir(exist_ok=True)
        (work / "errors" / "analysis-caught.log").write_text(traceback.format_exc(), encoding="utf-8")
        analysis = {"facts": [], "outlier_candidates": [], "trend_candidates": [], "source_anomalies": [],
                    "computational_results": {}, "curve_analysis": {}, "coverage": {}, "methods": [],
                    "issues": [{"kind": "runtime_failure", "stage": "analysis", "detail": type(exc).__name__ + ": " + str(exc),
                                "recovery": "Inspect native input tables and repair the identified stage; do not invent values."}]}
    analysis_interface.setdefault("runtime_failure", False)
    analysis.setdefault("coverage", {})["analysis_interface"] = analysis_interface
    dump_json(work / "analysis.json", analysis)
    try:
        from domain_analysis import analyze_domain
        domain = analyze_domain(data, case)
        next_fact = 1 + max([int(str(item.get("id", "F0"))[1:]) for item in analysis.get("facts", []) if str(item.get("id", "")).startswith("F") and str(item.get("id", ""))[1:].isdigit()] or [0])
        for offset, fact in enumerate(domain.get("facts", [])):
            fact["id"] = "F%04d" % (next_fact + offset)
            analysis.setdefault("facts", []).append(fact)
        analysis.setdefault("issues", []).extend(domain.get("issues", []))
        analysis.setdefault("methods", []).extend(domain.get("methods", []))
        if domain.get("coverage"):
            analysis.setdefault("coverage", {})["domain"] = domain["coverage"]
    except ImportError as exc:
        if getattr(exc, "name", None) not in (None, "domain_analysis"):
            analysis.setdefault("issues", []).append({"kind": "runtime_failure", "stage": "domain_analysis", "detail": str(exc)})
    except Exception as exc:
        analysis.setdefault("issues", []).append({"kind": "runtime_failure", "stage": "domain_analysis", "detail": type(exc).__name__ + ": " + str(exc)})
    apply_declared_rules(analysis, data, case.get("analysis_rules_v1", case.get("analysis_rules", [])), work)
    dump_json(work / "analysis.json", analysis)
    source_baseline = copy.deepcopy(audit.get("sources", []))
    audit = scientific_audit(case, data, analysis, environment)
    audit["sources"] = _deduplicate_sources(source_baseline, audit.get("sources", []))
    audit["run"]["model_execution_policy"] = _model_execution_policy()
    checkpoint(audit)
    if not _cooperative_budget_guard(args, work, audit, checkpoint, "before_reasoning_packet"):
        return 2
    write_reasoning_packet(work, case, task_text, data, analysis, audit)
    write_reasoning_template(work, case, analysis)
    semantic_seed = _write_reasoning_seed(work, case, analysis)
    if not _cooperative_budget_guard(args, work, audit, checkpoint, "after_reasoning_packet"):
        return 2
    next_action = {
        "state": "FACTS_READY_NOT_FINAL", "case_id": case.get("case_id"),
        "read_first": str(work / "analysis_packet.json"),
        "semantic_seed": {
            "path": str(semantic_seed) if semantic_seed else None,
            "state": "available_partial_requires_semantic_review" if semantic_seed else "unavailable",
            "error_file": str(work / "reasoning-seed-error.json") if not semantic_seed else None,
            "use": "Use the finalize --seed option with a compact semantic review, not a wholesale manual JSON rewrite. Verify task semantics and the complete original output_contract. The seed keeps real numeric results; the reviewer supplies meaning, evidence gaps and next actions. Do not promote partial to complete without review."
        },
        "original_task": str(task_path), "original_case": str(case_path),
        "reasoning_output": str(work / "reasoning.parts"),
        "fallback_single_reasoning_output": str(work / "reasoning.json"),
        "model_execution_policy": _model_execution_policy(),
        "finalize_command": "python3 " + str(Path(__file__).resolve()) + " finalize --case " + str(case_path) +
                            " --work " + str(work) + " --output " + str(output) + " --reasoning " + str(work / "reasoning.parts") +
                            (" --seed " + str(semantic_seed) if semantic_seed else ""),
        "no_final_claim_before": "FINAL_OK",
        "recommended_review_sequence": [
            "Verify the actual case, task, output_contract and semantic_seed first. Keep computed group summaries, counts, units and fact references.",
            "Read only required source pages/native ranges or result shards; batch reusable missing-source computation instead of writing many tiny scripts.",
            "Write one compact semantic review in reasoning.json or nonoverlapping parts. Do not replace correct deterministic sections with richer but unverified prose merely to increase detail.",
            "For original fields use fields: {original.path: value} or fields: {path: original.path, value: value}. Never nest a fields object inside fields or inside experiment_design.",
            "Finalize by 300 seconds where possible; verify by 540 seconds. Stop discretionary model generation by 14 native requests and retain the best honest partial rather than exhausting the official limits."
        ]
    }
    dump_json(work / "BOOTSTRAP_NEXT.json", next_action)
    print(json.dumps(next_action, ensure_ascii=False))
    return 0


def load_reasoning(path: Path, work: Path) -> dict:
    candidate = path
    if not candidate.exists() and (work / "reasoning.json").exists():
        candidate = work / "reasoning.json"
    if not candidate.exists():
        raise ValueError("Reasoning patch was not saved. Write reasoning.json or reasoning.parts/*.json and rerun finalizer; existing result is preserved.")
    if candidate.is_dir():
        part_paths = sorted(item for item in candidate.glob("*.json") if item.is_file())
        if not part_paths:
            raise ValueError("Reasoning patch directory contains no JSON parts; existing result is preserved.")
        patch = {}
        for part_path in part_paths:
            part = load_json(part_path)
            if not isinstance(part, dict):
                raise ValueError("Every reasoning part must be an object: " + str(part_path))
            patch = merge(patch, part)
        return patch
    patch = load_json(candidate)
    if not isinstance(patch, dict):
        raise ValueError("reasoning.json root is not an object; existing result is preserved")
    return patch


def _merge_seed_review(case: dict, seed_path: Path, review: dict, work: Path):
    seed = load_json(seed_path)
    if not isinstance(seed, dict) or seed.get("status") != "partial":
        raise ValueError("--seed must be the generated partial reasoning.seed.json")
    if seed.get("case_id") != case.get("case_id"):
        raise ValueError("Seed case_id does not match the original case")
    if review.get("case_id", case.get("case_id")) != case.get("case_id"):
        raise ValueError("Reasoning review changed case_id")
    digest_file = seed_path.with_name("reasoning.seed.sha256")
    if not digest_file.is_file():
        raise ValueError("Missing the seed/analysis digest; run bootstrap before --seed")
    if digest_file.read_text(encoding="utf-8").strip() != _sha256(work / "analysis.json"):
        raise ValueError("The seed is stale relative to analysis.json; refresh bootstrap, do not mix versions")
    allowed = {"case_id", "status", "fields", "trends", "anomalies", "possible_causes", "rechecks",
               "followup_experiments", "experiment_design", "basis_and_risk", "uncertainty",
               "confirmation_items", "sources", "issues", "change_records", "result_text", "source_artifacts"}
    unknown = sorted(set(review) - allowed)
    if unknown:
        raise ValueError("Unsupported review fields; map original output fields through fields: " + ",".join(unknown))
    merged = copy.deepcopy(seed)
    for key, value in review.items():
        if key in {"experiment_design", "basis_and_risk", "trends", "anomalies", "possible_causes",
                  "rechecks", "followup_experiments", "result_text"}:
            merged[key] = copy.deepcopy(value)
        elif key == "status":
            if value not in {"complete", "partial", "failed"}:
                raise ValueError("status must be complete, partial or failed")
            merged["status"] = value
        elif key == "fields":
            if isinstance(value, Mapping) and set(value) == {"path", "value"} and isinstance(value["path"], str):
                value = {value["path"]: value["value"]}
            if not isinstance(value, Mapping):
                raise ValueError("fields must contain a mapping of the original output fields, or one {path, value} assignment")
            merged[key] = merge(merged.get(key, {}), value)
        elif key in {"uncertainty", "confirmation_items", "sources", "issues", "change_records"}:
            merged[key] = merge(merged.get(key, []), value)
        elif key == "source_artifacts":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError("source_artifacts must be paths relative to work")
    artifact_paths = []
    for item in review.get("source_artifacts", []):
        source = Path(item)
        if source.is_absolute():
            source = source.resolve()
        else:
            source = (work / source).resolve()
        try:
            relative = source.relative_to(work)
        except ValueError as exc:
            raise ValueError("source_artifacts must stay inside work: " + item) from exc
        if not source.is_file() or source.is_symlink():
            raise ValueError("source_artifact is absent, not a regular work file, or is a symlink: " + item)
        artifact_paths.append(str(relative))
    merged["change_records"] = merge(merged.get("change_records", []), [{
        "stage": "seed_semantic_review", "seed_path": str(seed_path),
        "meaning": "Combined reviewed semantic fields with the deterministic partial evidence seed; this is not a claim of automatic scientific validation."
    }])
    return merged, artifact_paths


def _write_reasoning_seed(work: Path, case: dict, analysis: dict):
    from reasoning_seed import make_seed
    path = work / "reasoning.seed.json"
    try:
        dump_json(path, make_seed(case, analysis))
        path.with_name("reasoning.seed.sha256").write_text(_sha256(work / "analysis.json") + "\n", encoding="utf-8")
        error = work / "reasoning-seed-error.json"
        if error.exists():
            error.unlink()
        return path
    except Exception as exc:
        dump_json(work / "reasoning-seed-error.json", {
            "state": "unavailable", "detail": type(exc).__name__ + ": " + str(exc),
            "preserved": "Computed facts and partial output remain available; the seed was not promoted to a complete result."
        })
        (work / "errors").mkdir(exist_ok=True)
        (work / "errors" / "reasoning-seed.log").write_text(traceback.format_exc(), encoding="utf-8")
        return None


def _register_computational_artifact(analysis: dict, work: Path) -> dict:
    artifacts = analysis.setdefault("artifacts", [])
    if "computational_results" in analysis and analysis.get("computational_results") is not None:
        relative = "computational_results.json"
        dump_json(work / relative, analysis["computational_results"])
        if relative not in artifacts:
            artifacts.append(relative)
    if (work / "analysis_curve_traces.csv").is_file():
        relative = "analysis_curve_traces.csv"
        if relative not in artifacts:
            artifacts.append(relative)
    return analysis


def copy_artifacts(analysis: dict, work: Path, output: Path) -> list:
    manifest = []
    seen_targets = set()
    source_root = work.resolve()
    destination_root = (output / "artifacts").resolve()
    for artifact in analysis.get("artifacts", []):
        candidate = Path(str(artifact))
        if not candidate.is_absolute():
            candidate = work / candidate
        try:
            source = candidate.resolve()
            work_relative = source.relative_to(source_root)
        except (ValueError, OSError):
            continue
        if not source.is_file() or source.is_symlink():
            continue
        target = (destination_root / work_relative).resolve()
        target.relative_to(destination_root)
        output_relative = target.relative_to(output.resolve()).as_posix()
        if output_relative in seen_targets:
            continue
        seen_targets.add(output_relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        source_sha = _sha256(source)
        target_sha = _sha256(target)
        if source_sha != target_sha:
            raise RuntimeError("Artifact SHA changed during copy: " + str(source))
        manifest.append({"source": str(source), "source_relative_path": work_relative.as_posix(),
                         "relative_path": output_relative, "sha256": target_sha, "bytes": target.stat().st_size})
    return manifest


def finalize(args) -> int:
    case_path = Path(args.case).resolve()
    work = Path(args.work).resolve()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    case = case_load(case_path)
    analysis = load_json(work / "analysis.json") if (work / "analysis.json").exists() else {}
    patch = load_reasoning(Path(args.reasoning).resolve(), work)
    if getattr(args, "seed", None):
        patch, artifact_paths = _merge_seed_review(case, Path(args.seed).resolve(), patch, work)
        for relative in artifact_paths:
            if relative not in analysis.setdefault("artifacts", []):
                analysis["artifacts"].append(relative)
    audit = empty_audit(case)
    if (work / "audit.json").exists():
        audit = load_json(work / "audit.json")
    if (output / "audit.json").exists():
        previous_audit = load_json(output / "audit.json")
        if previous_audit.get("case_id") == case.get("case_id") and previous_audit.get("run", {}).get("stage") == "finalize":
            audit = previous_audit
    previous_result = load_json(output / "result.json") if (output / "result.json").exists() else None
    previous_valid = isinstance(previous_result, Mapping) and previous_result.get("case_id") == case.get("case_id")
    if _budget_snapshot(args)["exhausted"]:
        if previous_valid and validate_result(previous_result, case)["valid"]:
            state = {"state": "BUDGET_EXHAUSTED_PREVIOUS_RESULT_PRESERVED",
                     "budget": _budget_snapshot(args), "result": str(output / "result.json")}
            dump_json(work / "BUDGET_EXHAUSTED.json", state)
            print(json.dumps(state, ensure_ascii=False))
            return 2

        def save_budget_partial(current):
            result = assemble(case, {"status": "partial"}, current)
            check = validate_result(result, case, current)
            current["validation"] = check
            dump_json(work / "audit.final.json", current)
            dump_json(output / "result.json", result)
            dump_json(output / "audit.json", current)
            dump_json(output / "validation.json", check)
            (output / "analysis_report.md").write_text(render_markdown(result, current), encoding="utf-8")

        audit["run"]["stage"] = "finalize_budget"
        _cooperative_budget_guard(args, work, audit, save_budget_partial, "finalize_budget")
        return 2
    analysis = _register_computational_artifact(analysis, work)
    task_path = case_path.parent / 'task.md'
    final_task_sources = [_source_file(task_path, "STASK", "task", "stdlib.utf8-sig")] if task_path.is_file() else []
    audit["sources"] = _deduplicate_sources([_source_file(case_path, "SCASE", "json", "stdlib.json")], final_task_sources, audit.get("sources", []))
    artifact_manifest = copy_artifacts(analysis, work, output)
    audit["artifacts"] = artifact_manifest
    previous_artifact_manifest = previous_audit.get("artifacts") if 'previous_audit' in locals() and isinstance(previous_audit, Mapping) else None
    fingerprint = hashlib.sha256(json.dumps({"patch": patch, "analysis": analysis, "skill_environment": environment_record()}, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
    if previous_valid and audit.get("run", {}).get("reasoning_fingerprint") == fingerprint and previous_artifact_manifest == artifact_manifest:
        check = validate_result(previous_result, case, audit)
        if check["valid"]:
            print(json.dumps({"state": "FINAL_OK", "unchanged": True, "case_id": case.get("case_id"),
                              "run_status": audit.get("run", {}).get("status"), "validation": check}, ensure_ascii=False))
            return 0
    previous_run = copy.deepcopy(audit.get("run", {}))
    audit_patch, _ = canonical_patch(patch)
    audit = merge_audit(audit, audit_patch)
    audit["run"].update({"stage": "finalize", "reasoning_fingerprint": fingerprint,
                         "status": patch.get("status") if patch.get("status") in ("complete", "partial", "failed") else "partial"})
    audit["run"]["complete"] = audit["run"]["status"] == "complete"
    effective_patch = dict(patch, status=audit["run"]["status"])
    try:
        result = assemble(case, effective_patch, audit)
        check = validate_result(result, case, audit)
    except ValueError as exc:
        check = {"valid": False, "errors": [str(exc)], "warnings": []}
    if check["errors"]:
        audit["run"].update({"status": "partial", "complete": False})
        result = assemble(case, dict(patch, status="partial"), audit)
        check = validate_result(result, case, audit)
    if not check["valid"]:
        dump_json(work / "rejected-final-validation.json", check)
        if previous_valid and validate_result(previous_result, case)["valid"]:
            print(json.dumps({"state": "FINAL_REJECTED_PREVIOUS_RESULT_PRESERVED", "result": str(output / "result.json"),
                              "validation": check}, ensure_ascii=False))
            return 2
        audit["issues"] = audit.get("issues", []) + [{"kind": "runtime_failure", "stage": "finalize", "detail": "Final result did not satisfy the original contract; see rejected-final-validation.json."}]
    audit["validation"] = check
    audit["artifacts"] = artifact_manifest
    audit["change_records"] = audit.get("change_records", []) + [{"stage": "finalize", "timestamp": timestamp(),
        "change": "Applied reasoning patch, copied only real generated artifacts, and validated against the original case contract.",
        "previous_run": previous_run, "current_run": audit["run"]}]
    if isinstance(result.get("skill_audit"), dict):
        result["skill_audit"] = audit
    dump_json(work / "audit.final.json", audit)
    dump_json(output / "result.json", result)
    dump_json(output / "audit.json", audit)
    dump_json(output / "validation.json", check)
    (output / "analysis_report.md").write_text(render_markdown(result, audit), encoding="utf-8")
    print(json.dumps({"state": "FINAL_OK" if check["valid"] else "FINAL_PARTIAL",
                      "case_id": case.get("case_id"), "result": str(output / "result.json"),
                      "run_status": audit["run"]["status"], "validation": check}, ensure_ascii=False))
    return 0 if check["valid"] else 2


def _reset_declared_products(analysis: dict) -> dict:
    reset = copy.deepcopy(analysis)
    operations = {"fit_declared_model", "binding_model", "nonlinear_declared_model", "paired_replicate_range", "derived_ratio_audit", "group_summary_nested_treatment_contrast"}
    for key in ("facts", "source_anomalies"):
        reset[key] = [item for item in reset.get(key, []) if not item.get("declared_rule")]
    reset["issues"] = [item for item in reset.get("issues", []) if not (
        item.get("declared_rule") or item.get("operation") in operations or "rule_index" in item or
        str(item.get("stage", "")).startswith(("declared_analysis", "scientific_models", "paired_replicate_range", "derived_ratio_audit", "group_summary_nested_treatment_contrast", "diagnostic_render"))
    )]
    reset["methods"] = [item for item in reset.get("methods", []) if not (
        item.get("declared_rule") or item.get("operation") in operations or "rule_index" in item or
        str(item.get("name", "")) == "binding_unbound_fraction_one_to_one_eq11"
    )]
    preview_artifacts = {item.get("path") for item in reset.get("diagnostic_previews", []) if isinstance(item, Mapping)}
    reset["artifacts"] = [item for item in reset.get("artifacts", []) if item not in preview_artifacts]
    reset["diagnostic_previews"] = []
    reset["rule_examples"] = []
    reset["declared_rule_results"] = []
    reset["rule_execution_log"] = []
    reset["source_anomaly_dedupe_keys"] = []
    reset.pop("_source_anomaly_keys", None)
    coverage = reset.get("coverage", {})
    if isinstance(coverage, dict):
        reset["coverage"] = {key: value for key, value in coverage.items() if not key.startswith("declared_")}
    return reset


def expand(args) -> int:
    path = Path(args.case).resolve()
    work = Path(args.work).resolve()
    output = Path(args.output).resolve()
    case = case_load(path)
    rules_path = Path(args.rules).resolve()
    has_explicit_rules = (work / "analysis_rules.json").is_file()
    if rules_path.is_file():
        rules = load_json(rules_path)
        has_explicit_rules = True
    else:
        rules = case.get("analysis_rules_v1", case.get("analysis_rules"))
    if not _declared_operations(rules):
        print(json.dumps({"state": "NO_DECLARED_RULES", "detail": "First bootstrap, inspect actual source methods, then save supported rules to analysis_rules.json."}, ensure_ascii=False))
        return 0
    data = load_json(work / "extracted.json")
    original_analysis = load_json(work / "analysis.json") if (work / "analysis.json").exists() else {}
    previous_result = load_json(output / "result.json") if (output / "result.json").exists() else None
    previous_valid = isinstance(previous_result, Mapping) and previous_result.get("case_id") == case.get("case_id") and validate_result(previous_result, case)["valid"]
    previous_output_audit = load_json(output / "audit.json") if (output / "audit.json").exists() else {}
    previous_complete = previous_valid and (previous_result.get("status") == "complete" or isinstance(previous_output_audit, Mapping) and previous_output_audit.get("run", {}).get("status") == "complete")
    analysis = _reset_declared_products(original_analysis)
    try:
        apply_declared_rules(analysis, data, rules, work)
        dumped = json.dumps(analysis, ensure_ascii=False, allow_nan=False)
        dump_json(work / "analysis.json", analysis)
    except Exception as exc:
        dump_json(work / "analysis.json", original_analysis)
        print(json.dumps({"state": "EXPANSION_FAILED_ORIGINAL_ANALYSIS_RESTORED", "error": type(exc).__name__ + ": " + str(exc)}, ensure_ascii=False))
        return 2
    environment = environment_record()
    audit = scientific_audit(case, data, analysis, environment)
    audit["run"]["stage"] = "declared_analysis"
    audit["run"]["model_execution_policy"] = _model_execution_policy()
    audit["declared_rules"] = {"path": str(rules_path) if has_explicit_rules else "case.analysis_rules_v1", "hash": hashlib.sha256(json.dumps(rules, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()}
    if has_explicit_rules:
        audit["declared_rules"]["authorized_by"] = "Agent-extracted source method; inspect its evidence before treating it as an original experiment design"
    task_path = path.parent / "task.md"
    task_text = task_path.read_text(encoding="utf-8-sig") if task_path.is_file() else ""
    task_sources = [_source_file(task_path, "STASK", "task", "stdlib.utf8-sig")] if task_path.is_file() else []
    audit["sources"] = _deduplicate_sources([_source_file(path, "SCASE", "json", "stdlib.json")], task_sources, audit.get("sources", []))
    result = assemble(case, {"status": "partial"}, audit)
    check = validate_result(result, case, audit)
    if not check["valid"]:
        if previous_valid:
            dump_json(work / "rejected-expansion-validation.json", check)
            print(json.dumps({"state": "EXPANSION_REJECTED_PREVIOUS_RESULT_PRESERVED", "validation": check}, ensure_ascii=False))
            return 2
        return_code = 2
    else:
        return_code = 0
    audit["validation"] = check
    if isinstance(result.get("skill_audit"), dict):
        result["skill_audit"] = audit
    dump_json(work / "audit.json", audit)
    if not check["errors"] and not previous_complete:
        dump_json(output / "result.json", result)
        dump_json(output / "audit.json", audit)
        dump_json(output / "validation.json", check)
    write_reasoning_packet(work, case, task_text, data, analysis, audit)
    _write_reasoning_seed(work, case, analysis)
    print(json.dumps({"state": "EXPANDED_SOURCE_RULES_PREVIOUS_COMPLETE_PRESERVED" if previous_complete else "EXPANDED_SOURCE_RULES",
                      "previous_complete_output_preserved": previous_complete, "output_updated": bool(not check["errors"] and not previous_complete),
                      "validation": check, "declared_results_count": len(analysis.get("declared_rule_results", [])),
                      "analysis_bytes": len(dumped.encode("utf-8"))}, ensure_ascii=False))
    return return_code


def validate(args) -> int:
    output = Path(args.output).resolve()
    result = load_json(output / "result.json")
    case = case_load(Path(args.case).resolve())
    audit = load_json(output / "audit.json") if (output / "audit.json").exists() else None
    check = validate_result(result, case, audit)
    print(json.dumps(check, ensure_ascii=False))
    return 0 if check["valid"] else 2


def parse_args():
    parser = argparse.ArgumentParser(description="Evidence compiler for experimental result interpretation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("bootstrap", "expand", "finalize", "validate"):
        parser_cmd = subparsers.add_parser(command)
        parser_cmd.add_argument("--case", default="/input/case.json")
        parser_cmd.add_argument("--output", default="/app/output")
        parser_cmd.add_argument("--wall-budget-seconds", type=float, default=_NATIVE_WALL_LIMIT_SECONDS)
        if command != "validate":
            parser_cmd.add_argument("--work", default="/app/work")
        if command == "bootstrap":
            parser_cmd.add_argument("--task", default="/input/task.md")
        if command == "expand":
            parser_cmd.add_argument("--rules", default="/app/work/analysis_rules.json")
        if command == "finalize":
            parser_cmd.add_argument("--reasoning", default="/app/work/reasoning.json")
            parser_cmd.add_argument("--seed", default=None, help="Optionally merge the generated partial seed with a compact semantic review, rather than rewriting all computed fields.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_id = None
    try:
        if Path(args.case).exists():
            case_id = case_load(Path(args.case)).get("case_id")
    except Exception:
        pass
    output = Path(args.output).resolve()
    default_work = output.parent / "work" if args.command == "validate" else Path("/app/work")
    work = Path(getattr(args, "work", str(default_work))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    args._native_monotonic_start = start
    args._native_work = work
    _shared_wall_start.monotonic_start = start
    try:
        if args.command == "bootstrap":
            return run_stage("bootstrap", work, output, case_id, start, bootstrap, args)
        if args.command == "expand":
            return run_stage("expand", work, output, case_id, start, expand, args)
        if args.command == "finalize":
            return run_stage("finalize", work, output, case_id, start, finalize, args)
        return run_stage("validate", work, output, case_id, start, validate, args)
    except Exception as exc:
        failure = {"case_id": case_id, "status": "failed", "stage": args.command,
                   "issues": [{"kind": "runtime_failure", "detail": type(exc).__name__ + ": " + str(exc)}],
                   "uncertainty": ["The run did not complete; do not infer scientific conclusions from empty fields."],
                   "sources": [], "change_records": [{"stage": args.command, "status": "failed"}]}
        try:
            case = case_load(Path(args.case))
            existing = load_json(output / "result.json") if (output / "result.json").exists() else None
            if isinstance(existing, Mapping) and existing.get("case_id") == case.get("case_id") and validate_result(existing, case)["valid"]:
                failure["preserved_previous_result"] = True
                print(json.dumps({"state": "RUN_FAILED_PREVIOUS_RESULT_PRESERVED", "detail": failure["issues"][0]}, ensure_ascii=False))
                return 1
            fallback = empty_audit(case)
            fallback["issues"] = failure["issues"]
            fallback["run"] = {"status": "failed", "stage": args.command, "complete": False}
            result = assemble(case, {}, fallback)
            dump_json(output / "result.json", result)
        except Exception:
            dump_json(output / "result.json", failure)
        print(json.dumps({"state": "RUN_FAILED", "detail": failure["issues"][0]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
