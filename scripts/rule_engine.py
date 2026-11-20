"""Declarative, evidence-linked experimental-unit and QC calculations (stdlib only)."""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext


_MISSING = {"", "na", "n/a", "nan", "none", "null", "missing", "."}
_NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?$")
_CELL = re.compile(r"^(?P<prefix>.*!)(?P<column>[A-Z]+)(?P<row>\d+)$")
_ROW_RANGE = re.compile(r"^(?P<prefix>.*!)(?P<first>[A-Z]+)(?P<row>\d+):(?P<last>[A-Z]+)(?P<last_row>\d+)$")
_MAX_ROWS = 250000
_FLOAT_TOLERANCE = 1e-12
_OPERATIONS = {
    "paired_replicate_range",
    "derived_ratio_audit",
    "group_summary_nested_treatment_contrast",
}


def _norm(value):
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _token(value):
    return re.sub(r"[^a-z0-9]+", "_", _norm(value)).strip("_")


def _missing(value):
    return value is None or isinstance(value, str) and value.strip().casefold() in _MISSING


def _decimal(value):
    if _missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not _NUMBER.fullmatch(text):
            return None
    elif not isinstance(value, (int, float, Decimal)):
        return None
    try:
        result = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError, OverflowError):
        return None
    return result if result.is_finite() else None


def _integer(value, nonnegative=False):
    number = _decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    if nonnegative and number < 0:
        return None
    return number


