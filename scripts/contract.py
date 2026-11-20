"""Dynamic output-contract projection and bounded JSON-Schema validation."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple


SCHEMA_KEYS = {"type", "properties", "items", "required", "additionalProperties", "$schema", "$ref", "$defs", "definitions", "const", "enum", "minItems", "maxItems", "pattern", "allOf", "anyOf", "oneOf"}
META_KEYS = SCHEMA_KEYS | {"title", "description", "format", "examples", "default", "version", "name"}
FIELD_LIST_KEYS = ("fields", "required_fields", "output_fields", "keys", "required_keys", "required")
WRAPPERS = ("output", "result", "result.json", "schema", "json_schema", "response_format", "output_schema")
DEFAULT_FIELDS = ("experiment_design", "basis_and_risk", "confirmation_items", "trends", "anomalies", "possible_causes", "rechecks", "followup_experiments", "status", "sources", "uncertainty", "change_records")


def load_json(path: Path) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key: " + key)
            result[key] = value
        return result
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle, object_pairs_hook=pairs)


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")
        handle.flush()
    temporary.replace(path)


def _types(spec: Any) -> List[str]:
    value = spec.get("type") if isinstance(spec, Mapping) else None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _unwrap_schema(contract: Any) -> Any:
    if not isinstance(contract, Mapping):
        return contract
    for key in ("schema", "json_schema", "output_schema"):
        if isinstance(contract.get(key), Mapping):
            return contract[key]
    response = contract.get("response_format")
    if isinstance(response, Mapping) and isinstance(response.get("json_schema"), Mapping):
        return response["json_schema"].get("schema", response["json_schema"])
    if len(contract) == 1:
        for key in ("output", "result", "result.json"):
            candidate = contract.get(key)
            if isinstance(candidate, Mapping):
                return _unwrap_schema(candidate)
    return contract


def _field(path: Tuple[str, ...], spec: Any = None, description: str = "") -> Dict[str, Any]:
    return {"path": list(path), "pointer": "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1") for part in path),
            "description": description, "spec": copy.deepcopy(spec) if isinstance(spec, Mapping) else {}}


def _resolve_ref(schema: Any, root: Mapping[str, Any]) -> Any:
    seen = set()
    while isinstance(schema, Mapping) and isinstance(schema.get("$ref"), str) and schema["$ref"].startswith("#/"):
        reference = schema["$ref"]
        if reference in seen:
            return {"unsupported_schema": "cyclic $ref " + reference}
        seen.add(reference)
        target: Any = root
        for token in reference[2:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, Mapping) or token not in target:
                return {"unsupported_schema": "unresolved $ref " + reference}
            target = target[token]
        schema = target
    return schema


def _walk_schema(schema: Any, path: Tuple[str, ...], out: List[Dict[str, Any]], root: Mapping[str, Any]) -> None:
    schema = _resolve_ref(schema, root)
    if not isinstance(schema, Mapping):
        if path:
            out.append(_field(path, {"unsupported_schema": "nonobject schema"}, ""))
        return
    props = schema.get("properties")
    if isinstance(props, Mapping):
        for name, spec in props.items():
            if isinstance(name, str):
                _walk_schema(spec, path + (name,), out, root)
    elif path:
        out.append(_field(path, schema, schema.get("description", "") if isinstance(schema.get("description"), str) else ""))


def _paths(items: list, prefix: Tuple[str, ...] = ()) -> List[Dict[str, Any]]:
    result = []
    for item in items:
        if isinstance(item, str) and item.strip():
            parts = [part for part in re.split(r"[./]", item.strip().strip("/")) if part]
            if parts:
                result.append(_field(prefix + tuple(parts), {"description": item}))
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            spec = item
            child = _field(prefix + (item["name"],), item, str(item.get("description", "")))
            if isinstance(item.get("properties"), Mapping):
                _walk_schema(item, prefix + (item["name"],), result, item)
            else:
                result.append(child)
    return result


def discover_fields(contract: Any) -> Tuple[List[Dict[str, Any]], bool]:
    original = contract if isinstance(contract, Mapping) else None
    contract = _unwrap_schema(contract)
    fields: List[Dict[str, Any]] = []
    strict = bool(original and original.get("additionalProperties") is False)
    if isinstance(contract, Mapping) and ("properties" in contract or "$ref" in contract or any(key in contract for key in ("allOf", "anyOf", "oneOf"))):
        strict = strict or ("properties" in contract)
        _walk_schema(contract, (), fields, contract)
    else:
        source = contract
        if isinstance(contract, Mapping):
            for key in FIELD_LIST_KEYS + WRAPPERS:
                candidate = contract.get(key)
                if isinstance(candidate, (list, Mapping)):
                    source = candidate
                    break
        if isinstance(source, Mapping) and "properties" in source:
            strict = True
            _walk_schema(source, (), fields, source)
        elif isinstance(source, list):
            fields = _paths(source)
        elif isinstance(source, Mapping):
            for name, spec in source.items():
                if name in META_KEYS or not isinstance(name, str):
                    continue
                if isinstance(spec, Mapping) and (set(spec) & {"type", "properties", "items", "$ref", "enum", "const"}):
                    _walk_schema(spec, (name,), fields, source)
                    strict = strict or "properties" in spec
                elif isinstance(spec, Mapping):
                    nested, nested_strict = discover_fields(spec)
                    for item in nested:
                        fields.append(_field((name,) + tuple(item["path"]), item["spec"], item["description"]))
                    strict = strict or nested_strict
                else:
                    fields.append(_field((name,), {"description": str(spec)}))
    unique = []
    seen = set()
    for field in fields:
        key = tuple(field["path"])
        if key and key not in seen:
            seen.add(key)
            unique.append(field)
    return unique, strict


def _default(field: Mapping[str, Any]) -> Any:
    spec = field.get("spec", {})
    if isinstance(spec, Mapping) and "default" in spec:
        return copy.deepcopy(spec["default"])
    if isinstance(spec, Mapping) and "const" in spec:
        return copy.deepcopy(spec["const"])
    kind = _types(spec)
    if isinstance(spec, Mapping) and isinstance(spec.get("properties"), Mapping):
        kind.append("object")
    if "object" in kind:
        return {}
    if "array" in kind:
        return []
    if "boolean" in kind:
        return False
    return None


def deep_set(root: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    if not path:
        raise ValueError("empty result field path")
    node = root
    for part in path[:-1]:
        if not isinstance(node.get(part), Mapping):
            node[part] = {}
        node = node[part]
    node[path[-1]] = copy.deepcopy(value)


def initial_result(case: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], bool]:
    fields, strict = discover_fields(case.get("output_contract"))
    if not fields:
        fields = [_field((name,)) for name in DEFAULT_FIELDS]
    result: Dict[str, Any] = {}
    for field in fields:
        deep_set(result, field["path"], _default(field))
    result["case_id"] = copy.deepcopy(case.get("case_id"))
    return result, fields, strict


def empty_audit(case: Mapping[str, Any]) -> Dict[str, Any]:
    return {"schema_version": "evidence-compiler/1.1", "case_id": copy.deepcopy(case.get("case_id")),
            "run": {"status": "partial", "stage": "bootstrap", "complete": False},
            "experiment_design": {"objective": [], "hypotheses": [], "variables": [], "experimental_units": [],
                                  "controls": [], "steps": [], "parameters": [], "resources": [],
                                  "time_cost_constraints": [], "decision_rules": [], "completion_criteria": []},
            "trends": [], "anomalies": [], "possible_causes": [], "rechecks": [], "followup_experiments": [],
            "sources": [], "basis_and_risk": {"basis": [], "risks": [], "exceptions": []}, "confirmation_items": [],
            "uncertainty": [], "issues": [], "coverage": {}, "change_records": [],
            "validation": {"valid": False, "errors": [], "warnings": []}, "facts": []}


def merge(target: Any, patch: Any) -> Any:
    if isinstance(target, Mapping) and isinstance(patch, Mapping):
        result = copy.deepcopy(dict(target))
        for key, value in patch.items():
            result[key] = merge(result[key], value) if key in result else copy.deepcopy(value)
        return result
    return copy.deepcopy(patch)


def _append_unique(current: Any, patch: Any, kind: str) -> list:
    existing = list(current) if isinstance(current, list) else []
    additions = patch if isinstance(patch, list) else [patch]
    keys = set()
    combined = []
    for item in existing + [item for item in additions if item is not None]:
        if kind == "source" and isinstance(item, Mapping):
            identity = (item.get("path"), item.get("sha256"), item.get("pdf_page_1based"))
        elif kind == "confirmation" and isinstance(item, Mapping):
            identity = (item.get("id"), item.get("kind"), item.get("action"), item.get("owner_role"))
        elif kind == "issue" and isinstance(item, Mapping):
            identity = (item.get("kind"), item.get("stage"), item.get("detail"), item.get("path"))
        else:
            identity = None
        encoded = identity if identity is not None else json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        frozen = repr(encoded)
        if frozen not in keys:
            keys.add(frozen)
            combined.append(copy.deepcopy(item))
    return combined


def merge_audit(target: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    normal = {key: value for key, value in patch.items() if key not in ("sources", "issues", "confirmation_items", "uncertainty", "change_records")}
    result = merge(target, normal)
    for key, kind in (("sources", "source"), ("issues", "issue"), ("confirmation_items", "confirmation"), ("uncertainty", "other"), ("change_records", "other")):
        if key in patch:
            result[key] = _append_unique(target.get(key, []), patch[key], kind)
    return result


ALIASES = {
    "design": "experiment_design", "experimental_design": "experiment_design", "scheme": "experiment_design",
    "hypothesis": "hypotheses", "variable": "variables", "experimental_unit": "experimental_units", "control": "controls",
    "step": "steps", "parameter": "parameters", "resource": "resources", "decision_rule": "decision_rules", "time_cost": "time_cost_constraints",
    "trend": "trends", "anomaly": "anomalies", "outliers": "anomalies", "outlier": "anomalies",
    "causes": "possible_causes", "cause": "possible_causes", "candidate_causes": "possible_causes",
    "recheck": "rechecks", "verification_steps": "rechecks", "reproduction_steps": "rechecks",
    "followup": "followup_experiments", "future_experiments": "followup_experiments", "next_experiments": "followup_experiments",
    "evidence_risk": "basis_and_risk", "evidence_and_risk": "basis_and_risk", "basis": "basis_and_risk",
    "confirmation": "confirmation_items", "confirmations": "confirmation_items", "confirmation_required": "confirmation_items",
    "uncertainties": "uncertainty", "source": "sources", "change_log": "change_records", "changes": "change_records",
    "limitations": "uncertainty", "main_result": "results", "result": "results", "analysis": "results",
    "result_text": "results", "summary": "results", "narrative": "results",
}
INTERPRETATION_KEYS = {"experiment_design", "trends", "anomalies", "possible_causes", "rechecks", "followup_experiments", "uncertainty", "issues", "coverage", "results", "facts", "basis_and_risk", "confirmation_items"}


def _parse_pointer(key: Any) -> Tuple[str, ...]:
    if isinstance(key, list):
        return tuple(str(part) for part in key)
    text = str(key)
    if text.startswith("/"):
        return tuple(part.replace("~1", "/").replace("~0", "~") for part in text.split("/")[1:])
    return tuple(part for part in (text.split(".") if "." in text else text.split("/")) if part)


def _flatten_fields(value: Any, prefix: Tuple[str, ...], out: Dict[Tuple[str, ...], Any]) -> None:
    if isinstance(value, Mapping) and "value" in value and ("path" in value or "pointer" in value or "name" in value):
        key = value.get("pointer", value.get("path", value.get("name")))
        out[prefix + _parse_pointer(key)] = value["value"]
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _flatten_fields(item, prefix + (str(key),), out)
    else:
        out[prefix] = copy.deepcopy(value)


def canonical_patch(patch: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[Tuple[str, ...], Any]]:
    audit: Dict[str, Any] = {}
    for key in ("audit", "skill_audit"):
        if isinstance(patch.get(key), Mapping):
            audit = merge_audit(audit, patch[key])
    explicit: Dict[Tuple[str, ...], Any] = {}
    fields = patch.get("fields", {})
    if isinstance(fields, list):
        for item in fields:
            _flatten_fields(item, (), explicit)
    else:
        _flatten_fields(fields, (), explicit)
    for key, value in patch.items():
        if key in ("fields", "audit", "skill_audit", "case_id", "status"):
            continue
        target = ALIASES.get(key, key)
        if target in INTERPRETATION_KEYS:
            audit[target] = merge(audit.get(target, {}), value)
        elif target in ("sources", "change_records"):
            audit[target] = _append_unique([], value, "source" if target == "sources" else "other")
    if isinstance(patch.get("status"), str):
        audit.setdefault("run", {})["status"] = patch["status"]
    return audit, explicit


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())


def _section_for(field: Mapping[str, Any], audit: Mapping[str, Any]) -> Any:
    name = str(field["path"][-1])
    norm = _norm(name + " " + str(field.get("description", "")))
    checks = [
        ("trends", ("trend", "走势", "趋势", "拐点", "平台")), ("anomalies", ("anomal", "outlier", "异常", "离群", "偏离")),
        ("possible_causes", ("cause", "原因", "机理", "机制", "hypothesesranked")),
        ("rechecks", ("recheck", "reproduc", "复核", "复算", "核查", "traceability")),
        ("followup_experiments", ("followup", "nextexperiment", "后续实验", "futureexperiment", "后续建议")),
        ("sources", ("source", "evidence", "证据", "来源", "reference", "provenance")),
        ("uncertainty", ("uncertainty", "limitation", "局限", "不确定性", "无法判断")),
        ("change_records", ("change", "变更", "修改记录", "changelog")),
        ("confirmation_items", ("confirmation", "确认", "approval", "safety", "安全", "伦理")),
        ("experiment_design", ("design", "scheme", "方案", "hypothes", "variable", "control", "假设", "变量", "对照", "step", "procedure", "步骤", "parameter", "resource", "timecost", "判读", "completion")),
        ("basis_and_risk", ("basis", "risk", "依据", "风险")), ("run", ("status", "状态", "running")),
        ("results", ("summary", "mainresult", "resulttext", "result", "主结果", "结论", "ing")),
        ("coverage", ("coverage", "覆盖", "completeness")),
    ]
    if name == "case_id":
        return audit.get("case_id")
    parent = _norm(" ".join(field["path"][:-1]))
    for key, words in checks:
        if any(word in norm for word in words) or _norm(name) == key:
            value = audit.get(key)
            if key == "run":
                return audit.get("run", {}).get("status", "partial") if name in ("status", "run_status", "state", "status_summary") else value
            if key == "results" and isinstance(value, str) and value:
                return value
            if key in ("experiment_design", "basis_and_risk"):
                singular = ALIASES.get(name)
                if isinstance(value, Mapping) and singular in value:
                    return value[singular]
                match = next((k for k in value if _norm(k) == _norm(name)), None) if isinstance(value, Mapping) else None
                return value[match] if match else value
            return value if value is not None else []
    for key, words in checks:
        if any(word in parent for word in words) or (key == "experiment_design" and _norm(parent) in ("design", "experimentaldesign", "scheme")):
            parent_value = audit.get(key)
            if isinstance(parent_value, Mapping):
                match = next((k for k in parent_value if _norm(k) == _norm(name)), None)
                if match:
                    return parent_value[match]
            return parent_value.get(name, parent_value) if isinstance(parent_value, Mapping) else parent_value
    if len(field["path"]) == 1:
        return {"summary": audit.get("results", ""), "trends": audit.get("trends", []), "anomalies": audit.get("anomalies", []),
                "possible_causes": audit.get("possible_causes", []), "rechecks": audit.get("rechecks", []),
                "followup_experiments": audit.get("followup_experiments", []), "experiment_design": audit.get("experiment_design", {}),
                "uncertainty": audit.get("uncertainty", [])}
    return audit.get("uncertainty", ["该结果字段未从实际证据映射；需按原任务复核。"])


def project_value(field: Mapping[str, Any], value: Any) -> Any:
    kind = _types(field.get("spec", {}))
    if "string" in kind:
        return value if isinstance(value, (str, type(None))) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    if "array" in kind and not isinstance(value, list):
        return [value] if value not in (None, "") else []
    if "object" in kind and not isinstance(value, Mapping):
        return {"value": value} if value is not None else {}
    if "boolean" in kind:
        if value in (True, False):
            return value
        return str(value).lower() in ("complete", "completed", "true", "pass", "passed", "成功", "完成")
    if "number" in kind or "integer" in kind:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value) if "integer" in kind else value
        return None
    return copy.deepcopy(value)


def assemble(case: Mapping[str, Any], patch: Mapping[str, Any], audit: Mapping[str, Any]) -> Dict[str, Any]:
    if "case_id" in patch and patch["case_id"] != case.get("case_id"):
        raise ValueError("reasoning patch changed case_id")
    result, fields, strict = initial_result(case)
    audit_patch, explicit_patch = canonical_patch(patch)
    complete_audit = merge_audit(audit, audit_patch)
    declared = {tuple(field["path"]) for field in fields}
    consumed = set()
    raw_fields = patch.get("fields", {}) if isinstance(patch.get("fields", {}), Mapping) else {}

    def nested_patch(path):
        node = raw_fields
        matched = []
        for part in path:
            if not isinstance(node, Mapping) or part not in node:
                break
            matched.append(part)
            node = node[part]
        if matched:
            return copy.deepcopy(node), tuple(matched)
        candidates = [(key, value) for key, value in raw_fields.items() if key == path[-1]]
        if len(candidates) > 1 and len(path) > 1:
            raise ValueError("ambiguous leaf patch key: " + path[-1])
        if len(candidates) == 1:
            key, value = candidates[0]
            matches = [candidate for candidate in declared if candidate[-1] == key]
            if len(matches) == 1 and matches[0] == path:
                return copy.deepcopy(value), (key,)
        return None, None

    for field in fields:
        path = tuple(field["path"])
        if path in explicit_patch:
            value = explicit_patch[path]
            consumed.add(path)
        else:
            value, matched_path = nested_patch(path)
            if matched_path is None:
                value = _section_for(field, complete_audit)
            else:
                consumed.update(key for key in explicit_patch if key[:len(matched_path)] == matched_path)
        deep_set(result, path, project_value(field, value))
    for key in explicit_patch:
        if key not in declared and key not in consumed:
            if len(key) == 1 and sum(1 for path in declared if path[-1] == key[0]) == 1:
                continue
            raise ValueError("field is not in original output_contract: " + "/".join(key))
    result["case_id"] = copy.deepcopy(case.get("case_id"))
    if not strict:
        result["skill_audit"] = copy.deepcopy(complete_audit)
    return result


def _unsupported_schema(value: Any, path="") -> List[str]:
    if isinstance(value, Mapping):
        if isinstance(value.get("$ref"), str) and not value["$ref"].startswith("#/"):
            return ["unsupported external $ref at " + (path or "/")]
        return [message for key, item in value.items() for message in _unsupported_schema(item, path + "/" + str(key))]
    if isinstance(value, list):
        return [message for index, item in enumerate(value) for message in _unsupported_schema(item, path + "/" + str(index))]
    return []


def _schema_errors(value: Any, schema: Any, path: str, root: Mapping[str, Any], in_all_of=False) -> List[str]:
    schema = _resolve_ref(schema, root)
    if schema is True or schema is None:
        return []
    if schema is False:
        return [path + " disallowed by false schema"]
    if not isinstance(schema, Mapping):
        return [path + " unsupported schema value"]
    if schema.get("unsupported_schema"):
        return [path + " " + str(schema["unsupported_schema"])]
    if "allOf" in schema:
        return [message for item in schema["allOf"] for message in _schema_errors(value, item, path, root, True)]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branches = schema[key]
            matches = sum(not _schema_errors(value, item, path, root) for item in branches if isinstance(item, Mapping))
            if (key == "anyOf" and matches < 1) or (key == "oneOf" and matches != 1):
                return [path + " fails " + key]
            return []
    if "const" in schema and value != schema["const"]:
        return [path + " does not equal const"]
    if "enum" in schema and value not in schema["enum"]:
        return [path + " is not in enum"]
    types = _types(schema)
    if not types and isinstance(schema.get("properties"), Mapping):
        types = ["object"]
    matches_type = False
    for kind in types:
        if ((kind == "object" and isinstance(value, Mapping)) or (kind == "array" and isinstance(value, list)) or
            (kind == "string" and isinstance(value, str)) or (kind == "boolean" and isinstance(value, bool)) or
            (kind == "null" and value is None) or (kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool) and (value == value)) or
            (kind == "integer" and isinstance(value, int) and not isinstance(value, bool))):
            matches_type = True
            break
    if types and not matches_type:
        return [path + " expects type " + ", ".join(types)]
    errors = []
    props = schema.get("properties")
    if isinstance(value, Mapping):
        for required in schema.get("required", []) if isinstance(schema.get("required"), list) else []:
            if required not in value:
                errors.append(path + "/" + str(required) + " required property missing")
        if isinstance(props, Mapping):
            for name, item in value.items():
                if name in props:
                    errors.extend(_schema_errors(item, props[name], path + "/" + str(name), root))
                elif schema.get("additionalProperties") is False and not (path == "" and name == "case_id"):
                    errors.append(path + "/" + str(name) + " additional property is forbidden")
                elif isinstance(schema.get("additionalProperties"), Mapping):
                    errors.extend(_schema_errors(item, schema["additionalProperties"], path + "/" + str(name), root))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(path + " has fewer than minItems")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(path + " exceeds maxItems")
        if isinstance(schema.get("items"), Mapping):
            for index, item in enumerate(value):
                errors.extend(_schema_errors(item, schema["items"], path + "/" + str(index), root))
    if isinstance(value, str) and isinstance(schema.get("pattern"), str):
        if re.search(schema["pattern"], value) is None:
            errors.append(path + " does not match pattern")
    return errors


def validate_result(result: Mapping[str, Any], case: Mapping[str, Any], audit: Any = None) -> Dict[str, Any]:
    errors: List[str] = []
    warnings: List[str] = []
    if result.get("case_id") != case.get("case_id"):
        errors.append("case_id does not exactly match input")
    contract = case.get("output_contract")
    errors.extend(_unsupported_schema(contract))
    fields, _ = discover_fields(contract)
    for field in fields:
        node: Any = result
        for part in field["path"]:
            if not isinstance(node, Mapping) or part not in node:
                errors.append("missing original output field: " + "/".join(field["path"]))
                break
            node = node[part]
        else:
            if node is None:
                errors.append("unpopulated original output field: " + "/".join(field["path"]))
            schema = field.get("spec", {})
            if schema.get("unsupported_schema"):
                errors.append("/".join(field["path"]) + " " + schema["unsupported_schema"])
    schema = _unwrap_schema(contract)
    if isinstance(schema, Mapping) and ("properties" in schema or "$ref" in schema):
        errors.extend(_schema_errors(result, schema, "", schema))
    elif isinstance(schema, Mapping) and any(key in schema for key in ("allOf", "anyOf", "oneOf")):
        if not fields:
            errors.append("cannot_determine: root schema combinator does not expose a supported complete output shape; preserve an explicit partial result")
        else:
            errors.extend(_schema_errors(result, schema, "", schema))
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        errors.append("invalid or non-finite JSON: " + str(exc))
    if not isinstance(audit, Mapping):
        audit = result.get("skill_audit")
    if isinstance(audit, Mapping):
        fact_ids = {item.get("id") for item in audit.get("facts", []) if isinstance(item, Mapping)}
        known = fact_ids | {item.get("id") for key in ("outlier_candidates", "trend_candidates", "sources") for item in audit.get(key, []) if isinstance(item, Mapping)}
        for section in ("trends", "anomalies", "possible_causes", "rechecks", "followup_experiments"):
            findings = audit.get(section)
            if not findings:
                if audit.get("run", {}).get("status") == "complete":
                    errors.append("complete status with empty interpretation section: " + section)
                else:
                    warnings.append("unresolved interpretation section: " + section)
            if isinstance(findings, list):
                for finding in findings:
                    if not isinstance(finding, Mapping):
                        continue
                    if "Replace with" in str(finding.get("statement", "")):
                        errors.append("unfilled reasoning template in " + section)
                    if section == "possible_causes":
                        refs = finding.get("supporting_fact_ids", [])
                        if not isinstance(refs, list) or not finding.get("discriminating_action"):
                            warnings.append("cause lacks supporting evidence list or a discriminating action: " + str(finding.get("candidate", finding.get("id"))))
                    else:
                        refs = finding.get("evidence", finding.get("fact_ids", finding.get("numerical_result_id", [])))
                        if not refs and section != "followup_experiments":
                            warnings.append("finding lacks source/fact reference: " + str(finding.get("id", finding.get("statement"))))
                    for ref in refs if isinstance(refs, list) else [refs]:
                        if isinstance(ref, str) and re.fullmatch(r"(?:F|E|T|M|C)\d{2,6}", ref) and known and ref not in known:
                            errors.append("unregistered evidence reference: " + ref)
        if audit.get("issues"):
            warnings.append("issues retained; inspect issue categories before using conclusions")
        if not audit.get("sources"):
            warnings.append("source manifest is empty")
    return {"valid": not errors, "errors": list(dict.fromkeys(errors)), "warnings": sorted(set(warnings))}


def render_markdown(result: Mapping[str, Any], audit: Mapping[str, Any]) -> str:
    lines = ["# Experimental result interpretation", "", "case_id: `" + str(result.get("case_id")) + "`", "",
             "Run status: **" + str(audit.get("run", {}).get("status", "partial")) + "**", ""]
    for key, title in (("experiment_design", "Experiment design"), ("trends", "1. Trends"), ("anomalies", "2. Anomalies and deviations"),
                       ("possible_causes", "3. Competing explanations"), ("rechecks", "4. Recheck procedure"),
                       ("followup_experiments", "5. Proposed follow-up experiments"), ("uncertainty", "Uncertainty and unresolved boundaries"),
                       ("confirmation_items", "Human confirmation gates")):
        lines += ["## " + title, ""]
        value = audit.get(key)
        if not value:
            lines += ["Unresolved: actual input does not support a defensible conclusion here; see issues."]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    lines.append("- **" + str(item.get("statement", item.get("candidate", item.get("name", item.get("id", "Finding"))))) + "**")
                    for subkey, subvalue in item.items():
                        if subkey not in ("statement", "candidate", "name") and subvalue not in (None, "", [], {}):
                            lines.append("  - `" + str(subkey) + "`: " + str(subvalue))
                else:
                    lines.append("- " + str(item))
        elif isinstance(value, Mapping):
            lines.extend("- `" + str(subkey) + "`: " + str(subvalue) for subkey, subvalue in value.items())
        else:
            lines.append(str(value))
        lines.append("")
    lines += ["## Technical output contract", "", "```json", json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), "```", ""]
    return "\n".join(lines)