def _float(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return _float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_safe(item) for item in sorted(value, key=lambda item: str(item))]
    if hasattr(value, "item") and callable(value.item):
        try:
            return _safe(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _json_key(value):
    return json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _column_letter(index):
    number = index + 1
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _column_name(column, index):
    if isinstance(column, dict):
        for key in ("name", "column", "header", "id"):
            if column.get(key) not in (None, ""):
                return str(column[key])
    return "" if column is None else str(column)


def _raw_value(row, column_index, column_name=None):
    if isinstance(row, dict):
        if column_name in row:
            return row[column_name]
        if str(column_index) in row:
            return row[str(column_index)]
        if column_name is not None:
            matches = [value for key, value in row.items() if _norm(key) == _norm(column_name)]
            if len(matches) == 1:
                return matches[0]
        return None
    if isinstance(row, (list, tuple)):
        return row[column_index] if column_index < len(row) else None
    return row if column_index == 0 else None


def _header_unit(column):
    if isinstance(column, dict):
        for key in ("unit", "units"):
            if column.get(key) not in (None, ""):
                return str(column[key]).strip()
    text = _column_name(column, 0).strip()
    for pattern in (r"\[\s*([^[\]]{1,60})\s*\]\s*$", r"\(\s*([^()]{1,60})\s*\)\s*$"):
        match = re.search(pattern, text)
        if match:
            unit = match.group(1).strip()
            if unit and not re.fullmatch(r"\d+|n\s*=.*", unit, re.I):
                return unit
    return None


def _unit_norm(unit):
    if unit is None:
        return None
    text = str(unit).strip().casefold().replace("μ", "µ")
    text = re.sub(r"\s+", "", text)
    if text in {"1", "ratio", "dimensionless", "fraction", "proportion"}:
        return "1"
    return text


def _metric_unit(column, declared=None):
    native = _header_unit(column)
    if declared not in (None, "") and native is not None:
        if _unit_norm(declared) != _unit_norm(native):
            return None, {"declared_unit": str(declared), "native_header_unit": native}
    if declared not in (None, ""):
        return str(declared).strip(), None
    return native, None


def _mean(values):
    if not values:
        return None
    with localcontext() as context:
        context.prec = 50
        return sum(values, Decimal(0)) / Decimal(len(values))


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal(2)


def _same_category(left, right):
    left_number, right_number = _decimal(left), _decimal(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return _norm(left) == _norm(right)


def _category_key(value):
    number = _decimal(value)
    if number is not None:
        return "number:" + str(number.normalize())
    if value is None:
        return "null"
    return type(value).__name__ + ":" + _norm(value)


def _source_identity(table):
    return {
        "table_id": table.get("id"),
        "path": table.get("path"),
        "sheet": table.get("sheet"),
    }


def _row_ref(view, local_index):
    global_index = view["row_indices"][local_index]
    refs = view["original"].get("row_refs") or []
    if isinstance(refs, dict):
        ref = refs.get(str(global_index), refs.get(global_index))
    elif global_index < len(refs):
        ref = refs[global_index]
    else:
        ref = None
    if isinstance(ref, dict):
        ref = next((ref[key] for key in ("ref", "locator", "address", "range", "row") if ref.get(key) is not None), None)
    if ref is not None:
        return str(ref)
    header_row = view.get("header_row")
    offset = local_index + 1
    if isinstance(header_row, int):
        return str(header_row + offset)
    return str(global_index + 1)


def _location(view, ref):
    table = view["original"]
    path = str(table.get("path") or "<unknown-path>")
    sheet = table.get("sheet")
    ref = str(ref)
    if ref.startswith(path):
        return ref
    if sheet not in (None, "") and ref.startswith(str(sheet) + "!"):
        return path + "#" + ref
    return path + ("#" + str(sheet) if sheet not in (None, "") else "") + " | " + ref


def _row_location(view, local_index):
    return _location(view, _row_ref(view, local_index))


def _cell_location(view, local_index, column_index):
    ref = _row_ref(view, local_index)
    match = _ROW_RANGE.fullmatch(ref)
    if match and match.group("row") == match.group("last_row"):
        first = match.group("first")
        first_number = 0
        for character in first:
            first_number = first_number * 26 + ord(character) - 64
        column = _column_letter(first_number - 1 + column_index)
        return _location(view, match.group("prefix") + column + match.group("row"))
    return _row_location(view, local_index) + " | column=" + _column_letter(column_index)


def _compress_cells(cells):
    grouped = defaultdict(list)
    other = set()
    for cell in sorted(set(cells)):
        match = _CELL.fullmatch(cell)
        if match:
            grouped[(match.group("prefix"), match.group("column"))].append(int(match.group("row")))
        else:
            other.add(cell)
    result = []
    for (prefix, column), rows in sorted(grouped.items()):
        start = previous = rows[0]
        for row in rows[1:] + [None]:
            if row is not None and row == previous + 1:
                previous = row
                continue
            result.append(prefix + column + str(start) + (":" + str(previous) if previous != start else ""))
            if row is not None:
                start = previous = row
    result.extend(sorted(other))
    return result


def _header_location(view):
    table = view["original"]
    path = str(table.get("path") or "<unknown-path>")
    sheet = table.get("sheet")
    base = path + ("#" + str(sheet) if sheet not in (None, "") else "")
    if view.get("header_global_index") is not None:
        original = dict(view)
        original["row_indices"] = [view["header_global_index"]]
        return base + " | header=" + _row_ref(original, 0)
    return base + " | header_row=" + str(view.get("header_row"))


class _Engine:
    def __init__(self, data):
        self.data = data
        self.facts = []
        self.issues = []
        self.methods = []
        self.rule_coverage = []
        self.rule = None
        self.rule_index = None
        self.operation = None

    def column_index(self, view, name):
        return view["column_map"][_norm(name)]

    def fact(self, kind, metric, entity, value, unit, method, evidence, uncertainty):
        item = {
            "kind": kind,
            "metric": str(metric),
            "entity": _safe(entity),
            "value": _safe(value),
            "unit": str(unit) if unit not in (None, "") else None,
            "method": str(method),
            "evidence": _safe(list(dict.fromkeys(str(item) for item in evidence if item not in (None, "")))),
            "uncertainty": str(uncertainty),
        }
        self.facts.append(item)
        return item

    def issue(self, kind, stage, detail, evidence=(), **extra):
        item = {
            "kind": str(kind),
            "stage": str(stage),
            "detail": str(detail),
            "rule_index": self.rule_index,
            "operation": self.operation,
            "evidence": _safe(list(dict.fromkeys(str(item) for item in evidence if item not in (None, "")))),
        }
        item.update(_safe(extra))
        self.issues.append(item)
        return item

    def select_tables(self):
        tables = [table for table in self.data.get("tables") or [] if isinstance(table, dict)]
        selector = self.rule.get("sheet_selector", self.rule.get("table_selector"))
        table_id = self.rule.get("table_id")
        candidates = []
        for position, table in enumerate(tables):
            if table_id is not None and str(table.get("id")) != str(table_id):
                continue
            candidates.append((position, table))
        if selector is None and table_id is None:
            if len(candidates) != 1:
                self.issue("cannot_determine", "table_selection", "A unique sheet_selector or table_id is required; no source table was guessed.", requested_selectors=1, matched_tables=len(candidates) if len(tables) == 1 else 0)
                return []
        elif selector is not None:
            requested = selector if isinstance(selector, list) else [selector]
            if not requested or any(not isinstance(item, (str, int, float)) or isinstance(item, bool) for item in requested):
                self.issue("invalid_rule", "table_selection", "sheet_selector must be a string or a nonempty list of exact names.")
                return []
            selected = []
            missing = []
            for wanted in requested:
                matches = [(position, table) for position, table in candidates if any(_norm(table.get(key)) == _norm(wanted) for key in ("sheet", "name", "id"))]
                if not matches:
                    missing.append(str(wanted))
                selected.extend(matches)
            if missing:
                self.issue("cannot_determine", "table_selection", "One or more explicitly selected sheets/tables were not found; the rule was not evaluated partially.", missing_selectors=missing)
                return []
            unique = {}
            for position, table in selected:
                unique[position] = table
            candidates = sorted(unique.items())
            if len(candidates) != len(selected):
                self.issue("invalid_rule", "table_selection", "sheet_selector resolved the same table more than once; duplicate analysis was not emitted.", requested_selectors=len(requested), unique_tables=len(candidates))
                return []
        candidates.sort(key=lambda item: (str(item[1].get("path") or ""), _norm(item[1].get("sheet") or item[1].get("name") or ""), str(item[1].get("id") or ""), item[0]))
        oversized = [(position, table) for position, table in candidates if len(table.get("rows") or []) > _MAX_ROWS]
        for position, table in oversized:
            self.issue("cannot_determine", "row_bound", "Selected table exceeds the declared row bound; it was not silently truncated.", [_header_location({"original": table, "row_indices": [], "header_row": (table.get("headers") or {}).get("row")})], rows=len(table.get("rows") or []), row_bound=_MAX_ROWS)
        return [(position, table) for position, table in candidates if (position, table) not in oversized]

    def make_view(self, positioned, required, optional=()):
        position, table = positioned
        original_columns = table.get("columns") or []
        columns = [_column_name(column, index) for index, column in enumerate(original_columns)]
        rows = table.get("rows") or []
        if not columns and rows and isinstance(rows[0], dict):
            columns = list(dict.fromkeys(str(key) for row in rows if isinstance(row, dict) for key in row))
            original_columns = columns
        requested = list(dict.fromkeys([str(name) for name in list(required) + list(optional) if name not in (None, "")]))
        required_norm = {_norm(name) for name in required}

        def candidates(names, wanted):
            target = _norm(wanted)
            return [index for index, name in enumerate(names) if _norm(name) == target]

        usable = bool(columns) and all(candidates(columns, name) for name in required)
        header_global_index = None
        header_row = (table.get("headers") or {}).get("row") if isinstance(table.get("headers"), dict) else None
        if not usable:
            matches = []
            for row_index, row in enumerate(rows):
                candidate_columns = [_column_name(_raw_value(row, index), index) for index in range(len(original_columns) or (len(row) if isinstance(row, (list, tuple)) else 0))]
                if candidate_columns and all(candidates(candidate_columns, name) for name in required):
                    matches.append((row_index, candidate_columns))
            if len(matches) != 1:
                available = {name: candidates(columns, name) for name in requested if candidates(columns, name)}
                self.issue("cannot_determine", "header_contract", "Could not identify one unique native header row containing every required column; no column was guessed.", [_header_location({"original": table, "row_indices": [], "header_row": header_row})], required_columns=list(required), header_row_candidates=[item[0] for item in matches], matching_current_columns=available, required_normalized_names=sorted(required_norm))
                return None
            header_global_index, columns = matches[0]
            rows = rows[header_global_index + 1:]
            row_indices = list(range(header_global_index + 1, len(table.get("rows") or [])))
            header_ref = (table.get("row_refs") or [])[header_global_index] if header_global_index < len(table.get("row_refs") or []) else None
            native_match = re.search(r"(\d+)(?::[A-Z]+\d+)?$", str(header_ref or ""))
            header_row = int(native_match.group(1)) if native_match else None
            original_columns = columns
        else:
            row_indices = list(range(len(rows)))
        view = {
            "position": position,
            "original": table,
            "columns_original": original_columns,
            "columns": columns,
            "rows": rows,
            "row_indices": row_indices,
            "header_global_index": header_global_index,
            "header_row": header_row,
            "column_map": {},
            "duplicate_columns": [],
        }
        explicit = self.rule.get("column_indices", self.rule.get("column_disambiguation", {}))
        if not isinstance(explicit, dict):
            explicit = {}
        native_mapping = (table.get("headers") or {}).get("mapping", {}) if isinstance(table.get("headers"), dict) else {}
        warned = set()
        for name in requested:
            target = _norm(name)
            indices = candidates(columns, name)
            chosen = None
            basis = "unique_exact_normalized_name"
            if indices and target not in required_norm:
                chosen = indices[0]
            elif len(indices) == 1:
                chosen = indices[0]
            elif len(indices) > 1:
                hinted = next((explicit[key] for key in explicit if _norm(key) == target), None)
                hinted_number = _integer(hinted)
                if hinted_number is not None and int(hinted_number) in indices:
                    chosen = int(hinted_number)
                    basis = "rule_declared_zero_based_column_index"
                else:
                    native_hint = next((native_mapping[key] for key in native_mapping if _norm(key) == target), None)
                    if isinstance(native_hint, int) and native_hint in indices:
                        chosen = native_hint
                        basis = "reader_disambiguated_header_metadata"
                    else:
                        chosen = indices[0]
                        basis = "first_natural_column_zero_based_index"
                if target not in warned:
                    self.issue("ambiguous_column_disambiguated", "column_contract", "Duplicate normalized header names were resolved by %s; no duplicate metric was silently dropped or merged." % basis, [_header_location(view)], requested_column=name, candidate_zero_based_indices=indices, selected_zero_based_index=chosen, selection_basis=basis)
                    warned.add(target)
            if chosen is not None:
                view["column_map"][target] = chosen
                if len(indices) > 1:
                    view["duplicate_columns"].append({"requested": name, "candidates": indices, "selected": chosen, "basis": basis})
        for name in required:
            if _norm(name) not in view["column_map"]:
                self.issue("cannot_determine", "column_contract", "A required configured column is unavailable after header resolution; the source table was not analyzed by substitution.", [_header_location(view)], missing_column=str(name), available_columns=columns)
                return None
        return view

    def finish_rule(self, status, table_coverage, selections, extra=None):
        coverage = {
            "rule_index": self.rule_index,
            "operation": self.operation,
            "status": status,
            "input_tables_selected": len(table_coverage),
            "nrows": sum(int(item.get("nrows", 0)) for item in table_coverage),
            "selections": _safe(selections),
            "tables": table_coverage,
        }
        if extra:
            coverage.update(_safe(extra))
        self.rule_coverage.append(coverage)
        self.methods.append({
            "rule_index": self.rule_index,
            "operation": self.operation,
            "status": status,
            "selections": _safe(selections),
            "input_rows": coverage["nrows"],
            "algorithm": {
                "paired_replicate_range": "For each actual selected group, exactly one of each requested replicate is required. Compute requested_second-requested_first and its absolute value; only an explicit source QC threshold with strict > comparison can set quality_violation. Incomplete, duplicate, unexpected, missing-value groups remain raw and unresolved.",
                "derived_ratio_audit": "For every source row preserve raw numerator/denominator/display values and recompute numerator/denominator only for finite values with nonzero denominator. Report recomputed-minus-reported and absolute difference. Rounding/tolerance acceptance is checked only when declared; a 1e-12 scaled comparison is separately labelled floating precision, not inference.",
                "group_summary_nested_treatment_contrast": "Use configured identities only. Average available raw values per actual experimental unit and metric; ratio metrics divide raw numerator and denominator means over the same valid rows. Contrast left-right only for one structurally compatible inner unit per condition at each configured pair unit. Summarize complete metric-specific pairs by arithmetic mean, median and range, then perform whole-level lineage exclusion sensitivity.",
            }.get(self.operation, "No calculation for an unsupported operation."),
            "limitations": "Descriptive recalculation only. Missing is not zero, zero denominator is undefined, duplicate identities are not silently deduplicated, and no p-value, causal attribution, validated biological conclusion or unconfigured acceptance interval is generated.",
        })


def _selection_record(view):
    return {
        **_source_identity(view["original"]),
        "header_row": view.get("header_row"),
        "columns": [
            {"requested": requested, "native_header": view["columns"][index], "zero_based_index": index, "native_column": _column_letter(index)}
            for requested, index in sorted(view["column_map"].items())
        ],
        "duplicate_column_disambiguation": view["duplicate_columns"],
    }


def _row_snapshot(view, local_index):
    row = view["rows"][local_index]
    return {
        "row_ref": _row_ref(view, local_index),
        "source_locator": _row_location(view, local_index),
        "source_row_index_zero_based": view["row_indices"][local_index],
        "raw_row": _safe(row),
    }


def _paired_table(engine, view):
    rule = engine.rule
    group_names = rule.get("group_by")
    if not isinstance(group_names, list) or not group_names or any(not isinstance(name, str) or not name.strip() for name in group_names):
        engine.issue("invalid_rule", "paired_replicate_range", "group_by must be a nonempty list of native column names.")
        return None
    value_name = rule.get("value_column")
    replicate_name = rule.get("replicate_column")
    pair_values = rule.get("pair_values")
    threshold = _decimal(rule.get("threshold"))
    if not isinstance(value_name, str) or not isinstance(replicate_name, str) or not isinstance(pair_values, list) or len(pair_values) != 2:
        engine.issue("invalid_rule", "paired_replicate_range", "value_column, replicate_column and exactly two pair_values are required.")
        return None
    if _category_key(pair_values[0]) == _category_key(pair_values[1]):
        engine.issue("invalid_rule", "paired_replicate_range", "pair_values must identify two distinct requested replicates.")
        return None
    if threshold is None:
        engine.issue("source_qc_basis_ambiguous", "paired_replicate_range", "No finite source threshold was supplied; no fallback threshold or QC flag was invented.", [_header_location(view)])
        return None
    comparator = rule.get("comparison", rule.get("rule"))
    comparator_token = _token(comparator) if comparator not in (None, "") else ""
    if comparator_token and "strictly_greater_than_threshold" not in comparator_token:
        engine.issue("source_qc_basis_ambiguous", "paired_replicate_range", "The configured comparator is not strictly greater than the source threshold; no alternate QC interpretation was guessed.", [_header_location(view)], comparator=_safe(comparator))
        return None
    source = rule.get("source") if isinstance(rule.get("source"), dict) else {}
    source_type = source.get("type", rule.get("source_type"))
    source_basis = source.get("rule_basis", rule.get("source_rule_basis", rule.get("basis")))
    qc_declared = bool(source_type or source_basis) or "source_qc" in comparator_token
    if not qc_declared or not comparator_token and not source_basis:
        engine.issue("source_qc_basis_ambiguous", "paired_replicate_range", "The source QC basis is not explicit in this rule; paired differences were not promoted to a scientific QC rule.", [_header_location(view)], source_type=_safe(source_type), source_rule_basis=_safe(source_basis))
        return None
    unit, unit_conflict = _metric_unit(view["columns_original"][engine.column_index(view, rule.get("value_column"))], rule.get("unit"))
    if unit_conflict:
        engine.issue("unit_conflict", "paired_replicate_range", "Declared and native metric units conflict; the paired values were not combined.", [_header_location(view)], **unit_conflict)
        return None
    group_index = [engine.column_index(view, name) for name in group_names]
    value_index = engine.column_index(view, value_name)
    replicate_index = engine.column_index(view, replicate_name)
    groups = defaultdict(list)
    for local_index, row in enumerate(view["rows"]):
        key = tuple(_json_key(_safe(_raw_value(row, index, view["columns"][index]))) for index in group_index)
        groups[key].append(local_index)
    stats = {"nrows": len(view["rows"]), "ngroups": len(groups), "npairs": 0, "nflag": 0, "completepaircount": 0, "missingOrAmbiguous": 0}
    threshold_float = _float(threshold)
    if threshold_float is None:
        engine.issue("source_qc_basis_ambiguous", "paired_replicate_range", "The source threshold cannot be represented as a finite JSON scalar.", [_header_location(view)])
        return None
    for key in sorted(groups):
        members = sorted(groups[key])
        first = members[0]
        group_values = [_raw_value(view["rows"][first], index, view["columns"][index]) for index in group_index]
        group_object = {name: _safe(value) for name, value in zip(group_names, group_values)}
        records = []
        replicate_counts = Counter()
        missing_identity = False
        for local_index in members:
            row = view["rows"][local_index]
            replicate_raw = _raw_value(row, replicate_index, view["columns"][replicate_index])
            value_raw = _raw_value(row, value_index, view["columns"][value_index])
            replicate_counts[_category_key(replicate_raw)] += 1
            missing_identity = missing_identity or any(_missing(_raw_value(row, index, view["columns"][index])) for index in group_index) or _missing(replicate_raw)
            snapshot = _row_snapshot(view, local_index)
            snapshot.update({
                "replicate": _safe(replicate_raw),
                "value": _safe(value_raw),
                "finite_numeric_value": _float(_decimal(value_raw)),
                "value_cell": _cell_location(view, local_index, value_index),
            })
            records.append(snapshot)
        by_replicate = {token: [] for token in (_category_key(pair_values[0]), _category_key(pair_values[1]))}
        unexpected = []
        for position, local_index in enumerate(members):
            token = _category_key(_raw_value(view["rows"][local_index], replicate_index, view["columns"][replicate_index]))
            if token in by_replicate:
                by_replicate[token].append(local_index)
            else:
                unexpected.append(local_index)
        reasons = []
        if missing_identity:
            reasons.append("missing_group_or_replicate_identity")
        if any(len(by_replicate[_category_key(value)]) != 1 for value in pair_values):
            reasons.append("missing_pair")
        if any(len(by_replicate[_category_key(value)]) > 1 for value in pair_values):
            reasons.append("extra_same_replicate")
        if unexpected:
            reasons.append("unexpected_replicate")
        chosen = [by_replicate[_category_key(value)][0] for value in pair_values] if all(len(by_replicate[_category_key(value)]) == 1 for value in pair_values) else []
        numeric = [_decimal(_raw_value(view["rows"][local_index], value_index, view["columns"][value_index])) for local_index in chosen]
        if chosen and any(value is None for value in numeric):
            reasons.append("missing_or_nonfinite_value")
        entity = {**_source_identity(view["original"]), "group_by": list(group_names), "group": group_object, "group_native_rows": [_row_ref(view, index) for index in members]}
        evidence = [_cell_location(view, local_index, value_index) for local_index in members]
        if reasons:
            reasons = list(dict.fromkeys(reasons))
            stats["missingOrAmbiguous"] += 1
            value = {
                "status": "unresolved",
                "reason_codes": reasons,
                "group_row_count": len(members),
                "requested_pair_values": _safe(pair_values),
                "replicate_counts": [{"replicate_key": token, "count": count} for token, count in sorted(replicate_counts.items())],
                "raw_records": records,
                "disposition": "all_source_rows_retained_no_pairing_averaging_or_deletion",
            }
            engine.fact("unresolved", "paired_replicate_range:" + value_name, entity, value, unit, "Group identity and exactly-one-per-requested-replicate validation before any DeltaCt/value subtraction; raw source rows retained.", evidence, "This group does not yield a valid pair count or QC threshold evaluation; its raw rows do not establish an outlier, p-value, physical cause or independent biological replicate.")
            engine.issue("cannot_determine", "paired_replicate_range_unresolved", "A replicate group is incomplete, ambiguous or nonnumeric; no pair was averaged or silently removed.", evidence, group=group_object, group_row_count=len(members), reason_codes=reasons, row_refs=[_row_ref(view, index) for index in members])
            continue
        first_value, second_value = numeric
        difference = second_value - first_value
        absolute = abs(difference)
        difference_float, absolute_float = _float(difference), _float(absolute)
        flagged = absolute > threshold
        status = "quality_violation" if flagged else "within_source_qc_range"
        stats["npairs"] += 1
        stats["completepaircount"] += 1
        stats["nflag"] += int(flagged)
        source_record = {"type": _safe(source_type), "source_rule_basis": _safe(source_basis)}
        value = {
            "status": status,
            "first_replicate": _safe(pair_values[0]),
            "second_replicate": _safe(pair_values[1]),
            "first_value": _float(first_value),
            "second_value": _float(second_value),
            "second_minus_first": difference_float,
            "absolute_difference": absolute_float,
            "source_difference_definition": rule.get("metric", "paired_value_difference"),
            "threshold": threshold_float,
            "comparison": "absolute_difference_strictly_greater_than_threshold",
            "threshold_source": source_record,
            "raw_records": records,
            "disposition": "retained_in_audit_regardless_of_QC_status",
        }
        method = "Within each actual configured group: second requested replicate minus first, then absolute difference (the source-defined DeltaCt/value difference where so declared). Flag if and only if absolute difference > the finite source threshold; equality is not flagged. No copy-number conversion, independent-biological-n assignment or outlier test."
        evidence = [_cell_location(view, local_index, value_index) for local_index in chosen]
        engine.fact("derived", "paired_replicate_range:" + value_name, entity, value, unit, method, evidence, "quality_violation denotes only the explicitly supplied source-protocol QC threshold and excludes neither raw row. It is not a statistical outlier call, causal diagnosis or validated biological effect.")
        if flagged:
            engine.issue("quality_violation", "paired_replicate_range_source_qc", "The absolute paired difference strictly exceeds the source QC threshold; both raw records remain retained.", evidence, group=group_object, absolute_difference=absolute_float, threshold=threshold_float, threshold_source=source_record, row_refs=[_row_ref(view, index) for index in chosen], disposition="retained")
    return stats


def _ratio_table(engine, view):
    rule = engine.rule
    numerator_name = rule.get("numerator_column", rule.get("numerator"))
    denominator_name = rule.get("denominator_column", rule.get("denominator"))
    reported_name = rule.get("reported_column", rule.get("reported"))
    if not isinstance(numerator_name, str) or not isinstance(denominator_name, str):
        engine.issue("invalid_rule", "derived_ratio_audit", "Explicit numerator_column and denominator_column are required.")
        return None
    numerator_index = engine.column_index(view, numerator_name)
    denominator_index = engine.column_index(view, denominator_name)
    reported_index = engine.column_index(view, reported_name) if isinstance(reported_name, str) and _norm(reported_name) in view["column_map"] else None
    if isinstance(reported_name, str) and reported_index is None:
        engine.issue("cannot_determine", "derived_ratio_audit_reported_column", "The declared reported/displayed ratio column is unavailable; raw ratios are still audited and every row is reported as missing reported input rather than compared against a substitute.", [_header_location(view)], reported_column=reported_name)
    numerator_unit, numerator_conflict = _metric_unit(view["columns_original"][numerator_index])
    denominator_unit, denominator_conflict = _metric_unit(view["columns_original"][denominator_index])
    if numerator_conflict or denominator_conflict or (_unit_norm(numerator_unit) != _unit_norm(denominator_unit) and (numerator_unit is not None or denominator_unit is not None)):
        engine.issue("unit_conflict", "derived_ratio_audit", "Numerator and denominator native units are missing a compatible dimensionless-count interpretation; no unit conversion was guessed.", [_header_location(view)], numerator_unit=numerator_unit, denominator_unit=denominator_unit)
        return None
    unit = "1" if not rule.get("unit") else str(rule.get("unit"))
    if _unit_norm(unit) != "1":
        engine.issue("unit_conflict", "derived_ratio_audit", "This ratio audit requires a declared dimensionless result or omitted unit; a different ratio unit was not inferred.", [_header_location(view)], declared_unit=unit)
        return None
    rounding_digits = rule.get("rounding_digits")
    if rounding_digits is None and isinstance(rule.get("rounding"), dict):
        rounding_digits = rule["rounding"].get("digits")
    digits = _integer(rounding_digits)
    if rounding_digits is not None and (digits is None or digits < 0 or digits > 30):
        engine.issue("invalid_rule", "derived_ratio_audit", "rounding_digits must be an integer from 0 through 30 when declared.")
        return None
    tolerance_decimal = _decimal(rule.get("tolerance"))
    if rule.get("tolerance") is not None and (tolerance_decimal is None or tolerance_decimal < 0):
        engine.issue("invalid_rule", "derived_ratio_audit", "tolerance must be finite and nonnegative when declared.")
        return None
    if tolerance_decimal is not None and _float(tolerance_decimal) is None:
        engine.issue("invalid_rule", "derived_ratio_audit", "tolerance is outside finite JSON numeric range.")
        return None
    stats = Counter()
    stats["nrows"] = len(view["rows"])
    max_difference = None
    max_record = None
    for local_index, row in enumerate(view["rows"]):
        numerator_raw = _raw_value(row, numerator_index, view["columns"][numerator_index])
        denominator_raw = _raw_value(row, denominator_index, view["columns"][denominator_index])
        reported_raw = _raw_value(row, reported_index, view["columns"][reported_index]) if reported_index is not None else None
        numerator = _decimal(numerator_raw)
        denominator = _decimal(denominator_raw)
        reported = _decimal(reported_raw)
        raw = {"numerator": _safe(numerator_raw), "denominator": _safe(denominator_raw), "reported": _safe(reported_raw)}
        integer_numerator, integer_denominator = _integer(numerator_raw, True), _integer(denominator_raw, True)
        integer_counts = integer_numerator is not None and integer_denominator is not None
        if integer_counts:
            stats["integer_count_rows"] += 1
        entity = {**_source_identity(view["original"]), "row_ref": _row_ref(view, local_index), "source_row_index_zero_based": view["row_indices"][local_index], "numerator_column": numerator_name, "denominator_column": denominator_name, "reported_column": reported_name if reported_index is not None else None}
        cells = [_cell_location(view, local_index, numerator_index), _cell_location(view, local_index, denominator_index)]
        if reported_index is not None:
            cells.append(_cell_location(view, local_index, reported_index))
        value = {
            "raw_values": raw,
            "raw_numerator": _safe(numerator_raw),
            "raw_denominator": _safe(denominator_raw),
            "raw_reported": _safe(reported_raw),
            "integer_numerator": int(integer_numerator) if integer_numerator is not None else None,
            "integer_denominator": int(integer_denominator) if integer_denominator is not None else None,
            "inputs_are_nonnegative_integer_counts": integer_counts,
            "raw_row": _safe(row),
            "disposition": "source_row_retained",
        }
        if numerator is None or denominator is None:
            reasons = []
            if numerator is None:
                reasons.append("missing_or_nonfinite_numerator")
            if denominator is None:
                reasons.append("missing_or_nonfinite_denominator")
            stats["missing_or_invalid_inputs"] += 1
            value.update({"status": "missing_or_invalid_ratio_inputs", "reason_codes": reasons, "recomputed_ratio": None})
            engine.fact("unresolved", "derived_ratio_audit:" + numerator_name + "/" + denominator_name, entity, value, unit, "Raw values are preserved and finite-scalar conversion is required before division; no missing/nonfinite component is replaced by zero.", cells, "This row has no defined recomputed ratio; it remains in row coverage and is not silently dropped.")
            continue
        if denominator == 0:
            stats["undefined_zero_denominator"] += 1
            if numerator == 0:
                stats["zero_numerator_zero_denominator"] += 1
            value.update({"status": "undefined_zero_denominator", "missing_reason": "division_by_zero_undefined_not_zero", "recomputed_ratio": None})
            engine.fact("unresolved", "derived_ratio_audit:" + numerator_name + "/" + denominator_name, entity, value, unit, "Finite raw numerator retained while denominator is zero; ratio is undefined and no zero or one is imputed.", cells, "Zero denominator has no defined ratio, including zero numerator over zero denominator. The row remains retained rather than being discarded as an avoidable division error.")
            continue
        with localcontext() as context:
            context.prec = 50
            ratio = numerator / denominator
        ratio_float = _float(ratio)
        stats["ratios_computed"] += 1
        if numerator == 0:
            stats["zero_numerator_defined_ratio"] += 1
        value.update({"recomputed_ratio": ratio_float})
        if reported_index is None or _missing(reported_raw):
            stats["reported_missing"] += 1
            value.update({"status": "recomputed_reported_missing", "reported_ratio": None, "missing_reported_reason": "reported_column_unavailable" if reported_index is None else "missing_or_nonfinite_reported_value", "recomputed_minus_reported": None, "absolute_difference": None, "within_declared_rounding": None, "matches_recomputed_within_float_tolerance": None})
            engine.fact("derived", "derived_ratio_audit:" + numerator_name + "/" + denominator_name, entity, value, unit, "Recomputed raw numerator/nonzero raw denominator. Missing displayed ratio is separately reported and never replaced by zero or a generated precision claim.", cells, "No displayed source ratio is available for comparison; the recomputation remains descriptive and carries no inferential threshold.")
            continue
        if reported is None:
            stats["reported_invalid"] += 1
            value.update({"status": "recomputed_reported_nonnumeric", "reported_ratio": None, "recomputed_minus_reported": None, "absolute_difference": None, "within_declared_rounding": None, "matches_recomputed_within_float_tolerance": None})
            engine.fact("unresolved", "derived_ratio_audit:" + numerator_name + "/" + denominator_name, entity, value, unit, "Recomputed raw numerator/nonzero raw denominator; nonnumeric displayed ratio remains raw and unresolved.", cells, "The displayed value is not a finite scalar and cannot support a numeric agreement statement.")
            continue
        stats["reported_compared"] += 1
        signed = ratio - reported
        absolute = abs(signed)
        signed_float, absolute_float, reported_float = _float(signed), _float(absolute), _float(reported)
        if max_difference is None or absolute > max_difference:
            max_difference = absolute
            max_record = {"row_ref": _row_ref(view, local_index), "absolute_difference": absolute_float, "source_locator": _row_location(view, local_index)}
        float_limit = Decimal(str(_FLOAT_TOLERANCE)) * max(Decimal(1), abs(ratio), abs(reported))
        float_match = absolute <= float_limit
        declared_check = None
        comparison_basis = None
        comparison_limit = None
        if digits is not None:
            comparison_limit = Decimal(1).scaleb(-int(digits)) / Decimal(2)
            comparison_basis = "declared_rounding_digits_half_unit_envelope"
            declared_check = absolute <= comparison_limit + float_limit
        elif tolerance_decimal is not None:
            comparison_limit = tolerance_decimal
            comparison_basis = "declared_absolute_tolerance"
            declared_check = absolute <= comparison_limit + float_limit
        stats["within_declared_comparison"] += int(declared_check is True)
        stats["outside_declared_comparison"] += int(declared_check is False)
        value.update({
            "status": "recomputed_reported_compared",
            "reported_ratio": reported_float,
            "recomputed_minus_reported": signed_float,
            "absolute_difference": absolute_float,
            "within_declared_rounding": declared_check,
            "declared_rounding_digits": int(digits) if digits is not None else None,
            "declared_tolerance": _float(tolerance_decimal),
            "declared_comparison_basis": comparison_basis,
            "declared_comparison_limit": _float(comparison_limit),
            "matches_recomputed_within_float_tolerance": float_match,
            "floating_comparison_relative_tolerance": _FLOAT_TOLERANCE,
            "exact_decimal_recomputed_equals_reported": ratio == reported,
        })
        method = "Recompute numerator/nonzero denominator from raw finite scalars with Decimal arithmetic; retain all raw digits in raw_values and exact integer counts where present. Difference is recomputed-reported. within_declared_rounding is null unless rounding_digits or tolerance is explicitly declared; the separate scaled 1e-12 status addresses floating comparison only, not scientific acceptance or a p-value."
        engine.fact("derived", "derived_ratio_audit:" + numerator_name + "/" + denominator_name, entity, value, unit, method, cells, "Displayed precision and computed agreement do not validate the count definition, denominator provenance or biological interpretation. Outside a declared comparison is not an outlier p-value or causal error.")
    result = dict(stats)
    result["max_absolute_difference"] = _float(max_difference)
    result["max_absolute_difference_record"] = max_record
    return result


def _identity_values(view, rows, names):
    result = {}
    for name in dict.fromkeys(names):
        index = _column_map_index(view, name)
        values = [_raw_value(view["rows"][local], index, view["columns"][index]) for local in rows]
        unique = {_json_key(_safe(value)): _safe(value) for value in values if not _missing(value)}
        result[name] = list(unique.values())[0] if len(unique) == 1 else _safe(values[0]) if len(values) == 1 else None
    return result


def _column_map_index(view, name):
    return view["column_map"][_norm(name)]


def _nested_metric_unit(view, metric, value_name, numerator_name=None, denominator_name=None):
    kind = metric.get("kind")
    declared = metric.get("unit")
    if kind == "ratio_from_counts":
        num_unit, num_conflict = _metric_unit(view["columns_original"][_column_map_index(view, numerator_name)])
        den_unit, den_conflict = _metric_unit(view["columns_original"][_column_map_index(view, denominator_name)])
        if num_conflict or den_conflict or ((num_unit is not None or den_unit is not None) and _unit_norm(num_unit) != _unit_norm(den_unit)):
            return None, {"metric": metric.get("name"), "numerator_unit": num_unit, "denominator_unit": den_unit, "reason": "ratio_count_units_not_verifiably_compatible"}
        if declared not in (None, "") and _unit_norm(declared) != "1":
            return None, {"metric": metric.get("name"), "declared_unit": declared, "reason": "count_ratio_must_be_dimensionless"}
        return "1", None
    return _metric_unit(view["columns_original"][_column_map_index(view, value_name)], declared)


def _inner_record(view, metric, unit, rows, row_unit_name):
    kind = metric["kind"]
    name = metric["name"]
    value_name = metric.get("value_column", name)
    numerator_name = metric.get("numerator_column")
    denominator_name = metric.get("denominator_column")
    value_index = _column_map_index(view, value_name) if kind != "ratio_from_counts" and value_name else None
    numerator_index = _column_map_index(view, numerator_name) if numerator_name else None
    denominator_index = _column_map_index(view, denominator_name) if denominator_name else None
    row_unit_index = _column_map_index(view, row_unit_name)
    records = []
    values = []
    numerators = []
    denominators = []
    missing_n = 0
    invalid_n = 0
    zero_denominator_n = 0
    cells = []
    for local_index in rows:
        row = view["rows"][local_index]
        source = {
            "row_ref": _row_ref(view, local_index),
            "source_row_index_zero_based": view["row_indices"][local_index],
            "row_unit_id": _safe(_raw_value(row, row_unit_index, view["columns"][row_unit_index])),
        }
        if kind == "ratio_from_counts":
            numerator_raw = _raw_value(row, numerator_index, view["columns"][numerator_index])
            denominator_raw = _raw_value(row, denominator_index, view["columns"][denominator_index])
            numerator = _integer(numerator_raw, True)
            denominator = _integer(denominator_raw, True)
            source.update({
                "raw_numerator": _safe(numerator_raw),
                "raw_denominator": _safe(denominator_raw),
                "numerator_cell": _cell_location(view, local_index, numerator_index),
                "denominator_cell": _cell_location(view, local_index, denominator_index),
            })
            cells.extend([source["numerator_cell"], source["denominator_cell"]])
            if _missing(numerator_raw) or _missing(denominator_raw):
                source["status"] = "missing_numerator_or_denominator"
                missing_n += 1
            elif numerator is None or denominator is None:
                source["status"] = "invalid_nonnegative_integer_count"
                invalid_n += 1
            else:
                numerators.append(numerator)
                denominators.append(denominator)
                source["status"] = "valid_raw_count_pair_zero_denominator_retained" if denominator == 0 else "valid_raw_count_pair"
                source["included_in_raw_count_means"] = True
                zero_denominator_n += int(denominator == 0)
        else:
            raw = _raw_value(row, value_index, view["columns"][value_index])
            number = _integer(raw, True) if kind == "count" else _decimal(raw)
            source.update({"raw_value": _safe(raw), "value_cell": _cell_location(view, local_index, value_index)})
            cells.append(source["value_cell"])
            if _missing(raw):
                source["status"] = "missing_measurement_not_zero"
                missing_n += 1
            elif number is None:
                source["status"] = "invalid_finite_count" if kind == "count" else "invalid_finite_numeric_measurement"
                invalid_n += 1
            else:
                values.append(number)
                source["status"] = "valid_zero_measurement" if number == 0 else "valid_measurement"
                source["included_in_inner_mean"] = True
        records.append(source)
    n = len(values) if kind != "ratio_from_counts" else len(numerators)
    public = {
        "metric_name": name,
        "kind": kind,
        "n_source_rows": len(rows),
        "n": n,
        "missing_n": missing_n,
        "invalid_n": invalid_n,
        "source_records": records,
    }
    numeric_value = None
    if kind == "ratio_from_counts":
        numerator_mean = _mean(numerators)
        denominator_mean = _mean(denominators)
        public.update({
            "numerator_column": numerator_name,
            "denominator_column": denominator_name,
            "zero_denominator_source_rows": zero_denominator_n,
            "raw_numerator_mean": _float(numerator_mean),
            "raw_denominator_mean": _float(denominator_mean),
            "integer_count_inputs_required": True,
        })
        if numerator_mean is not None and denominator_mean != 0:
            with localcontext() as context:
                context.prec = 50
                numeric_value = numerator_mean / denominator_mean
            public.update({"status": "computed", "ratio_of_raw_count_means": _float(numeric_value), "mean": _float(numeric_value)})
        elif numerator_mean is None:
            public.update({"status": "no_valid_count_pairs", "ratio_of_raw_count_means": None, "mean": None, "missing_reason": "no_valid_numerator_denominator_pairs"})
        else:
            public.update({"status": "undefined_zero_denominator_mean", "ratio_of_raw_count_means": None, "mean": None, "missing_reason": "mean_raw_denominator_is_zero_not_imputed"})
    else:
        average = _mean(values)
        numeric_value = average
        public.update({"source_value_column": value_name, "arithmetic_mean": _float(average), "mean": _float(average), "status": "computed" if average is not None else "no_valid_measurements"})
        if average is None:
            public["missing_reason"] = "missing_measurement_is_not_zero"
    return {"public": public, "number": numeric_value, "rows": list(rows), "unit": unit, "evidence": _compress_cells(cells)}


def _unique_level_count(view, rows, column_name):
    index = _column_map_index(view, column_name)
    return len({_json_key(_safe(_raw_value(view["rows"][local], index, view["columns"][index]))) for local in rows if not _missing(_raw_value(view["rows"][local], index, view["columns"][index]))})


def _nested_table(engine, view):
    rule = engine.rule
    row_unit_name = rule.get("row_unit_column")
    experimental_names = rule.get("experimental_unit_columns")
    pair_names = rule.get("pair_unit_columns")
    condition_name = rule.get("condition_column")
    left_condition = rule.get("left_condition", rule.get("left"))
    right_condition = rule.get("right_condition", rule.get("right"))
    metrics = rule.get("metrics")
    lineage_names = rule.get("lineage_dimensions", [])
    if not isinstance(row_unit_name, str) or not isinstance(experimental_names, list) or not experimental_names or not isinstance(pair_names, list) or not pair_names or not isinstance(condition_name, str):
        engine.issue("invalid_rule", "group_summary_nested_treatment_contrast", "Explicit row-unit, experimental-unit, pair-unit and condition columns are required.")
        return None
    if left_condition in (None, "") or right_condition in (None, "") or _same_category(left_condition, right_condition):
        engine.issue("invalid_rule", "group_summary_nested_treatment_contrast", "Two distinct left_condition/right_condition values are required.")
        return None
    if not isinstance(metrics, list) or not metrics or not isinstance(lineage_names, list):
        engine.issue("invalid_rule", "group_summary_nested_treatment_contrast", "A nonempty metrics list and a list-valued lineage_dimensions are required.")
        return None
    prepared_metrics = []
    metric_names = set()
    for metric_index, metric in enumerate(metrics):
        if not isinstance(metric, dict) or not isinstance(metric.get("name"), str) or metric["kind"] not in {"count", "ratio_from_counts", "numeric_continuous"}:
            engine.issue("invalid_rule", "group_summary_nested_treatment_contrast", "Every metric must have a unique name and kind count, ratio_from_counts or numeric_continuous.", metric_index=metric_index)
            return None
        name = metric["name"]
        if _norm(name) in metric_names:
            engine.issue("invalid_rule", "group_summary_nested_treatment_contrast", "Duplicate metric names would collide; no metric was silently replaced.", metric_index=metric_index)
            return None
        metric_names.add(_norm(name))
        current = dict(metric)
        if current["kind"] == "ratio_from_counts":
            if not isinstance(current.get("numerator_column"), str) or not isinstance(current.get("denominator_column"), str):
                engine.issue("invalid_rule", "group_summary_nested_treatment_contrast", "ratio_from_counts requires explicit numerator_column and denominator_column.", metric_index=metric_index)
                return None
        else:
            current["value_column"] = current.get("value_column", name)
            if not isinstance(current["value_column"], str):
                engine.issue("invalid_rule", "group_summary_nested_treatment_contrast", "A native metric value column is required.", metric_index=metric_index)
                return None
        prepared_metrics.append(current)
    duplicates_by_vial = defaultdict(list)
    row_unit_index = _column_map_index(view, row_unit_name)
    for local_index, row in enumerate(view["rows"]):
        raw = _raw_value(row, row_unit_index, view["columns"][row_unit_index])
        duplicates_by_vial[_json_key(_safe(raw))].append(local_index)
    duplicate_groups = {key: rows for key, rows in duplicates_by_vial.items() if len(rows) > 1 and not _missing(_raw_value(view["rows"][rows[0]], row_unit_index, view["columns"][row_unit_index]))}
    for key in sorted(duplicate_groups):
        rows = duplicate_groups[key]
        raw_id = _raw_value(view["rows"][rows[0]], row_unit_index, view["columns"][row_unit_index])
        records = [_row_snapshot(view, local) for local in rows]
        engine.fact("observed", "duplicate_row_unit_identity", {**_source_identity(view["original"]), "row_unit_column": row_unit_name, "row_unit_id": _safe(raw_id)}, {"status": "quality_violation", "row_count": len(rows), "raw_records": records, "disposition": "all_duplicate_rows_retained_but_excluded_as_ambiguous_inner_units"}, None, "Detect repeated configured row-unit IDs before averaging; do not treat repeats as new experimental units or choose one silently.", [_row_location(view, local) for local in rows], "Duplicate row-unit identity prevents a verified unique-vial aggregation; this is an identity QC issue, not a treatment effect or physical cause.")
        engine.issue("quality_violation", "nested_duplicate_row_unit", "Configured row-unit identifier is not unique; every duplicate row is retained in evidence and excluded from inner means as ambiguous identity.", [_row_location(view, local) for local in rows], row_unit_id=_safe(raw_id), row_refs=[_row_ref(view, local) for local in rows], count=len(rows))
    excluded = defaultdict(list)
    usable_rows = []
    identity_names = list(dict.fromkeys(experimental_names + pair_names + [condition_name, row_unit_name] + lineage_names))
    identity_indices = {name: _column_map_index(view, name) for name in identity_names}
    duplicate_rows = {local for rows in duplicate_groups.values() for local in rows}
    for local_index, row in enumerate(view["rows"]):
        reasons = ["duplicate_row_unit_id"] if local_index in duplicate_rows else []
        for name, index in identity_indices.items():
            if _missing(_raw_value(row, index, view["columns"][index])):
                reasons.append("missing_identity:" + name)
        if reasons:
            excluded[local_index].extend(reasons)
        else:
            usable_rows.append(local_index)
    inner_columns = list(dict.fromkeys(experimental_names + [condition_name] + lineage_names))
    inner_groups = defaultdict(list)
    for local_index in usable_rows:
        key = tuple(_category_key(_raw_value(view["rows"][local_index], identity_indices[name], view["columns"][identity_indices[name]])) for name in inner_columns)
        inner_groups[key].append(local_index)
    inners = []
    for key in sorted(inner_groups):
        rows = sorted(inner_groups[key])
        identity = _identity_values(view, rows, inner_columns)
        inner = {
            "identity": identity,
            "identity_values": [identity.get(name) for name in inner_columns],
            "condition": identity.get(condition_name),
            "pair_values": _identity_values(view, rows, pair_names),
            "lineage_values": _identity_values(view, rows, lineage_names),
            "rows": rows,
            "row_unit_ids": [_safe(_raw_value(view["rows"][local], row_unit_index, view["columns"][row_unit_index])) for local in rows],
            "metrics": {},
        }
        tank_entity = {**_source_identity(view["original"]), "experimental_unit": identity, "row_unit_column": row_unit_name, "row_unit_ids": inner["row_unit_ids"]}
        for metric in prepared_metrics:
            unit = metric["_unit"]
            aggregate = _inner_record(view, metric, unit, rows, row_unit_name)
            inner["metrics"][metric["name"]] = aggregate
            public = dict(aggregate["public"])
            public["unit_basis"] = "native_header_or_rule_declared" if unit != "1" else "dimensionless_ratio_of_compatible_raw_integer_counts"
            kind = "derived" if aggregate["number"] is not None else "unresolved"
            prefix = "inner_experimental_unit_ratio_of_raw_count_means:" if metric["kind"] == "ratio_from_counts" else "inner_experimental_unit_arithmetic_mean:"
            method = "For this actual configured tank/experimental-unit×condition×lineage identity and this metric only, arithmetic mean over its own valid source rows; n and missing/invalid n are explicit, missing is not zero. Ratio metrics average raw numerator and raw denominator over the same valid integer-count rows, then divide the means; displayed rounded proportions are not used."
            engine.fact(kind, prefix + metric["name"], tank_entity, public, unit, method, aggregate["evidence"] or [_row_location(view, local) for local in rows], "A vial/sample average within an experimental unit is not a set of independent treatment allocations. Undefined or absent means are not zero; zero raw measurements remain real observations.")
        inners.append(inner)
    by_pair = defaultdict(list)
    for inner in inners:
        key = tuple(_json_key(inner["pair_values"].get(name)) for name in pair_names)
        by_pair[key].append(inner)
    pair_records = []
    pair_structures = []
    for key in sorted(by_pair):
        members = sorted(by_pair[key], key=lambda inner: (_category_key(inner["condition"]), _json_key(inner["identity"])))
        pair_values = members[0]["pair_values"]
        condition_counts = Counter(_category_key(inner["condition"]) for inner in members)
        left = [inner for inner in members if _same_category(inner["condition"], left_condition)]
        right = [inner for inner in members if _same_category(inner["condition"], right_condition)]
        reasons = []
        if len(left) != 1:
            reasons.append("missing_left_inner_unit" if not left else "ambiguous_multiple_left_inner_units")
        if len(right) != 1:
            reasons.append("missing_right_inner_unit" if not right else "ambiguous_multiple_right_inner_units")
        if len(members) and any(value is None for name, value in pair_values.items()):
            reasons.append("unresolved_pair_identity")
        if len(left) == len(right) == 1:
            for name in lineage_names:
                if _json_key(left[0]["lineage_values"].get(name)) != _json_key(right[0]["lineage_values"].get(name)):
                    reasons.append("conflicting_lineage_within_pair:" + name)
        reasons = list(dict.fromkeys(reasons))
        structure = {"pair_values": pair_values, "members": members, "left": left[0] if len(left) == 1 else None, "right": right[0] if len(right) == 1 else None, "reasons": reasons, "condition_counts": dict(sorted(condition_counts.items()))}
        pair_structures.append(structure)
        if reasons:
            for inner in members:
                for local in inner["rows"]:
                    excluded[local].extend(reasons)
            evidence = [_row_location(view, local) for inner in members for local in inner["rows"]]
            engine.fact("unresolved", "nested_pair_structure", {**_source_identity(view["original"]), "pair_unit": pair_values, "left_condition": _safe(left_condition), "right_condition": _safe(right_condition)}, {"status": "unresolved", "reason_codes": reasons, "inner_unit_treatment_counts": structure["condition_counts"], "inner_units": [{"experimental_unit": inner["identity"], "row_unit_ids": inner["row_unit_ids"]} for inner in members], "disposition": "paired_source_identity_withheld_not_zero"}, None, "Require exactly one left and one right actual inner experimental unit per configured pair identity and consistent lineage. No cross-product, zero-fill or unmatched pairing.", evidence, "This potential pair has no verified treatment contrast. Its rows remain in inner-unit and coverage records and are not silent missing pairs.")
            engine.issue("cannot_determine", "nested_pair_structure_unresolved", "A potential pair lacks one unambiguous inner unit per requested condition or has conflicting lineage; no contrast was fabricated.", evidence, pair_unit=pair_values, reason_codes=reasons, inner_unit_treatment_counts=structure["condition_counts"])
            continue
        for metric in prepared_metrics:
            left_metric = structure["left"]["metrics"][metric["name"]]
            right_metric = structure["right"]["metrics"][metric["name"]]
            left_number, right_number = left_metric["number"], right_metric["number"]
            pair_values_full = {
                "left_condition": _safe(left_condition),
                "right_condition": _safe(right_condition),
                "left": left_metric["public"],
                "right": right_metric["public"],
                "left_experimental_unit": structure["left"]["identity"],
                "right_experimental_unit": structure["right"]["identity"],
                "left_Tank_or_experimental_unit_IDs": structure["left"]["identity"],
                "right_Tank_or_experimental_unit_IDs": structure["right"]["identity"],
                "left_Vial_or_row_unit_IDs": structure["left"]["row_unit_ids"],
                "right_Vial_or_row_unit_IDs": structure["right"]["row_unit_ids"],
            }
            if left_number is None or right_number is None:
                pair_values_full.update({"status": "unresolved_metric_pair", "difference_left_minus_right": None, "reason": "one_or_both_inner_metric_means_unavailable"})
                pair_kind = "unresolved"
            else:
                difference = left_number - right_number
                pair_values_full.update({"status": "computed", "difference_left_minus_right": _float(difference), "nInnerEachCondition": {"left": left_metric["public"]["n"], "right": right_metric["public"]["n"]}})
                pair_kind = "derived"
            entity = {**_source_identity(view["original"]), "pair_unit": pair_values, "lineage": {name: structure["left"]["lineage_values"].get(name) for name in lineage_names}, "left_experimental_unit": structure["left"]["identity"], "right_experimental_unit": structure["right"]["identity"]}
            evidence = list(dict.fromkeys(left_metric["evidence"] + right_metric["evidence"]))
            pair_record = {"structure": structure, "metric": metric, "left_metric": left_metric, "right_metric": right_metric, "difference": left_number - right_number if left_number is not None and right_number is not None else None, "entity": entity, "evidence": evidence}
            pair_records.append(pair_record)
            engine.fact(pair_kind, "nested_paired_left_minus_right:" + metric["name"], entity, pair_values_full, metric["_unit"], "After metric-specific raw inner aggregation, compute configured left minus right only for this complete Family/pair-unit×Block(or configured pair columns) structure and its actual two experimental units. Raw left/right means, count means, IDs, nInnerEachCondition and all missingness remain attached.", evidence or [_row_location(view, local) for local in structure["left"]["rows"] + structure["right"]["rows"]], "Pairwise completeness is specific to this metric. No missing pair becomes zero, no p-value/CI/significance or independent-vial treatment claim is produced.")
    summary_by_metric = {}
    compatible_structure_count = sum(not structure["reasons"] for structure in pair_structures)
    for metric in prepared_metrics:
        records = [record for record in pair_records if record["metric"]["name"] == metric["name"]]
        valid = [record for record in records if record["difference"] is not None]
        differences = [record["difference"] for record in valid]
        values = {
            "nPairs": len(valid),
            "all_potential_pairs": len(pair_structures),
            "structurally_compatible_pairs": compatible_structure_count,
            "unresolved_metric_pairs": len(records) - len(valid),
            "mean_paired_difference": _float(_mean(differences)),
            "median_paired_difference": _float(_median(differences)),
            "range_paired_difference": [_float(min(differences)), _float(max(differences))] if differences else None,
            "left_condition": _safe(left_condition),
            "right_condition": _safe(right_condition),
            "pairwise_complete_policy": "each metric has its own valid set; missing or undefined is not zero",
        }
        summary_by_metric[metric["name"]] = {"values": values, "records": valid}
        evidence = []
        for record in valid:
            evidence.extend(record["evidence"])
        if not valid:
            engine.issue("cannot_determine", "nested_metric_pair_summary", "No complete metric-specific pair remained; no zero, unpaired mean or inferential substitute was used.", [_header_location(view)], metric=metric["name"], all_potential_pairs=len(pair_structures))
        engine.fact("derived" if valid else "unresolved", "nested_paired_contrast_summary:" + metric["name"], {**_source_identity(view["original"]), "pair_unit_columns": pair_names, "experimental_unit_columns": experimental_names}, values, metric["_unit"], "Arithmetic mean, median and [minimum,maximum] of actual left-right inner-unit paired differences over this metric's complete pairs only; nPairs is recomputed from valid records and all potential/structurally compatible pair counts are reported separately.", _compress_cells(evidence) or [_header_location(view)], "Descriptive paired summary only. No default confidence interval, t-test, significance claim, treatment cause or independence inflation is generated.")
    for dimension in lineage_names:
        dimension_index = _column_map_index(view, dimension)
        levels = {}
        for local in usable_rows:
            raw = _raw_value(view["rows"][local], dimension_index, view["columns"][dimension_index])
            levels.setdefault(_json_key(_safe(raw)), _safe(raw))
        for level_key in sorted(levels):
            level = levels[level_key]
            kept_by_metric = {}
            excluded_pair_count = 0
            excluded_evidence = []
            kept_evidence = []
            for structure in pair_structures:
                if structure["reasons"]:
                    continue
                same = _json_key(structure["left"]["lineage_values"].get(dimension)) == level_key or _json_key(structure["right"]["lineage_values"].get(dimension)) == level_key
                if same:
                    excluded_pair_count += 1
                    for inner in (structure["left"], structure["right"]):
                        excluded_evidence.extend(_row_location(view, local) for local in inner["rows"])
            insufficient = []
            for metric in prepared_metrics:
                summary = summary_by_metric[metric["name"]]
                before = summary["records"]
                kept = [record for record in before if _json_key(record["structure"]["left"]["lineage_values"].get(dimension)) != level_key and _json_key(record["structure"]["right"]["lineage_values"].get(dimension)) != level_key]
                differences = [record["difference"] for record in kept]
                remaining_mean = _mean(differences)
                overall_mean = _mean([record["difference"] for record in before])
                status = "computed" if kept else "insufficient_remaining_units"
                if not kept:
                    insufficient.append(metric["name"])
                kept_by_metric[metric["name"]] = {
                    "status": status,
                    "nPairs_before": len(before),
                    "nPairs_after": len(kept),
                    "overall_mean_paired_difference": _float(overall_mean),
                    "remaining_mean_paired_difference": _float(remaining_mean),
                    "change_in_mean_paired_difference": _float(remaining_mean - overall_mean) if remaining_mean is not None and overall_mean is not None else None,
                }
                for record in kept:
                    kept_evidence.extend(record["evidence"])
            engine.fact("derived" if len(insufficient) < len(prepared_metrics) else "unresolved", "nested_lineage_leave_one_level_out:" + dimension, {**_source_identity(view["original"]), "lineage_dimension": dimension, "excluded_level": level}, {"status": "insufficient_remaining_units" if len(insufficient) == len(prepared_metrics) else "computed_with_metric_specific_limits", "excluded_pair_count": excluded_pair_count, "whole_level_exclusion": True, "metrics": kept_by_metric, "insufficient_metrics": insufficient}, None, "Exclude every complete pair belonging to this one lineage level across both conditions, then recompute the simple arithmetic mean paired contrast per metric over remaining Family/pair×Block units; change is remaining mean minus overall mean. This is sensitivity description, not independent significance testing.", _compress_cells(kept_evidence) or sorted(set(excluded_evidence)), "Leave-one-level-out removes whole parent groups without splitting vials or treating repeated G-matrix/family blocks as independent tests. Empty remaining metric sets remain unresolved and are never replaced by the overall mean.")
            if insufficient:
                engine.issue("cannot_determine", "nested_lineage_leave_one_level_out_insufficient", "Whole-level lineage exclusion leaves no complete pair for one or more metrics; no overall-mean substitute was used.", sorted(set(excluded_evidence)), lineage_dimension=dimension, excluded_level=level, insufficient_metrics=insufficient)
    for local_index, reasons in excluded.items():
        excluded[local_index] = list(dict.fromkeys(reasons))
    computed_counts = {
        "data_rows_after_header_resolution": len(view["rows"]),
        "identity_rows_used_for_inner_units": len(usable_rows),
        "distinct_row_units": len({key for key in duplicates_by_vial if not _missing(_raw_value(view["rows"][duplicates_by_vial[key][0]], row_unit_index, view["columns"][row_unit_index]))}),
        "inner_experimental_units": len(inners),
        "potential_pair_units": len(pair_structures),
        "selected_compatible_pairs": compatible_structure_count,
        "treatment_levels": _unique_level_count(view, usable_rows, condition_name),
        "duplicate_row_unit_groups": len(duplicate_groups),
        "duplicate_row_unit_records": sum(len(rows) for rows in duplicate_groups.values()),
    }
    column_cardinality = {name: _unique_level_count(view, usable_rows, name) for name in dict.fromkeys(experimental_names + pair_names + lineage_names)}
    computed_counts["distinct_configured_column_values"] = column_cardinality
    tank_candidates = [name for name in experimental_names if name not in pair_names and name != condition_name and name not in lineage_names]
    family_candidates = [name for name in pair_names if name in lineage_names]
    generic_aliases = {
        "rows": len(view["rows"]),
        "vials": computed_counts["distinct_row_units"],
        "tanks": column_cardinality.get(tank_candidates[0]) if len(tank_candidates) == 1 else len(inners),
        "families": column_cardinality.get(family_candidates[0]) if len(family_candidates) == 1 else column_cardinality.get(pair_names[0]) if len(pair_names) == 1 else None,
        "pairs": len(pair_structures),
        "treatment_levels": computed_counts["treatment_levels"],
    }
    declared = next((rule[key] for key in ("declared_counts", "expected_counts", "design_counts") if isinstance(rule.get(key), dict)), None)
    declarations = None
    if declared is not None:
        declarations = []
        for name in sorted(declared, key=lambda item: _token(item)):
            actual = generic_aliases.get(_token(name), computed_counts.get(str(name)))
            expected = _integer(declared[name], True)
            declarations.append({"declared_name": str(name), "declared": int(expected) if expected is not None else _safe(declared[name]), "computed": actual, "matches": actual == int(expected) if expected is not None and actual is not None else None})
            if expected is None or actual is None:
                engine.issue("cannot_determine", "nested_declared_outer_count", "A declared outer count could not be mapped unambiguously to a computed configured structural count.", [_header_location(view)], declaration=_safe(declarations[-1]))
            elif actual != int(expected):
                engine.issue("quality_violation", "nested_declared_outer_count_mismatch", "A source-declared structural count differs from the actual selected rows/identities; neither count was overwritten.", [_header_location(view)], declaration=_safe(declarations[-1]))
    pair_treatment_count = [{"pair_unit": structure["pair_values"], "inner_unit_treatment_counts": structure["condition_counts"], "complete_compatible_pair": not structure["reasons"], "reason_codes": structure["reasons"]} for structure in pair_structures]
    excluded_records = []
    by_reason = Counter()
    for local_index in sorted(excluded):
        row = view["rows"][local_index]
        reasons = excluded[local_index]
        for reason in reasons:
            by_reason[reason] += 1
        excluded_records.append({"row_ref": _row_ref(view, local_index), "source_locator": _row_location(view, local_index), "row_unit_id": _safe(_raw_value(row, row_unit_index, view["columns"][row_unit_index])), "reason_codes": reasons, "raw_row": _safe(row), "disposition": "retained_in_coverage_not_silently_deleted"})
    result = {
        "nrows": len(view["rows"]),
        "computed_outer_counts": computed_counts,
        "generic_outer_count_aliases": generic_aliases,
        "declared_outer_count_comparisons": declarations,
        "pair_treatment_count": pair_treatment_count,
        "n_selected_compatible_pairs": compatible_structure_count,
        "n_all_potential_pairs": len(pair_structures),
        "excluded_rows": excluded_records,
        "excluded_rows_by_reason": dict(sorted(by_reason.items())),
        "metric_pair_counts": {name: summary_by_metric[name]["values"]["nPairs"] for name in sorted(summary_by_metric)},
    }
    return result


def _required_nested_columns(rule):
    names = [rule.get(key) for key in ("row_unit_column", "condition_column")]
    for key in ("experimental_unit_columns", "pair_unit_columns", "lineage_dimensions"):
        value = rule.get(key, [])
        if isinstance(value, list):
            names.extend(value)
    metrics = rule.get("metrics")
    if isinstance(metrics, list):
        for metric in metrics:
            if not isinstance(metric, dict):
                continue
            if metric.get("kind") == "ratio_from_counts":
                names.extend([metric.get("numerator_column"), metric.get("denominator_column")])
            else:
                names.append(metric.get("value_column", metric.get("name")))
    return [name for name in names if isinstance(name, str) and name]


def analyze_rules(data: dict, rules: list | dict) -> dict:
    """Evaluate supported declarative rules without case identity or label-based answers."""
    safe_data = data if isinstance(data, dict) else {}
    if isinstance(rules, dict):
        if isinstance(rules.get("analysis_rules_v1"), list):
            rule_list = rules["analysis_rules_v1"]
        elif isinstance(rules.get("rules"), list):
            rule_list = rules["rules"]
        elif "operation" in rules:
            rule_list = [rules]
        else:
            rule_list = None
    elif isinstance(rules, list):
        rule_list = rules
    else:
        rule_list = None
    engine = _Engine(safe_data)
    if not isinstance(data, dict):
        engine.issue("invalid_input", "analyze_rules", "data must be a dictionary; no tables were analyzed.")
    if rule_list is None:
        engine.issue("invalid_rules", "analyze_rules", "rules must be a rule object, a list, or an object containing analysis_rules_v1/rules list.")
        rule_list = []
    for rule_index, rule in enumerate(rule_list):
        engine.rule_index = rule_index
        engine.rule = deepcopy(rule) if isinstance(rule, dict) else {}
        engine.operation = engine.rule.get("operation") if isinstance(engine.rule.get("operation"), str) else None
        if not isinstance(rule, dict) or engine.operation not in _OPERATIONS:
            engine.issue("unsupported_operation" if engine.operation is not None or isinstance(rule, dict) else "invalid_rule", "rule_dispatch", "Only the three explicitly supported operations are evaluated; no guessed analysis was performed.", requested_operation=_safe(engine.operation))
            engine.finish_rule("skipped", [], {"operation": engine.operation})
            continue
        before_facts = len(engine.facts)
        selections = {"operation": engine.operation, "sheet_selector": _safe(rule.get("sheet_selector", rule.get("table_selector"))), "table_id": _safe(rule.get("table_id"))}
        try:
            positioned_tables = engine.select_tables()
            table_coverage = []
            for positioned in positioned_tables:
                if engine.operation == "paired_replicate_range":
                    required = list(rule.get("group_by") or []) + [rule.get("value_column"), rule.get("replicate_column")]
                    required = [name for name in required if isinstance(name, str)]
                    view = engine.make_view(positioned, required)
                elif engine.operation == "derived_ratio_audit":
                    required = [rule.get("numerator_column", rule.get("numerator")), rule.get("denominator_column", rule.get("denominator"))]
                    required = [name for name in required if isinstance(name, str)]
                    optional = [rule.get("reported_column", rule.get("reported"))]
                    view = engine.make_view(positioned, required, [name for name in optional if isinstance(name, str)])
                else:
                    required = _required_nested_columns(rule)
                    view = engine.make_view(positioned, required)
                if view is None:
                    table_coverage.append({"nrows": 0, **_source_identity(positioned[1]), "status": "skipped_column_or_header_contract"})
                    continue
                selections.setdefault("resolved_tables", []).append(_selection_record(view))
                if engine.operation == "group_summary_nested_treatment_contrast":
                    metrics = engine.rule.get("metrics") if isinstance(engine.rule.get("metrics"), list) else []
                    conflict = False
                    for metric in metrics:
                        if not isinstance(metric, dict) or metric.get("kind") not in {"count", "ratio_from_counts", "numeric_continuous"}:
                            continue
                        value_name = metric.get("value_column", metric.get("name"))
                        unit, unit_conflict = _nested_metric_unit(view, metric, value_name, metric.get("numerator_column"), metric.get("denominator_column"))
                        if unit_conflict:
                            engine.issue("unit_conflict", "group_summary_nested_treatment_contrast", "A configured metric has conflicting or incompatible units; this table's hierarchy was not analyzed by choosing another column/unit.", [_header_location(view)], **unit_conflict)
                            conflict = True
                        else:
                            metric["_unit"] = unit
                    if conflict:
                        table_coverage.append({"nrows": len(view["rows"]), **_source_identity(view["original"]), "status": "skipped_unit_conflict"})
                        continue
                if engine.operation == "paired_replicate_range":
                    coverage = _paired_table(engine, view)
                elif engine.operation == "derived_ratio_audit":
                    coverage = _ratio_table(engine, view)
                else:
                    coverage = _nested_table(engine, view)
                if coverage is None:
                    table_coverage.append({"nrows": len(view["rows"]), **_source_identity(view["original"]), "status": "skipped_invalid_or_ambiguous_rule"})
                else:
                    coverage = dict(coverage)
                    coverage.update({**_source_identity(view["original"]), "status": "completed", "input_rows": len(view["rows"])})
                    table_coverage.append(coverage)
            status = "completed" if table_coverage and all(item.get("status") == "completed" for item in table_coverage) else "partial" if any(item.get("status") == "completed" for item in table_coverage) else "skipped"
            if not positioned_tables:
                status = "skipped"
            extra = {"facts_emitted": len(engine.facts) - before_facts}
            aggregate_keys = ("ngroups", "npairs", "nflag", "completepaircount", "missingOrAmbiguous", "ratios_computed", "undefined_zero_denominator", "missing_or_invalid_inputs", "reported_compared", "reported_missing", "n_selected_compatible_pairs", "n_all_potential_pairs")
            for key in aggregate_keys:
                values = [item[key] for item in table_coverage if isinstance(item.get(key), (int, float))]
                if values:
                    extra[key] = sum(values)
            engine.finish_rule(status, table_coverage, selections, extra)
        except Exception as exc:
            engine.issue("runtime_failure", "rule_evaluation", "Rule evaluation failed safely with no guessed result: %s: %s" % (type(exc).__name__, str(exc)))
            engine.finish_rule("failed", [], selections, {"facts_emitted": len(engine.facts) - before_facts})
    engine.rule = None
    coverage = {
        "rules_total": len(rule_list),
        "rules_completed": sum(item["status"] == "completed" for item in engine.rule_coverage),
        "rules_partial": sum(item["status"] == "partial" for item in engine.rule_coverage),
        "rules_skipped_or_failed": sum(item["status"] in {"skipped", "failed"} for item in engine.rule_coverage),
        "facts": len(engine.facts),
        "issues": len(engine.issues),
        "operations": dict(sorted(Counter(item["operation"] for item in engine.rule_coverage if item.get("operation")).items())),
        "rules": engine.rule_coverage,
        "row_bound_per_selected_table": _MAX_ROWS,
        "numeric_policy": "Finite scalars only; Decimal arithmetic with finite JSON floats; nonfinite results become null and unresolved rather than NaN/Infinity.",
        "ordering": "Rules retain input order; tables and all groups/levels are sorted deterministically by source identity and native values; raw source order is retained inside each record.",
        "unsupported_analysis": "Unknown operations and ambiguous source/QC contracts produce issues and no guessed calculations.",
    }
    return _safe({"facts": engine.facts, "issues": engine.issues, "methods": engine.methods, "coverage": coverage})


__all__ = ["analyze_rules"]
