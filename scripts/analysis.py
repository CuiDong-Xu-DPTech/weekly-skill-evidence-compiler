"""Deterministic, evidence-linked scientific calculations for the runtime workflow."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import warnings as _stdlib_warnings
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple


_MAD_SCALE = 0.6744897501960817
_MAD_THRESHOLD = 3.5
_GRID_POINTS = 201
_MIN_CURVE_POINTS = 20
_MAX_CURVES = 1000
_MAX_MBD_CURVES = 300
_MAX_RAW_POINTS_PER_TRACE = 20000
_MAX_LOO_WORK = 10000000
_MAX_PREDICTION_RECORDS = 100000
_MISSING_TEXT = {"", "na", "n/a", "nan", "null", "none", "missing", "."}
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[Ee][-+]?\d+)?(?![A-Za-z0-9_.])")
_QUALITY_BAD_RE = re.compile(r"fail|invalid|error|saturat|out[- ]?of[- ]?range|overrange|underflow|overflow|corrupt|bad|reject|异常|失败|无效|饱和|超量程|错误|冲突", re.I)
_CONDITION_WORDS = ("condition", "group", "treatment", "dose", "temperature", "batch", "channel", "plate", "instrument", "device", "run", "sample", "specimen", "subject", "well")
_TECHNICAL_WORDS = ("repeat", "replicate", "channel", "technical_rep", "time")
_X_WORDS = ("time", "displacement", "deflection", "strain", "wavelength", "wavenumber", "temperature", "concentration", "voltage", "current", "frequency", "distance", "position", "x_axis", "x value", "elapsed", "扫描时间", "位移", "应变", "波长", "时间")


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", str(value).lower())


def _finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            text = value.strip()
            if text.lower() in _MISSING_TEXT:
                return None
            if "," in text:
                if not re.fullmatch(r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d*)?(?:[Ee][-+]?\d+)?", text):
                    return None
                text = text.replace(",", "")
            if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?", text):
                return None
            value = text
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in _MISSING_TEXT:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return False


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        try:
            result = float(value)
            return result if math.isfinite(result) else None
        except (ValueError, OverflowError):
            return None
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_safe(item) for item in sorted(value, key=lambda item: str(item))]
    if hasattr(value, "item"):
        try:
            return _safe(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value), "sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _json_key(value: Any) -> str:
    return json.dumps(_safe(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def _quantile(values: Sequence[float], probability: float) -> Optional[float]:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _median(values: Sequence[float]) -> Optional[float]:
    return _quantile(values, 0.5)


def _mad(values: Sequence[float], center: Optional[float] = None) -> Optional[float]:
    if not values:
        return None
    if center is None:
        center = _median(values)
    return _median([abs(value - center) for value in values])


def _sample_sd(values: Sequence[float]) -> Optional[float]:
    if len(values) < 2:
        return None
    mean = math.fsum(values) / len(values)
    variance = math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def _column_name(column: Any, index: int) -> str:
    if isinstance(column, Mapping):
        for key in ("name", "column", "id", "header"):
            if column.get(key) not in (None, ""):
                return str(column[key])
    if column not in (None, ""):
        return str(column)
    return "column_%d" % (index + 1)


def _column_letter(index: int) -> str:
    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _row_mapping(table: Mapping[str, Any], row: Any, names: Sequence[str]) -> Dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    values = list(row) if isinstance(row, (list, tuple)) else [row]
    return {name: values[index] if index < len(values) else None for index, name in enumerate(names)}


def _row_value(row: Mapping[str, Any], name: str, index: int) -> Any:
    if name in row:
        return row[name]
    if str(index) in row:
        return row[str(index)]
    normalized = _norm(name)
    for key, value in row.items():
        if _norm(key) == normalized:
            return value
    return None


def _row_ref(table: Mapping[str, Any], index: int) -> str:
    refs = table.get("row_refs")
    ref = None
    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)) and index < len(refs):
        ref = refs[index]
    elif isinstance(refs, Mapping):
        ref = refs.get(str(index), refs.get(index))
    if isinstance(ref, Mapping):
        for key in ("ref", "locator", "address", "range", "row"):
            if ref.get(key) is not None:
                ref = ref[key]
                break
    if ref is not None:
        return str(ref)
    metadata = table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}
    header_rows = int(_finite_float(table.get("header_rows", metadata.get("header_rows", 1))) or 1)
    return str(index + header_rows + 1)


def _location(path: Any, sheet: Any, cell_range: Optional[str] = None) -> str:
    result = str(path) if path not in (None, "") else "<unknown-path>"
    if sheet not in (None, ""):
        result += "#" + str(sheet)
        if cell_range and cell_range.startswith(str(sheet) + "!"):
            cell_range = cell_range[len(str(sheet)) + 1:]
    if cell_range:
        if sheet in (None, "") and cell_range.startswith(result + ":"):
            return cell_range
        result += "!" + cell_range
    return result


def _native_array_range(table: Mapping[str, Any], column_index: Optional[int], row_index: Optional[int]) -> Optional[str]:
    refs = table.get("row_refs")
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)) or not refs:
        return None
    pattern = re.compile(r"^(?P<prefix>.*\[)(?P<row>\d+),(?P<first_column>\d+):(?P<last_column>\d+)(?P<suffix>\])$")
    selected = _row_ref(table, row_index) if row_index is not None else _row_ref(table, 0)
    match = pattern.fullmatch(selected)
    if not match:
        return selected if "::" in selected else None
    base = int(match.group("first_column"))
    first_column = base if column_index is None else base + column_index
    last_column = int(match.group("last_column")) + 1 if column_index is None else first_column
    if row_index is not None:
        cell_range = match.group("row") + "," + str(first_column)
        if first_column != last_column:
            cell_range += ":" + str(last_column)
        return match.group("prefix") + cell_range + match.group("suffix")
    last_match = pattern.fullmatch(_row_ref(table, len(refs) - 1))
    last_row = last_match.group("row") if last_match else match.group("row")
    row_range = match.group("row") if last_row == match.group("row") else match.group("row") + ":" + last_row
    column_range = str(first_column) if first_column == last_column else str(first_column) + ":" + str(last_column)
    return match.group("prefix") + row_range + "," + column_range + match.group("suffix")


def _table_range(table: Mapping[str, Any], column_index: Optional[int] = None, row_index: Optional[int] = None) -> str:
    native = _native_array_range(table, column_index, row_index)
    if native is not None:
        return native
    names = list(table.get("_names", []))
    row_count = int(table.get("_row_count", 0))
    sheet = table.get("sheet")
    prefix = str(sheet) + "!" if sheet not in (None, "") else ""
    def ref(row):
        value = _row_ref(table, row)
        return value[len(prefix):] if prefix and value.startswith(prefix) else value
    def cell(row, last=False):
        first, _, end = ref(row).partition(":")
        match = re.fullmatch(r"(?:([A-Z]+))?(\d+)", end if last and end else first)
        if not match:
            return None
        letter = _column_letter(column_index) if column_index is not None else match.group(1)
        return (letter + match.group(2)) if letter else None
    def fallback(row):
        value = _location(table.get("path"), sheet, ref(row))
        if column_index is not None and column_index < len(names):
            value += " [column=" + str(names[column_index]) + "]"
        return value
    if row_index is not None:
        first, last = cell(row_index), cell(row_index, True)
        if not first or not last:
            return fallback(row_index)
        value = first if first == last else first + ":" + last
        return _location(table.get("path"), sheet, value)
    if not row_count:
        return _location(table.get("path"), sheet)
    start, end = cell(0), cell(row_count - 1, True)
    if not start or not end:
        return fallback(0) + " .. " + fallback(row_count - 1)
    return _location(table.get("path"), sheet, start + ":" + end)


def _header_unit(header: str) -> Optional[str]:
    text = str(header).strip()
    patterns = (r"\[\s*([^\[\]]{1,40})\s*\]\s*$", r"\(\s*(?:unit\s*[:=]\s*)?([^()]{1,40})\s*\)\s*$", r"\bunit\s*[:=]\s*([^\s,;]{1,40})\s*$")
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            unit = match.group(1).strip()
            if not re.search(r"\d\s*=|^\d+$|^(?:mean|average|note|notes|sample|n\s*=)$", unit, re.I):
                return unit
    return None


def _case_unit(case: Mapping[str, Any], metric: str) -> Optional[str]:
    target = _norm(metric)
    for key in ("units", "column_units", "metric_units"):
        mapping = case.get(key)
        if isinstance(mapping, Mapping):
            for name, unit in mapping.items():
                if (_norm(name) == target or _norm(name) in target or target in _norm(name)) and isinstance(unit, str) and unit.strip():
                    return unit.strip()
    for text in _case_text(case).splitlines():
        if target and target in _norm(text):
            patterns = (
                re.escape(str(metric)) + r"[^\n]{0,50}?\(\s*(?:unit\s*[:=]\s*)?([^()\d=]{1,30})\s*\)",
                re.escape(str(metric)) + r"[^\n]{0,50}?\[\s*([^\[\]]{1,30})\s*\]",
                re.escape(str(metric)) + r"[^\n]{0,50}?\bunit\s*(?:is|=|:)\s*([^\s,;]{1,30})",
            )
            for pattern in patterns:
                match = re.search(pattern, text, re.I)
                if match:
                    return match.group(1).strip()
    return None


def _metric_unit(case: Mapping[str, Any], metric: str) -> Optional[str]:
    return _header_unit(metric) or _case_unit(case, metric)


def _case_text(value: Any) -> str:
    chunks: List[str] = []

    def visit(node: Any, depth: int) -> None:
        if depth > 12 or sum(len(chunk) for chunk in chunks) > 2000000:
            return
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, Mapping):
            for key in sorted(node, key=lambda item: str(item)):
                visit(node[key], depth + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item, depth + 1)

    visit(value, 0)
    return "\n".join(chunks)


def _stats(values: Sequence[float]) -> Dict[str, Any]:
    ordered = sorted(values)
    median = _median(ordered)
    q1 = _quantile(ordered, 0.25)
    q3 = _quantile(ordered, 0.75)
    return {
        "min": ordered[0] if ordered else None,
        "max": ordered[-1] if ordered else None,
        "mean": math.fsum(ordered) / len(ordered) if ordered else None,
        "sample_sd": _sample_sd(ordered),
        "median": median,
        "iqr": q3 - q1 if q1 is not None and q3 is not None else None,
        "mad": _mad(ordered, median),
    }


def _rank(values: Sequence[float]) -> List[float]:
    order = sorted(range(len(values)), key=lambda index: (values[index], index))
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position + 1
        while end < len(order) and values[order[end]] == values[order[position]]:
            end += 1
        rank = (position + 1 + end) / 2.0
        for place in range(position, end):
            ranks[order[place]] = rank
        position = end
    return ranks


def _correlation(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    if len(x) != len(y) or len(x) < 2:
        return None
    mean_x = math.fsum(x) / len(x)
    mean_y = math.fsum(y) / len(y)
    xx = math.fsum((value - mean_x) ** 2 for value in x)
    yy = math.fsum((value - mean_y) ** 2 for value in y)
    if xx == 0 or yy == 0:
        return None
    return math.fsum((x[index] - mean_x) * (y[index] - mean_y) for index in range(len(x))) / math.sqrt(xx * yy)


def _ols(x: Sequence[float], y: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    if len(x) != len(y) or len(x) < 2:
        return None, None
    mean_x = math.fsum(x) / len(x)
    mean_y = math.fsum(y) / len(y)
    denominator = math.fsum((value - mean_x) ** 2 for value in x)
    if denominator == 0:
        return None, mean_y
    slope = math.fsum((x[index] - mean_x) * (y[index] - mean_y) for index in range(len(x))) / denominator
    return slope, mean_y - slope * mean_x


def _unique_spacing(values: Sequence[float]) -> Optional[float]:
    unique = sorted(set(values))
    differences = [unique[index + 1] - unique[index] for index in range(len(unique) - 1) if unique[index + 1] > unique[index]]
    return min(differences) if differences else None


def _robust_deviation(observed: float, references: Sequence[float]) -> Dict[str, Any]:
    center = _median(references)
    if center is None:
        return {"reference": None, "deviation": None, "scale": None, "score": None, "candidate": False, "threshold": {"value": None, "basis": "no finite leave-one-out reference"}}
    deviation = observed - center
    scale = _mad(references, center)
    if scale is not None and scale > 0:
        score = _MAD_SCALE * abs(deviation) / scale
        return {
            "reference": center, "deviation": deviation, "scale": scale, "score": score,
            "candidate": score > _MAD_THRESHOLD,
            "threshold": {"value": _MAD_THRESHOLD, "basis": "exploratory NIST/SEMATECH modified-z rule: 0.67448975*|x-leave-one-out median|/MAD > 3.5; not a calibrated p-value"},
        }
    spacing = _unique_spacing(references)
    multiple = abs(deviation) / spacing if spacing and spacing > 0 else None
    return {
        "reference": center, "deviation": deviation, "scale": 0.0, "score": multiple,
        "candidate": deviation != 0,
        "fallback": "raw_deviation_and_unique_spacing",
        "unique_spacing": spacing,
        "unique_multiple": multiple,
        "threshold": {"value": 0.0, "basis": "MAD=0 fallback flags a finite nonzero raw deviation from the leave-one-out median; unique_multiple=|deviation|/minimum positive spacing of distinct references when defined; exploratory, no infinite z-score and no p-value"},
    }


def _quality_flags(row: Mapping[str, Any]) -> List[Dict[str, Any]]:
    flags = []
    for key, value in row.items():
        normalized = _norm(key)
        if any(word in normalized for word in ("quality", "qc", "status", "valid", "error", "flag")):
            if isinstance(value, str) and _QUALITY_BAD_RE.search(value):
                flags.append({"column": str(key), "value": value})
            elif value is False and "valid" in normalized:
                flags.append({"column": str(key), "value": value})
    return flags


def _is_categorical(values: Sequence[Any]) -> bool:
    present = [value for value in values if not _missing(value)]
    if not present:
        return False
    numeric = sum(_finite_float(value) is not None for value in present)
    return numeric < len(present) or len({_json_key(value) for value in present}) <= min(30, max(2, len(present) // 2))


def _entity(table: Mapping[str, Any], row: Mapping[str, Any], index: int, metric: Optional[str] = None) -> Dict[str, Any]:
    result = {
        "table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet"),
        "row_index_zero_based": index, "row_ref": _row_ref(table, index), "values": _safe(row),
    }
    if metric is not None:
        result["metric"] = metric
    return result


def _declared_rule_scope(rules: Any) -> Tuple[set, bool]:
    if isinstance(rules, Mapping):
        operations = rules.get("operations", [rules] if "operation" in rules else [])
    else:
        operations = rules
    if isinstance(operations, Mapping):
        operations = [operations]
    if not isinstance(operations, list):
        return set(), False
    names = set()
    has_curve_model = False
    for item in operations:
        if not isinstance(item, Mapping):
            continue
        if item.get("operation") == "fit_declared_model":
            has_curve_model = True
        for key in ("value_column", "response_header", "uncertainty_header", "x_column_family", "control_header_family", "numerator_column", "denominator_column", "reported_column"):
            if isinstance(item.get(key), str):
                names.add(_norm(item[key]))
        for metric in item.get("metrics", []) if isinstance(item.get("metrics"), list) else []:
            if isinstance(metric, Mapping):
                for role in ("name", "value_column", "numerator_column", "denominator_column"):
                    if isinstance(metric.get(role), str):
                        names.add(_norm(metric[role]))
            elif isinstance(metric, str):
                names.add(_norm(metric))
    return names, bool(operations) and not has_curve_model


def _in_declared_metric_scope(name: Any, specified: set) -> bool:
    if not specified:
        return True
    text = re.sub(r"__\d+$", "", str(name).strip())
    without_unit = re.split(r"[\(\[\{]", text, maxsplit=1)[0].strip()
    return _norm(text) in specified or _norm(without_unit) in specified


class _Analysis:
    def __init__(self, data: Mapping[str, Any], case: Mapping[str, Any], work_dir: Path) -> None:
        self.data = data
        self._declared_metric_names, self._declared_tabular_only = _declared_rule_scope(case.get("analysis_rules_v1", case.get("analysis_rules")))
        self.case = case
        self.work_dir = work_dir
        self.tables: List[Dict[str, Any]] = []
        self.result: Dict[str, Any] = {
            "table_summaries": [], "facts": [], "outlier_candidates": [], "trend_candidates": [],
            "curve_analysis": {"groups": [], "coverage": {}},
            "computational_results": {"runs": [], "paired_comparison": [], "limits": []},
            "source_anomalies": [], "issues": [], "methods": [], "artifacts": [],
            "coverage": {"tables": {}, "series": {}, "records": {}, "limitations": []},
        }
        self._fact_counter = 0
        self._outlier_counter = 0
        self._trend_counter = 0
        self._issue_counter = 0
        self._source_counter = 0
        self._method_names = set()
        self._artifact_set = set()
        self._limitations = set()
        self._loo_work = 0

    def fact(self, kind: str, metric: str, entity: Any, value: Any, unit: Any, method: str, evidence: Sequence[Any], uncertainty: str) -> Dict[str, Any]:
        self._fact_counter += 1
        item = {
            "id": "F%04d" % self._fact_counter, "kind": kind, "metric": str(metric),
            "entity": _safe(entity), "value": _safe(value),
            "unit": str(unit) if unit not in (None, "") else None,
            "method": method, "evidence": [str(item) for item in evidence if item not in (None, "")],
            "uncertainty": uncertainty,
        }
        self.result["facts"].append(item)
        return item

    def issue(self, kind: str, message: str, path: Any = None, **details: Any) -> Dict[str, Any]:
        self._issue_counter += 1
        item = {"id": "I%04d" % self._issue_counter, "kind": kind, "message": message}
        if path not in (None, ""):
            item["path"] = str(path)
        if details:
            item["details"] = _safe(details)
        self.result["issues"].append(item)
        return item

    def limitation(self, text: str) -> None:
        if text:
            self._limitations.add(text)

    def method(self, name: str, algorithm: str, limitations: str) -> None:
        if name not in self._method_names:
            self._method_names.add(name)
            self.result["methods"].append({"name": name, "algorithm": algorithm, "limitations": limitations})

    def artifact(self, path: Path) -> None:
        try:
            relative = path.resolve().relative_to(self.work_dir.resolve()).as_posix()
        except (OSError, ValueError):
            self.issue("artifact_path_error", "Generated artifact is outside work_dir and was not registered.", path)
            return
        if relative not in self._artifact_set:
            self._artifact_set.add(relative)
            self.result["artifacts"].append(relative)

    def source_anomaly(self, kind: str, path: Any, evidence: Sequence[Any], status: str, **details: Any) -> Dict[str, Any]:
        if kind == "formula_cache_missing":
            for existing in self.result["source_anomalies"]:
                if existing.get("kind") == kind and existing.get("path") == str(path) and existing.get("sheet") == details.get("sheet") and existing.get("cell") == details.get("cell"):
                    view = str(details.get("audit", "reader formula_cells audit"))
                    views = list(existing.get("audit_views", []))
                    if view not in views:
                        views.append(view)
                    existing["audit_views"] = views
                    existing["evidence"] = sorted(set(existing.get("evidence", [])) | {str(item) for item in evidence})
                    return existing
        self._source_counter += 1
        item = {"id": "S%04d" % self._source_counter, "kind": kind, "path": str(path), "status": status, "evidence": [str(item) for item in evidence]}
        item.update(_safe(details))
        if kind == "formula_cache_missing":
            item["audit_views"] = [str(details.get("audit", "reader formula_cells audit"))]
        self.result["source_anomalies"].append(item)
        return item

    def prepare_tables(self) -> None:
        tables = self.data.get("tables", []) if isinstance(self.data.get("tables", []), list) else []
        prepared = []
        for position, original in enumerate(tables):
            if not isinstance(original, Mapping):
                self.issue("invalid_table", "Skipped a table that is not an object.", details={"position": position})
                continue
            table = dict(original)
            columns = table.get("columns", []) if isinstance(table.get("columns", []), list) else []
            names = [_column_name(column, index) for index, column in enumerate(columns)]
            raw_rows = table.get("rows", []) if isinstance(table.get("rows", []), list) else []
            rows = [_row_mapping(table, row, names) for row in raw_rows]
            table["_names"] = names
            table["_rows"] = rows
            table["_row_count"] = len(rows)
            table["_position"] = position
            prepared.append(table)
        self.tables = sorted(prepared, key=lambda table: (str(table.get("path", "")), str(table.get("sheet", "")), str(table.get("id", "")), table["_position"]))

    def summarize_tables(self) -> None:
        self.method("column_distribution", "Non-missing values are counted from actual parsed rows; finite numeric values use mean, sample SD (n-1), linear-quantile median/IQR and unscaled median absolute deviation.", "Text-to-number conversion is conservative; unsupported dates and non-finite numbers are not treated as measurements.")
        for table in self.tables:
            names = table["_names"]
            rows = table["_rows"]
            columns = []
            for index, name in enumerate(names):
                values = [_row_value(row, name, index) for row in rows]
                missing = sum(_missing(value) for value in values)
                present = [value for value in values if not _missing(value)]
                numeric = [_finite_float(value) for value in values]
                finite = [value for value in numeric if value is not None]
                nonfinite = sum((not _missing(value)) and _finite_float(value) is None and isinstance(value, (int, float)) and not isinstance(value, bool) for value in values)
                summary = {
                    "name": name, "n": len(present), "n_missing": missing,
                    "unique": len({_json_key(value) for value in present}), "count_numeric": len(finite),
                    **_stats(finite),
                    "n_nonfinite": nonfinite,
                    "unit": _metric_unit(self.case, name),
                    "evidence_locator": _table_range(table, index),
                }
                columns.append(_safe(summary))
                if finite:
                    self.fact("observed", name, {"table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet"), "column": name}, {key: summary[key] for key in ("n", "n_missing", "unique", "count_numeric", "min", "max", "mean", "sample_sd", "median", "iqr", "mad")}, summary["unit"], "column_distribution on finite numeric cells; n is all non-missing cells", [summary["evidence_locator"]], "Descriptive summaries do not establish uncertainty over batches, causes or population generalization.")
            table_summary = {
                "table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet"), "name": table.get("name"),
                "shape": [len(rows), len(names)], "columns": columns,
                "evidence_locator": _table_range(table) if names else _location(table.get("path"), table.get("sheet")),
                "headers": _safe(table.get("headers")), "metadata": _safe(table.get("metadata")),
            }
            self.result["table_summaries"].append(table_summary)
            self.fact("observed", "table_shape", {"table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet"), "name": table.get("name")}, {"rows": len(rows), "columns": len(names)}, None, "len(parsed rows), len(parsed columns)", [table_summary["evidence_locator"]], "Shape describes parsed material, not unprovided images or omitted sheets.")

    def _dimension_columns(self, table: Mapping[str, Any], metric_index: int, words: Sequence[str]) -> List[Tuple[int, str]]:
        result = []
        for index, name in enumerate(table["_names"]):
            if index == metric_index:
                continue
            normalized = _norm(name)
            if not any(_norm(word) in normalized for word in words):
                continue
            values = [_row_value(row, name, index) for row in table["_rows"]]
            if _is_categorical(values):
                unique = len({_json_key(value) for value in values if not _missing(value)})
                if 1 < unique < len(table["_rows"]):
                    result.append((index, name))
        return result

    def _condition_columns(self, table: Mapping[str, Any], metric_index: int) -> List[Tuple[int, str]]:
        candidates = self._dimension_columns(table, metric_index, _CONDITION_WORDS)
        if not candidates:
            return []
        viable = []
        for index, name in candidates:
            groups = Counter(_json_key(_row_value(row, name, index)) for row in table["_rows"] if not _missing(_row_value(row, name, index)))
            adequate = sum(count >= 4 for count in groups.values())
            priority = next((position for position, word in enumerate(_CONDITION_WORDS) if _norm(word) in _norm(name)), len(_CONDITION_WORDS))
            viable.append((-adequate, priority, -max(groups.values()), index, name))
        viable.sort()
        chosen = [(viable[0][3], viable[0][4])]
        for item in viable[1:]:
            if len(chosen) >= 2 or item[0] == 0:
                continue
            combined = Counter(_json_key([_row_value(row, chosen[0][1], chosen[0][0]), _row_value(row, item[4], item[3])]) for row in table["_rows"])
            if any(count >= 4 for count in combined.values()):
                chosen.append((item[3], item[4]))
        return chosen

    def screen_table_outliers(self, table: Mapping[str, Any]) -> None:
        rows = table["_rows"]
        if not rows:
            return
        self.method("leave_one_out_mad", "Each numeric cell is screened against other rows in its selected condition/batch/channel context; the global column is used only when no usable context exists. Modified z uses unscaled MAD and threshold 3.5; MAD=0 reports raw deviation and a distinct-reference spacing multiple.", "Exploratory screening only; no p-value, calibrated false-positive rate, deletion or causal inference. Contexts with fewer than four rows are marked insufficient.")
        quality_seen = set()
        for row_index, row in enumerate(rows):
            flags = _quality_flags(row)
            if not flags:
                continue
            for metric_index, metric in enumerate(table["_names"]):
                observed = _finite_float(_row_value(row, metric, metric_index))
                if observed is None:
                    continue
                key = (row_index, metric_index)
                if key in quality_seen:
                    continue
                quality_seen.add(key)
                self._add_outlier(table, row, row_index, metric, observed, {"reference": None, "deviation": None, "scale": None, "score": None, "threshold": {"value": None, "basis": "explicit source quality/status conflict"}}, status="quality_violation", method="Explicit quality/status field conflict; no statistical deletion", extra={"quality_flags": flags, "quality_override": True})
        for metric_index, metric in enumerate(table["_names"]):
            if metric_index in table.get("_abscissa_columns", set()) or metric_index in table.get("_axis_unresolved_columns", set()):
                continue
            if not _in_declared_metric_scope(metric, self._declared_metric_names):
                table.setdefault("_metric_scope_skips", []).append(metric)
                continue
            numeric = [(index, _finite_float(_row_value(row, metric, metric_index))) for index, row in enumerate(rows)]
            numeric = [(index, value) for index, value in numeric if value is not None]
            if len(numeric) < 2 or _is_identifier_name(metric):
                continue
            condition_columns = self._condition_columns(table, metric_index)
            groups: Dict[Tuple[str, ...], List[Tuple[int, float]]] = defaultdict(list)
            for row_index, value in numeric:
                key = tuple(_json_key(_row_value(rows[row_index], name, index)) for index, name in condition_columns)
                groups[key].append((row_index, value))
            if len(numeric) < 4:
                self.issue("outlier_reference_n_insufficient", "Fewer than four finite values; no leave-one-out MAD screening for this metric.", table.get("path"), table_id=table.get("id"), metric=metric, n=len(numeric))
                continue
            for key, members in sorted(groups.items(), key=lambda item: item[0]):
                if len(members) < 4:
                    self.issue("outlier_reference_n_insufficient", "Condition group has fewer than four finite values; retained but not screened by MAD.", table.get("path"), table_id=table.get("id"), metric=metric, condition_columns=[name for _, name in condition_columns], condition_values=list(key), n=len(members))
                    continue
                work = len(members) * (len(members) - 1)
                if self._loo_work + work > _MAX_LOO_WORK:
                    self.issue("outlier_screen_bounded", "Leave-one-out work budget reached; this metric/group retains summaries but no additional pairwise candidate screen.", table.get("path"), table_id=table.get("id"), metric=metric, n=len(members), requested_pair_work=work, budget=_MAX_LOO_WORK)
                    continue
                self._loo_work += work
                for row_index, observed in members:
                    references = [value for other_index, value in members if other_index != row_index]
                    deviation = _robust_deviation(observed, references)
                    flags = _quality_flags(rows[row_index])
                    if flags:
                        continue
                    if deviation["candidate"]:
                        status = "candidate_for_review" if deviation.get("fallback") else "outlier_supported"
                        self._add_outlier(table, rows[row_index], row_index, metric, observed, deviation, status=status, method="Conditional leave-one-out median/MAD exploratory screen", extra={"condition_columns": [name for _, name in condition_columns], "condition_values": list(key), "reference_n": len(references)})

    def _add_outlier(self, table: Mapping[str, Any], row: Mapping[str, Any], row_index: int, metric: str, observed: Any, deviation: Mapping[str, Any], status: str, method: str, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        self._outlier_counter += 1
        entity = _entity(table, row, row_index, metric)
        unit_metric = metric.replace(" [median-polish residual]", "")
        item = {
            "id": "O%04d" % self._outlier_counter, "entity": entity, "metric": metric,
            "observed": observed, "reference": deviation.get("reference"), "deviation": deviation.get("deviation"),
            "scale": deviation.get("scale"), "score": deviation.get("score"), "threshold": _safe(deviation.get("threshold")),
            "evidence": [_table_range(table, table["_names"].index(metric), row_index) if metric in table["_names"] else _location(table.get("path"), table.get("sheet"), _row_ref(table, row_index))],
            "status": status, "inclusion": "retained", "unit": _metric_unit(self.case, unit_metric), "method": method,
        }
        for key in ("fallback", "unique_spacing", "unique_multiple"):
            if key in deviation:
                item[key] = deviation[key]
        if deviation.get("fallback"):
            item["deviation"] = deviation.get("deviation")
        if extra:
            item.update(_safe(extra))
        self.result["outlier_candidates"].append(item)
        self.fact("derived" if status != "quality_violation" else "observed", "outlier_screen:" + metric, entity, {key: item.get(key) for key in ("id", "observed", "reference", "deviation", "scale", "score", "threshold", "status", "inclusion")}, item["unit"], method, item["evidence"], "A quality conflict or robust deviation does not establish its physical cause; the observation remains included.")
        return item

    def median_polish_table(self, table: Mapping[str, Any], max_metrics: int = 6) -> None:
        numeric_indices = [index for index, name in enumerate(table["_names"]) if index not in table.get("_abscissa_columns", set()) and _in_declared_metric_scope(name, self._declared_metric_names) and sum(_finite_float(_row_value(row, name, index)) is not None for row in table["_rows"]) >= 4 and not _is_identifier_name(name)]
        if not numeric_indices:
            return
        condition_candidates = self._dimension_columns(table, -1, ("condition", "group", "treatment", "sample", "specimen", "time"))
        technical_candidates = self._dimension_columns(table, -1, _TECHNICAL_WORDS)
        if not condition_candidates or not technical_candidates:
            if condition_candidates or technical_candidates:
                self.issue("median_polish_unavailable", "Skipped median polish: no unconfounded two-dimensional condition-by-technical matrix was identifiable.", table.get("path"), table_id=table.get("id"))
                self.limitation("Median polish requires an identifiable condition-by-technical-repeat/time matrix; missing dimensions were not invented.")
            return
        condition = condition_candidates[0]
        technical = next((item for item in technical_candidates if item[0] != condition[0]), None)
        if technical is None:
            return
        used = 0
        for metric_index in numeric_indices:
            if used >= max_metrics:
                self.issue("median_polish_bounded", "Median-polish metric limit reached; remaining metrics retain their ordinary column screens.", table.get("path"), table_id=table.get("id"), max_metrics=max_metrics)
                break
            cells: Dict[Tuple[str, str], List[Tuple[int, float]]] = defaultdict(list)
            row_levels = set()
            column_levels = set()
            for row_index, row in enumerate(table["_rows"]):
                value = _finite_float(_row_value(row, table["_names"][metric_index], metric_index))
                row_value = _row_value(row, condition[1], condition[0])
                column_value = _row_value(row, technical[1], technical[0])
                if value is None or _missing(row_value) or _missing(column_value):
                    continue
                row_key = _json_key(row_value)
                column_key = _json_key(column_value)
                cells[(row_key, column_key)].append((row_index, value))
                row_levels.add(row_key)
                column_levels.add(column_key)
            if not (2 <= len(row_levels) <= 30 and 2 <= len(column_levels) <= 30):
                continue
            if any(len(members) != 1 for members in cells.values()):
                self.issue("median_polish_confounded", "Skipped a median-polish matrix because cells have missing or multiple observations; averaging would hide technical replication.", table.get("path"), table_id=table.get("id"), metric=table["_names"][metric_index], condition=condition[1], technical=technical[1])
                continue
            if len(cells) != len(row_levels) * len(column_levels):
                self.issue("median_polish_incomplete", "Skipped an incomplete condition-by-technical matrix.", table.get("path"), table_id=table.get("id"), metric=table["_names"][metric_index])
                continue
            matrix = [[cells[(row_key, column_key)][0] for column_key in sorted(column_levels)] for row_key in sorted(row_levels)]
            residuals, row_effects, column_effects, grand = _median_polish([[value for _, value in row] for row in matrix])
            flat_residuals = [value for row in residuals for value in row]
            used += 1
            self.method("conditional_median_polish", "Up to 20 additive median-polish iterations on one complete condition-by-technical matrix per metric; residuals are screened against leave-one-out residual median/MAD.", "Additive row and column effects can alias interaction or confounded batch/time sources; no cross-metric pooling or causal attribution.")
            self.fact("derived", "median_polish_effects:" + table["_names"][metric_index], {"table_id": table.get("id"), "condition": condition[1], "technical": technical[1]}, {"grand": grand, "row_effects": dict(zip(sorted(row_levels), row_effects)), "column_effects": dict(zip(sorted(column_levels), column_effects)), "iterations_max": 20}, _metric_unit(self.case, table["_names"][metric_index]), "two-way additive median polish", [_table_range(table, metric_index)], "Effects are descriptive and not independent of the conditioning design.")
            for i, row_key in enumerate(sorted(row_levels)):
                for j, column_key in enumerate(sorted(column_levels)):
                    row_index, _ = matrix[i][j]
                    observed = residuals[i][j]
                    references = [residuals[ii][jj] for ii in range(len(residuals)) for jj in range(len(residuals[ii])) if (ii, jj) != (i, j)]
                    deviation = _robust_deviation(observed, references)
                    if deviation["candidate"] and not _quality_flags(table["_rows"][row_index]):
                        self._add_outlier(table, table["_rows"][row_index], row_index, table["_names"][metric_index] + " [median-polish residual]", observed, deviation, "candidate_for_review", "Conditional median-polish residual leave-one-out MAD screen", {"raw_observed": matrix[i][j][1], "condition": row_key, "technical": column_key, "reference_n": len(references)})

    def trends_for_table(self, table: Mapping[str, Any]) -> None:
        rows = table["_rows"]
        if len(rows) < 3:
            return
        numeric_by_column = []
        for index, name in enumerate(table["_names"]):
            finite = [(row_index, _finite_float(_row_value(row, name, index))) for row_index, row in enumerate(rows)]
            numeric_by_column.append([(row_index, value) for row_index, value in finite if value is not None])
        x_index = next((index for index, name in enumerate(table["_names"]) if len(numeric_by_column[index]) >= 3 and (_norm(name) in {"x", "xvalue", "xvalues", "abscissa", "xaxis", "横坐标", "x轴"} or any(_norm(word) in _norm(name) for word in _X_WORDS)) and not _is_identifier_name(name)), None)
        if x_index is None:
            return
        self.method("descriptive_xy_trend", "Original-row-order first/last delta, range, ordinary-least-squares slope, Pearson r and tie-aware Spearman rank correlation are computed on finite pairs.", "No slope is a causal effect or calibrated significance claim; a fold index is never used as a time axis.")
        x_name = table["_names"][x_index]
        for metric_index, metric in enumerate(table["_names"]):
            if metric_index == x_index or _is_identifier_name(metric):
                continue
            pairs = []
            for row_index, y in numeric_by_column[metric_index]:
                x = _finite_float(_row_value(rows[row_index], x_name, x_index))
                if x is not None:
                    pairs.append((row_index, x, y))
            if len(pairs) < 3 or _is_fold_axis(table, x_index, metric_index):
                continue
            xs = [pair[1] for pair in pairs]
            ys = [pair[2] for pair in pairs]
            slope, intercept = _ols(xs, ys)
            delta = ys[-1] - ys[0]
            value = {
                "n": len(pairs), "x": x_name, "x_first": xs[0], "x_last": xs[-1], "x_min": min(xs), "x_max": max(xs),
                "y_first": ys[0], "y_last": ys[-1], "delta": delta, "min": min(ys), "max": max(ys),
                "xy_slope": slope, "intercept": intercept, "pearson_r": _correlation(xs, ys),
                "spearman_rho": _correlation(_rank(xs), _rank(ys)),
            }
            self._trend_counter += 1
            item = {
                "id": "T%04d" % self._trend_counter, "entity": {"table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet"), "rows_used_zero_based": [pair[0] for pair in pairs]},
                "metric": metric, "unit": _metric_unit(self.case, metric), "x_unit": _metric_unit(self.case, x_name),
                "range": {"x_min": min(xs), "x_max": max(xs), "y_min": min(ys), "y_max": max(ys)},
                **value, "method": "original-row-order delta + OLS xy slope + tie-aware Spearman rho",
                "evidence": [_table_range(table, x_index), _table_range(table, metric_index)], "status": "observed_descriptive",
                "slope_unit": (_metric_unit(self.case, metric) or "y") + "/(" + (_metric_unit(self.case, x_name) or "x") + ")" if _metric_unit(self.case, metric) and _metric_unit(self.case, x_name) else None,
            }
            self.result["trend_candidates"].append(item)
            self.fact("derived", "xy_trend:" + metric, item["entity"], value, item["unit"], item["method"], item["evidence"], "Descriptive association only; retained original row order and no stationarity, independence or causal assumption.")


def _is_identifier_name(name: str) -> bool:
    normalized = _norm(name)
    return normalized in {"id", "index", "row", "rowid", "sampleid", "fold", "foldid", "split", "splitid", "repeat", "replicate"}


def _is_fold_axis(table: Mapping[str, Any], x_index: int, metric_index: int) -> bool:
    names = [_norm(name) for name in table["_names"]]
    fold_context = any(name in {"fold", "foldid", "split", "splitid", "cvfold"} for name in names)
    estimator_context = any("estimator" in name or "model" in name or "classifier" in name for name in names)
    metric_context = any(word in _norm(table["_names"][metric_index]) for word in ("accuracy", "correct", "score", "loss", "error"))
    return fold_context and (estimator_context or metric_context)


def _median_polish(matrix: Sequence[Sequence[float]]) -> Tuple[List[List[float]], List[float], List[float], float]:
    residual = [list(row) for row in matrix]
    row_effects = [0.0] * len(residual)
    column_effects = [0.0] * len(residual[0])
    grand = 0.0
    for _ in range(20):
        previous = (list(row_effects), list(column_effects), grand)
        for i, row in enumerate(residual):
            effect = _median(row) or 0.0
            row_effects[i] += effect
            residual[i] = [value - effect for value in row]
        column_medians = [_median([residual[i][j] for i in range(len(residual))]) or 0.0 for j in range(len(column_effects))]
        center = _median(column_medians) or 0.0
        grand += center
        column_medians = [value - center for value in column_medians]
        for j, effect in enumerate(column_medians):
            column_effects[j] += effect
            for i in range(len(residual)):
                residual[i][j] -= effect
        change = max([abs(row_effects[i] - previous[0][i]) for i in range(len(row_effects))] + [abs(column_effects[j] - previous[1][j]) for j in range(len(column_effects))] + [abs(grand - previous[2])])
        if change <= 1e-12 * max(1.0, abs(grand)):
            break
    return residual, row_effects, column_effects, grand


_METADATA_ROLE_ALIASES = {
    "source_file": {"sourcefile", "sourcefilename", "sourcepath", "filepath", "filename", "file", "basename", "path"},
    "sample_id": {"sampleid", "specimenid", "sample", "specimen"},
    "trace_id": {"traceid", "trace"},
    "run_id": {"runid", "run"},
    "curve_id": {"curveid", "curve"},
    "comparison_group": {"comparison_group", "comparisongroup", "comparison_group_id"},
    "condition": {"condition", "experimental_condition", "group"},
    "treatment": {"treatment"},
    "batch": {"batch", "batchid", "lot", "lotid"},
    "channel": {"channel", "channelid"},
    "instrument": {"instrument", "instrumentid", "device", "deviceid"},
    "x_unit": {"xunit", "x_unit", "indexunit", "abscissaunit", "timeunit", "displacementunit"},
    "y_unit": {"yunit", "y_unit", "responseunit", "signalunit"},
    "mat_variable": {"matvariable", "matvar", "arrayvariable", "variablename"},
    "mat_column": {"matcolumn", "matcol", "arraycolumn", "matrixcolumn"},
    "mat_column_base": {"matcolumnbase", "columnindexbase", "arraycolumnbase"},
    "axis_column": {"axis_column", "axiscolumn", "xcolumn", "abscissacolumn", "gridcolumn", "indexcolumn", "timecolumn", "displacementcolumn"},
    "response_column": {"responsecolumn", "ycolumn", "signalcolumn"},
    "response_metric": {"responsemetric", "ymetric", "metric", "metricname", "measurementname", "responsename"},
    "x_metric": {"xmetric", "axisname", "abscissaname", "xname"},
}
_METADATA_ID_ROLES = ("sample_id", "trace_id", "run_id", "curve_id")
_COMPARISON_ROLES = ("comparison_group", "condition", "treatment")
_TECHNICAL_METADATA_ROLES = ("batch", "channel", "instrument")


def _metadata_role(name: Any) -> Optional[str]:
    normalized = str(name).strip().lower().replace(" ", "_")
    compact = _norm(name)
    for role, aliases in _METADATA_ROLE_ALIASES.items():
        if normalized in aliases or compact in {_norm(alias) for alias in aliases}:
            return role
    return None


def _metadata_evidence(table: Mapping[str, Any], row_index: int) -> str:
    ref = _row_ref(table, row_index)
    sheet = str(table.get("sheet") or "")
    path = str(table.get("path") or "<unknown-path>")
    if sheet and ref.startswith(sheet + "!"):
        return path + "#" + ref
    if ref.startswith(str(table.get("path")) + "::"):
        return ref
    return _location(path, sheet, ref)


def _merge_source_curve_mappings(self: _Analysis) -> None:
    supplied = self.data.get("source_curve_mappings", [])
    if isinstance(supplied, Mapping):
        supplied = supplied.get("mappings", [])
    if not isinstance(supplied, list):
        return
    for mapping_index, mapping in enumerate(supplied):
        if not isinstance(mapping, Mapping):
            continue
        status = str(mapping.get("binding_status", "")).lower()
        if any(word in status for word in ("ambiguous", "conflict", "invalid", "rejected")):
            continue
        source_file = mapping.get("file")
        variable = mapping.get("variable")
        sample_id = mapping.get("sampleID")
        columns = mapping.get("column") if isinstance(mapping.get("column"), list) else [mapping.get("column")]
        valid_sample = bool(sample_id.strip()) if isinstance(sample_id, str) else isinstance(sample_id, (int, float)) and not isinstance(sample_id, bool) and _finite_float(sample_id) is not None
        if not isinstance(source_file, str) or not source_file.strip() or not isinstance(variable, str) or not variable.strip() or not valid_sample:
            continue
        if not columns or any(isinstance(column, bool) or not isinstance(column, int) or column < 1 for column in columns):
            continue
        declaration = mapping.get("declaration") if isinstance(mapping.get("declaration"), Mapping) else {}
        line = declaration.get("line", mapping_index + 1)
        end_line = declaration.get("end_line", line)
        evidence = "%s#line=%s-%s" % (mapping.get("source_path", "source_curve_mappings"), line, end_line)
        for column in columns:
            fields = {"source_file": source_file, "mat_variable": variable, "mat_column": column, "mat_column_base": 1, "sample_id": _safe(sample_id)}
            if mapping.get("group_status") == "explicit_constant" and mapping.get("group") not in (None, ""):
                fields["comparison_group"] = _safe(mapping["group"])
            for source_key, role in (("x_unit", "x_unit"), ("y_unit", "y_unit"), ("x_metric", "x_metric"), ("response_metric", "response_metric"), ("batch", "batch"), ("channel", "channel"), ("instrument", "instrument")):
                if mapping.get(source_key) not in (None, ""):
                    fields[role] = _safe(mapping[source_key])
            record = {
                "fields": fields, "raw_fields": {"source_curve_mapping": _safe(mapping)}, "table_id": "source_curve_mappings[%d]" % mapping_index,
                "path": mapping.get("source_path"), "sheet": None, "row_index": mapping_index,
                "row_ref": "line=%s-%s,column=%s" % (line, end_line, column), "evidence": evidence + ",column=" + str(column),
                "identifiers": {"sample_id": _safe(sample_id)}, "declaration": _safe(declaration), "mapping_origin": "source_curve_mappings",
            }
            self._metadata_records.append(record)
            self._metadata_by_id[_json_key(_safe(sample_id))].append(record)


def _prepare_source_metadata(self: _Analysis) -> None:
    self._metadata_records = []
    self._metadata_table_ids = set()
    self._metadata_by_id = defaultdict(list)
    self._source_join_issues = []
    self._table_source_matches = {}
    self._metadata_cache_checksum = None
    for table in self.tables:
        roles = {index: _metadata_role(name) for index, name in enumerate(table["_names"])}
        role_names = {role for role in roles.values() if role}
        supports_join = bool(role_names & (set(_METADATA_ID_ROLES) | {"source_file"}))
        supports_metadata = bool(role_names & (set(_COMPARISON_ROLES) | set(_TECHNICAL_METADATA_ROLES) | {"x_unit", "y_unit", "mat_variable", "mat_column", "axis_column", "response_column", "response_metric", "x_metric"}))
        if not supports_join or not supports_metadata:
            continue
        has_measurement_series = any((_explicit_x_header(name) is not None or _response_header_interpretable(name)) and sum(_finite_float(_row_value(row, name, index)) is not None for row in table["_rows"]) >= _MIN_CURVE_POINTS for index, name in enumerate(table["_names"]))
        if has_measurement_series:
            continue
        self._metadata_table_ids.add(table.get("id"))
        for row_index, row in enumerate(table["_rows"]):
            fields = {}
            raw_fields = {}
            for index, role in roles.items():
                if not role:
                    continue
                value = _row_value(row, table["_names"][index], index)
                if _missing(value):
                    continue
                raw_fields[table["_names"][index]] = _safe(value)
                if role in fields and _json_key(fields[role]) != _json_key(_safe(value)):
                    fields[role] = None
                    raw_fields["conflicting_" + role] = True
                else:
                    fields[role] = _safe(value)
            identifiers = {role: fields.get(role) for role in _METADATA_ID_ROLES if fields.get(role) not in (None, "")}
            if fields.get("source_file") in (None, "") and not identifiers:
                continue
            record = {
                "fields": fields, "raw_fields": raw_fields, "table_id": table.get("id"),
                "path": table.get("path"), "sheet": table.get("sheet"), "row_index": row_index,
                "row_ref": _row_ref(table, row_index), "evidence": _metadata_evidence(table, row_index),
                "identifiers": identifiers,
            }
            self._metadata_records.append(record)
            for value in set(identifiers.values()):
                self._metadata_by_id[_json_key(value)].append(record)
    _merge_source_curve_mappings(self)
    if self._metadata_records:
        self.method("exact_source_metadata_join", "Metadata is joined only by complete unique source path/source filename, explicit matrix variable+column coordinates, or complete unique sample/trace/run/curve identifiers. Technical contexts stay separate from comparison groups.", "No fuzzy, order, timestamp or response-shape matching is used. Ambiguous or unmatched traces retain descriptive features and never inherit another row's group or unit.")


def _source_join_issue(self: _Analysis, kind: str, message: str, **details: Any) -> None:
    issue = self.issue(kind, message, **details)
    if hasattr(self, "_source_join_issues"):
        self._source_join_issues.append(issue)


def _normalized_path_text(value: Any) -> str:
    import os
    text = str(value).strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return os.path.normpath(text)


def _source_path_matches(metadata_path: Any, source_path: Any) -> bool:
    if metadata_path in (None, "") or source_path in (None, ""):
        return False
    metadata_text = _normalized_path_text(metadata_path)
    source_text = _normalized_path_text(source_path)
    if metadata_text == source_text:
        return True
    if "/" not in metadata_text:
        return metadata_text == source_text.rsplit("/", 1)[-1]
    return source_text.endswith("/" + metadata_text.lstrip("./"))


def _table_source_matches(self: _Analysis, table: Mapping[str, Any]) -> List[Dict[str, Any]]:
    key = str(table.get("id"))
    if not hasattr(self, "_table_source_ambiguities"):
        self._table_source_ambiguities = {}
    if key not in self._table_source_matches:
        matches = [record for record in self._metadata_records if _source_path_matches(record["fields"].get("source_file"), table.get("path")) and str(record.get("table_id")) != str(table.get("id"))]
        ambiguous = []
        unique = []
        for record in matches:
            source_tables = [candidate.get("id") for candidate in self.tables if candidate.get("id") not in self._metadata_table_ids and _source_path_matches(record["fields"].get("source_file"), candidate.get("path"))]
            if len(set(source_tables)) != 1:
                ambiguous.append(record)
            else:
                unique.append(record)
        if ambiguous:
            self._table_source_ambiguities[key] = ambiguous
        self._table_source_matches[key] = sorted(unique, key=lambda record: (str(record["path"]), str(record["sheet"]), record["row_index"], record["table_id"]))
    return self._table_source_matches[key]


def _explicit_x_header(name: Any) -> Optional[Dict[str, str]]:
    text = str(name).strip()
    base = re.sub(r"\([^)]*\)|\[[^]]*\]", "", text).strip()
    normalized = _norm(base)
    if normalized in {"x", "xvalue", "xvalues", "xaxis", "abscissa", "横坐标", "x轴"}:
        return {"identity": "x", "status": "header_named_abscissa", "basis": "explicit x/abscissa header"}
    if normalized in {"index", "indexau", "indexa_u", "indexarb", "indexarbitrary", "indexunit", "indexunits", "indexordinal"}:
        return {"identity": "index", "status": "unconfirmed_shared_abscissa", "basis": "explicit index header; index semantics are not assumed to be a physical abscissa"}
    if "time" in normalized or "时间" in base:
        return {"identity": "time", "status": "header_named_abscissa", "basis": "explicit time/time_index header"}
    for word, identity in (("displacement", "displacement"), ("deflection", "deflection"), ("strain", "strain"), ("wavelength", "wavelength"), ("wavenumber", "wavenumber"), ("frequency", "frequency"), ("distance", "distance"), ("position", "position"), ("位移", "displacement"), ("应变", "strain"), ("波长", "wavelength")):
        if word in base.lower() or word in base:
            return {"identity": identity, "status": "header_named_abscissa", "basis": "explicit abscissa-meaning header token: " + identity}
    if normalized.startswith("xaxis") or normalized.startswith("xindex"):
        return {"identity": "x", "status": "header_named_abscissa", "basis": "explicit x-axis/x-index header"}
    return None


def _trace_column_excluded(name: Any) -> bool:
    normalized = _norm(name)
    if _is_identifier_name(str(name)):
        return True
    if any(word in normalized for word in ("quality", "qcflag", "qcstatus", "validflag", "errorflag", "sourcefile", "filepath", "filename", "comparison_group", "sampleid", "traceid", "curveid", "runid", "instrumentid", "batchid", "channelid", "matcolumn", "matvariable")):
        return True
    return normalized in {"status", "valid", "error", "flag", "batch", "channel", "instrument", "condition", "treatment", "comparison_group", "repeat", "replicate"}


def _response_header_interpretable(name: Any) -> bool:
    base = re.sub(r"\([^)]*\)|\[[^]]*\]", "", str(name)).strip()
    normalized = _norm(base)
    return normalized in {"y", "yvalue", "yvalues", "response", "responseau", "responsevalues", "signal", "signalau", "measurement", "measuredvalue", "intensity"} or normalized.startswith("response") or normalized.startswith("signal")


def _uniform_grid(values: Sequence[Optional[float]]) -> bool:
    finite = [value for value in values if value is not None]
    if len(finite) != len(values) or len(finite) < _MIN_CURVE_POINTS:
        return False
    differences = [right - left for left, right in zip(finite, finite[1:])]
    center = _median(differences)
    if center is None or center == 0:
        return False
    scale = max(abs(center), 1e-12)
    return all(math.isclose(value, center, rel_tol=1e-8, abs_tol=1e-10 * scale) for value in differences)


def _trace_metric_key(variable: Any, source: Mapping[str, Any], explicit_metric: Any = None) -> str:
    if explicit_metric not in (None, ""):
        return "metadata:" + _norm(explicit_metric)
    text = str(variable).split("|", 1)[0].strip()
    text = re.sub(r"\([^)]*\)|\[[^]]*\]", "", text).strip()
    array_variable = source.get("array_variable") or (source.get("metadata", {}) if isinstance(source.get("metadata"), Mapping) else {}).get("variable")
    if array_variable:
        return "array:" + _norm(array_variable) + ":response"
    for role in ("sample", "specimen", "trace", "curve", "replicate", "repeat", "run", "test", "trial"):
        text = re.sub(r"\b" + role + r"\b", "", text, flags=re.I)
    family = re.sub(r"[\d\W_]+", "", text, flags=re.UNICODE).casefold()
    return "family:" + (family or "response")


def _axis_reference_index(self: _Analysis, table: Mapping[str, Any], records: Sequence[Mapping[str, Any]], role: str) -> Optional[int]:
    references = [record["fields"].get(role) for record in records if record["fields"].get(role) not in (None, "")]
    if not references:
        return None
    resolved = []
    for reference in references:
        exact = [index for index, name in enumerate(table["_names"]) if str(name).strip() == str(reference).strip()]
        if len(exact) == 1:
            resolved.append(exact[0])
            continue
        number = _finite_float(reference)
        array_hint = bool(table.get("_array_links"))
        metadata = table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}
        if number is not None and number.is_integer():
            base = int(_finite_float(table.get("_array_index_base", metadata.get("mat_index_base", 1 if array_hint else 0))) or 0)
            if array_hint:
                index = int(number) - base
                if 0 <= index < len(table["_names"]):
                    resolved.append(index)
    if resolved and len(set(resolved)) == 1:
        return resolved[0]
    _source_join_issue(self, "axis_reference_unresolved", "Metadata abscissa reference did not resolve to one unique source column.", table_id=table.get("id"), path=table.get("path"), role=role, references=_safe(references))
    return None


def _array_values(array: Mapping[str, Any]) -> Any:
    for key in ("values", "data", "array", "rows"):
        value = array.get(key)
        if isinstance(value, list):
            return value
    return None


def _trace_entity(source: Mapping[str, Any], variable: str) -> Dict[str, Any]:
    return {
        "table_id": source.get("id") or source.get("table_id"), "array_id": source.get("array_id"),
        "path": source.get("path"), "sheet": source.get("sheet"), "variable": variable,
        "row_indices_zero_based": source.get("row_indices"), "headers": _safe(source.get("headers")),
        "array_variable": source.get("array_variable"), "mat_index_base": source.get("index_base"), "source_metadata": _safe(source.get("metadata")),
    }


def _build_table_trace_group(self: _Analysis, table: Mapping[str, Any], array_hint: bool = False) -> List[Dict[str, Any]]:
    if self._declared_tabular_only and not array_hint:
        return []
    if table.get("id") in getattr(self, "_metadata_table_ids", set()):
        return []
    rows = table["_rows"]
    names = table["_names"]
    table["_abscissa_columns"] = set()
    if len(rows) < _MIN_CURVE_POINTS or not names:
        return []
    normalized_names = [_norm(name) for name in names]
    if _prediction_schema(table) is not None or (any(re.search(r"(?:ypred|pred|prediction|predicted)(?:class|label)?$", name) for name in normalized_names) and any("fold" in name or "split" in name for name in normalized_names)):
        return []
    numeric = []
    for index, name in enumerate(names):
        values = [_finite_float(_row_value(row, name, index)) for row in rows]
        numeric.append(values)
        if sum(value is not None for value in values) > _MAX_RAW_POINTS_PER_TRACE:
            self.issue("curve_points_bounded", "Trace exceeds the per-trace point bound; only the declared prefix is analyzed.", table.get("path"), table_id=table.get("id"), variable=name, limit=_MAX_RAW_POINTS_PER_TRACE)
    source_matches = _table_source_matches(self, table)
    x_index = _axis_reference_index(self, table, source_matches, "axis_column")
    x_info = None
    if x_index is not None:
        x_info = {"identity": "metadata_axis", "status": "metadata_explicit_abscissa", "basis": "unique explicit metadata axis_column source reference"}
    else:
        x_index = next((index for index, name in enumerate(names) if sum(value is not None for value in numeric[index]) >= _MIN_CURVE_POINTS and _explicit_x_header(name) is not None), None)
        if x_index is not None:
            x_info = _explicit_x_header(names[x_index])
    if x_index is None and array_hint:
        candidates = [index for index, name in enumerate(names) if sum(value is not None for value in numeric[index]) >= _MIN_CURVE_POINTS and not _trace_column_excluded(name)]
        remaining = [index for index in candidates[1:] if not _trace_column_excluded(names[index])]
        grid_flags = [_uniform_grid(numeric[index]) for index in candidates]
        if candidates and len(remaining) >= 2 and grid_flags[0] and not all(grid_flags):
            x_index = candidates[0]
            x_info = {"identity": "array_grid:" + str(table.get("id")), "status": "unconfirmed_shared_abscissa", "basis": "first linked-array column is a constant-step numeric grid shared by at least two remaining response candidates and not every numeric column is equally grid-like; no force/displacement semantics inferred"}
        elif candidates and len(remaining) >= 2 and all(grid_flags):
            self.issue("shared_abscissa_unconfirmed", "Every candidate column is a constant-step sequence, so no unique shared x was inferred; all columns remain responses on explicit ordinal indices.", table.get("path"), table_id=table.get("id"), variable=table.get("_array_variable"))
    table["_abscissa_columns"] = {x_index} if x_index is not None else set()
    entity_candidates = self._dimension_columns(table, -1, ("curve", "trace", "sample", "specimen", "material", "entity"))
    if x_index is not None and entity_candidates and not array_hint:
        entity_index, entity_name = entity_candidates[0]
        metric_indices = [index for index, name in enumerate(names) if index not in (x_index, entity_index) and sum(value is not None for value in numeric[index]) >= _MIN_CURVE_POINTS and not _trace_column_excluded(name)]
        groups = []
        for metric_index in metric_indices[:6]:
            traces = []
            entities = sorted({_json_key(_row_value(row, entity_name, entity_index)) for row in rows if not _missing(_row_value(row, entity_name, entity_index))})
            for entity_key in entities:
                row_indices = [index for index, row in enumerate(rows) if _json_key(_row_value(row, entity_name, entity_index)) == entity_key]
                if sum(numeric[metric_index][index] is not None for index in row_indices) < _MIN_CURVE_POINTS:
                    continue
                variable = names[metric_index] + " | " + entity_name + "=" + entity_key
                entity_value = json.loads(entity_key)
                traces.append({
                    "id": "", "variable": variable, "x": [numeric[x_index][index] for index in row_indices], "y": [numeric[metric_index][index] for index in row_indices],
                    "x_label": names[x_index], "y_label": names[metric_index], "x_unit": _metric_unit(self.case, names[x_index]), "y_unit": _metric_unit(self.case, names[metric_index]),
                    "index_base": 0, "evidence": [_table_range(table, x_index), _table_range(table, metric_index)],
                    "source_column_zero_based": metric_index, "source_id_candidates": [entity_value], "metric_key": _trace_metric_key(names[metric_index], table),
                    "abscissa": {**x_info, "unit": _metric_unit(self.case, names[x_index]), "source_column_zero_based": x_index},
                    "entity": {"table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet"), "variable": names[metric_index], "condition": {entity_name: entity_value}, "row_indices_zero_based": row_indices},
                })
            if traces:
                groups.append({"source": table, "traces": traces, "routing": "long-format entity/condition traces with actual x column", "x_mode": "shared_column_per_trace"})
        if groups:
            table["_response_trace_count"] = sum(len(group["traces"]) for group in groups)
            return groups
    y_indices = [index for index, name in enumerate(names) if index != x_index and sum(value is not None for value in numeric[index]) >= _MIN_CURVE_POINTS and not _trace_column_excluded(name)]
    response_axis = _axis_reference_index(self, table, source_matches, "response_column")
    if response_axis is not None:
        y_indices = [response_axis] if response_axis in y_indices else []
    if not y_indices:
        return []
    if array_hint and x_index is None and len(y_indices) <= 2 and response_axis is None:
        table["_axis_unresolved_columns"] = set(y_indices)
        self.issue("curve_abscissa_unresolved", "Linked array has too few potential response columns for defensible axis inference. No numeric column was promoted to a response trajectory and no axial outlier was generated; supply an explicit source/header axis mapping to resolve it.", table.get("path"), table_id=table.get("id"), array_variable=table.get("_array_variable"), candidate_columns=[names[index] for index in y_indices], source_ranges=[_table_range(table, index) for index in y_indices])
        return []
    if x_index is None and not array_hint:
        if len(y_indices) == 1 and not _response_header_interpretable(names[y_indices[0]]):
            return []
        if len(y_indices) > 1 and len({_trace_metric_key(names[index], table) for index in y_indices}) != 1:
            return []
    traces = []
    source_arrays = table.get("_array_ids", {})
    metadata = table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}
    index_base = int(_finite_float(table.get("_array_index_base", metadata.get("mat_index_base", 0))) or 0)
    array_variable = table.get("_array_variable", metadata.get("variable"))
    for index in y_indices:
        row_indices = list(range(min(len(rows), _MAX_RAW_POINTS_PER_TRACE)))
        variable = str(array_variable) + "::" + names[index] if array_hint and array_variable and _norm(array_variable) != _norm(names[index]) else names[index]
        if x_index is None:
            x = [float(position + index_base) for position in row_indices]
            x_label = "original array index (%d-based ordinal, not a measured abscissa)" % index_base if array_hint else "original row index (0-based ordinal, not a measured abscissa)"
            x_unit = None
            evidence = [_table_range(table, index)]
            x_mode = "original_array_index" if array_hint else "original_row_index"
            abscissa = {"identity": "ordinal_row:" + (str(table.get("id")) if array_hint else "generic"), "status": "unconfirmed_ordinal_not_experimental_abscissa", "basis": "no defensible measured common x was selected; original row/array order only", "unit": None, "source_column_zero_based": None}
        else:
            x = numeric[x_index][:len(row_indices)]
            x_label = names[x_index]
            x_unit = _metric_unit(self.case, names[x_index])
            evidence = [_table_range(table, x_index), _table_range(table, index)]
            x_mode = "metadata_explicit_abscissa" if x_info["status"] == "metadata_explicit_abscissa" else "unconfirmed_shared_grid_candidate" if x_info["status"] == "unconfirmed_shared_abscissa" and x_info["identity"].startswith("array_grid:") else "explicit_header_common_x"
            abscissa = {**x_info, "unit": x_unit, "source_column_zero_based": x_index}
        array_id = source_arrays.get(index, source_arrays.get("_default"))
        traces.append({
            "id": "", "variable": variable, "x": x, "y": numeric[index][:len(row_indices)],
            "x_label": x_label, "y_label": variable, "x_unit": x_unit, "y_unit": _metric_unit(self.case, names[index]),
            "array_id": array_id, "index_base": index_base, "evidence": evidence,
            "source_column_zero_based": index, "source_id_candidates": [re.sub(r"\([^)]*\)|\[[^]]*\]", "", names[index]).strip()],
            "metric_key": _trace_metric_key(variable, table), "abscissa": abscissa,
            "entity": _trace_entity({"id": table.get("id"), "array_id": array_id, "array_variable": array_variable, "index_base": index_base, "path": table.get("path"), "sheet": table.get("sheet"), "row_indices": row_indices, "headers": table.get("headers"), "metadata": metadata}, variable),
            "x_mode": x_mode,
        })
    table["_response_trace_count"] = len(traces)
    routing = "linked MATLAB/array response columns; shared grid candidate is explicitly unconfirmed" if array_hint else "source/header-routed response sequence candidates with common x removed"
    group = {"source": table, "traces": traces, "routing": routing, "x_mode": traces[0]["x_mode"]}
    if len({trace.get("y_unit") for trace in traces}) > 1:
        group["depth_block"] = "different explicit y units; cross-metric functional comparison is not meaningful"
    elif table.get("_array_mapping_ambiguous"):
        group["depth_block"] = "multiple array variables map ambiguously to this table; metric identity is unresolved"
    return [group]


def _source_checksum(self: _Analysis, table: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    inputs = self.data.get("inputs", [])
    items = inputs if isinstance(inputs, list) else [{"path": path, **(metadata if isinstance(metadata, Mapping) else {})} for path, metadata in inputs.items()] if isinstance(inputs, Mapping) else []
    matches = [item for item in items if isinstance(item, Mapping) and _normalized_path_text(item.get("path", "")) == _normalized_path_text(table.get("path", ""))]
    if len(matches) != 1:
        return None
    item = matches[0]
    for key in ("sha256", "hash", "checksum"):
        if item.get(key) not in (None, ""):
            return {"algorithm": "sha256" if key == "sha256" else "reader_declared_" + key, "value": item[key], "verification": "retained from reader input record; not recomputed here"}
    return None


def _mat_coordinate_matches(table: Mapping[str, Any], trace: Mapping[str, Any], records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    matches = []
    column = trace.get("source_column_zero_based")
    if column is None:
        return matches
    array_variable = trace["entity"].get("array_variable")
    for record in records:
        fields = record["fields"]
        if fields.get("mat_variable") not in (None, "") and array_variable and _norm(fields["mat_variable"]) != _norm(array_variable):
            continue
        reference = fields.get("mat_column")
        if reference in (None, ""):
            continue
        exact_header = _norm(reference).lstrip("column") == str(column + 1) and str(reference).strip().startswith("column_")
        number = _finite_float(reference)
        if number is not None and number.is_integer():
            base = _finite_float(fields.get("mat_column_base", trace.get("index_base", 0)))
            base = int(base if base is not None else 0)
            exact_index = int(number) - base == column
        else:
            exact_index = False
        if exact_header or exact_index:
            matches.append(record)
    return matches


def _best_trace_metadata(self: _Analysis, table: Mapping[str, Any], trace: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], str, str, List[Dict[str, Any]]]:
    inline_record = trace.get("inline_metadata_record")
    if inline_record is not None:
        return inline_record, "matched", "array_object_explicit_metadata", [inline_record]
    source_matches = _table_source_matches(self, table)
    global_ambiguities = getattr(self, "_table_source_ambiguities", {}).get(str(table.get("id")), [])
    if global_ambiguities:
        return None, "mapping_ambiguous", "metadata path/basename is not unique across all source tables", global_ambiguities
    coordinate_matches = _mat_coordinate_matches(table, trace, source_matches)
    if len(coordinate_matches) == 1:
        return coordinate_matches[0], "matched", "exact_source_file_mat_variable_mat_column", coordinate_matches
    if len(coordinate_matches) > 1:
        return None, "mapping_ambiguous", "multiple complete source matrix-coordinate matches", coordinate_matches
    if table.get("_response_trace_count", 1) == 1:
        applicable = [record for record in source_matches if record["fields"].get("mat_variable") in (None, "") or _norm(record["fields"]["mat_variable"]) == _norm(trace["entity"].get("array_variable"))]
        if len(applicable) == 1:
            return applicable[0], "matched", "exact_unique_source_file", applicable
        if len(applicable) > 1:
            return None, "mapping_ambiguous", "multiple metadata rows match the complete source filename", applicable
    candidates = set()
    metadata = trace["entity"].get("source_metadata")
    if isinstance(metadata, Mapping):
        for key, value in metadata.items():
            if _metadata_role(key) in _METADATA_ID_ROLES and not _missing(value):
                candidates.add(_json_key(_safe(value)))
    for value in trace.get("source_id_candidates", []):
        if not _missing(value):
            candidates.add(_json_key(_safe(value)))
    id_matches = []
    for candidate in sorted(candidates):
        for record in self._metadata_by_id.get(candidate, []):
            if record not in id_matches:
                id_matches.append(record)
    if len(id_matches) == 1:
        return id_matches[0], "matched", "complete_unique_trace_sample_run_curve_identifier", id_matches
    if len(id_matches) > 1:
        return None, "mapping_ambiguous", "identifier is complete but not unique across metadata rows", id_matches
    if source_matches and table.get("_response_trace_count", 1) > 1:
        return None, "mapping_unresolved", "source file matched metadata, but column/sample identity is unresolved without an exact matrix coordinate or complete unique ID", source_matches
    return None, "unmatched", "no complete unique source or identifier match", source_matches + id_matches


def _attach_trace_metadata(self: _Analysis, table: Mapping[str, Any], trace: MutableMapping[str, Any]) -> None:
    record, status, method, candidates = _best_trace_metadata(self, table, trace)
    fields = record["fields"] if record else {}
    trace["source_join_status"] = status
    trace["source_join_method"] = method if record else None
    trace["source_join_basis"] = method
    trace["metadata"] = _safe(fields) if record else None
    trace["metadata_provenance"] = {key: record[key] for key in ("table_id", "path", "sheet", "row_index", "row_ref", "evidence")} if record else None
    trace["source_join_candidates"] = [item["evidence"] for item in candidates[:20]] if status.startswith("mapping_") else []
    checksum = _source_checksum(self, table)
    trace["source_checksum"] = checksum
    representation = "representation:" + str(table.get("id")) + ":" + str(trace["entity"].get("array_variable") or "") + ":" + str(trace.get("source_column_zero_based", trace["variable"]))
    trace["representation_id"] = representation
    independent = None
    identity_basis = "unresolved_representation_identity"
    if record:
        for role in _METADATA_ID_ROLES:
            if role in record["identifiers"]:
                independent = "identifier:" + _json_key(record["identifiers"][role])
                identity_basis = "exact_metadata_" + role
                break
    if independent is None:
        independent = representation
        identity_basis = "unmatched_representations_kept_separate_not_proven_independent"
    trace["independent_unit_id"] = independent
    trace["independent_unit_basis"] = identity_basis
    trace["comparison_context"] = None
    if record:
        for role in _COMPARISON_ROLES:
            if fields.get(role) not in (None, ""):
                trace["comparison_context"] = {"basis": "explicit_metadata_" + role, "value": fields[role]}
                break
    trace["technical_context"] = {role: fields[role] for role in _TECHNICAL_METADATA_ROLES if fields.get(role) not in (None, "")}
    if fields.get("response_metric") not in (None, ""):
        trace["metric_key"] = _trace_metric_key(trace["variable"], table, fields["response_metric"])
    if fields.get("y_unit") not in (None, ""):
        previous = trace.get("y_unit")
        supplied = str(fields["y_unit"]).strip()
        if previous and previous.strip() != supplied:
            trace["unit_conflict"] = {"field": "y_unit", "header_unit": previous, "metadata_unit": supplied}
            _source_join_issue(self, "metadata_unit_conflict", "Header and joined metadata disagree on response units; functional comparison is blocked for this trace.", table_id=table.get("id"), trace_id=trace.get("id"), variable=trace["variable"], header_unit=previous, metadata_unit=supplied, metadata_evidence=record["evidence"])
        trace["y_unit"] = supplied
        trace["y_unit_provenance"] = record["evidence"]
    abscissa = dict(trace.get("abscissa", {}))
    if fields.get("x_unit") not in (None, ""):
        previous = abscissa.get("unit")
        supplied = str(fields["x_unit"]).strip()
        if previous and str(previous).strip() != supplied:
            trace["abscissa_unit_conflict"] = {"header_unit": previous, "metadata_unit": supplied}
            _source_join_issue(self, "metadata_unit_conflict", "Header and joined metadata disagree on abscissa units; cross-file functional comparison is separated.", table_id=table.get("id"), variable=trace["variable"], header_unit=previous, metadata_unit=supplied, metadata_evidence=record["evidence"])
        abscissa["unit"] = supplied
        abscissa["unit_provenance"] = record["evidence"]
    if fields.get("x_metric") not in (None, ""):
        abscissa["identity"] = "metadata_x:" + _norm(fields["x_metric"])
        abscissa["identity_provenance"] = record["evidence"]
    trace["abscissa"] = abscissa
    trace["x_unit"] = abscissa.get("unit")
    trace["x_label"] = str(trace.get("x_label")) + (" [unconfirmed shared abscissa]" if abscissa.get("status") == "unconfirmed_shared_abscissa" and "unconfirmed" not in str(trace.get("x_label")) else "")
    entity = trace["entity"]
    entity.update({
        "source_join_status": status, "source_join_method": trace["source_join_method"],
        "metadata": trace["metadata"], "metadata_provenance": trace["metadata_provenance"],
        "source_checksum": checksum, "source_raw_path": table.get("path"), "source_sheet": table.get("sheet"),
        "source_row_range": _table_range(table, trace.get("source_column_zero_based")) if trace.get("source_column_zero_based") is not None and table.get("_names") else trace.get("evidence", [None])[0] if trace.get("evidence") else None,
        "representation_id": representation, "independent_unit_id": independent, "independent_unit_basis": identity_basis,
    })


def _regroup_trace_candidates(self: _Analysis, candidates: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    orphan_summary: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for candidate_index, candidate in enumerate(candidates):
        table = candidate["source"]
        for trace in candidate["traces"]:
            _attach_trace_metadata(self, table, trace)
            orphan_key = (str(table.get("id")), trace["source_join_status"], trace["source_join_basis"])
            orphan_summary.setdefault(orphan_key, {"table": table, "count": 0, "evidence": trace["source_join_candidates"]})
            orphan_summary[orphan_key]["count"] += 1
            comparison = trace.get("comparison_context")
            if comparison:
                namespace = "metadata_comparison:" + comparison["basis"] + ":" + _json_key(comparison["value"])
                routing = "cross-source exact metadata comparison group"
            else:
                namespace = "source_%d_metric_family" % candidate_index
                routing = candidate.get("routing", "source-local compatible metric family")
            abscissa = trace.get("abscissa", {})
            key = (namespace, str(trace["metric_key"]), _json_key(trace.get("y_unit")), str(abscissa.get("identity")), _json_key(abscissa.get("unit")))
            bucket = buckets.setdefault(key, {"source": table, "traces": [], "routing": routing, "comparison_context": comparison, "metric_key": trace["metric_key"], "abscissa": abscissa, "source_tables": set(), "source_files": set(), "depth_block": candidate.get("depth_block")})
            bucket["traces"].append(trace)
            bucket["source_tables"].add(table.get("id"))
            bucket["source_files"].add(table.get("path"))
            if trace.get("unit_conflict") or trace.get("abscissa_unit_conflict"):
                bucket["depth_block"] = "header/metadata unit conflict; units cannot be reconciled by this module"
            if trace["source_join_status"] == "mapping_ambiguous" and comparison:
                bucket["depth_block"] = "ambiguous metadata mapping; no common comparison was forced"
    groups = []
    for bucket in buckets.values():
        bucket["traces"].sort(key=lambda trace: (_normalized_path_text(trace["entity"].get("path", "")), str(trace["entity"].get("table_id")), str(trace.get("source_column_zero_based", -1)), str(trace["variable"])))
    for key, bucket in sorted(buckets.items(), key=lambda item: item[0]):
        bucket["source_files"] = sorted(path for path in bucket["source_files"] if path not in (None, ""))
        bucket["source_tables"] = sorted(table_id for table_id in bucket["source_tables"] if table_id is not None)
        bucket["representation_count"] = len(bucket["traces"])
        bucket["independent_unit_count"] = len({trace["independent_unit_id"] for trace in bucket["traces"]})
        bucket["technical_contexts"] = sorted({json.dumps(_safe(trace["technical_context"]), ensure_ascii=False, sort_keys=True) for trace in bucket["traces"] if trace["technical_context"]})
        bucket["x_mode"] = bucket["traces"][0]["x_mode"]
        groups.append(bucket)
    compatibility = defaultdict(list)
    for group in groups:
        if group.get("comparison_context"):
            base = group["comparison_context"]
            compatibility[(base["basis"], _json_key(base["value"]), str(group["metric_key"]))].append(group)
    for (basis, value, metric_key), related in sorted(compatibility.items()):
        y_units = sorted({_json_key(trace.get("y_unit")) for group in related for trace in group["traces"]})
        x_identities = sorted({str(trace.get("abscissa", {}).get("identity")) for group in related for trace in group["traces"]})
        x_units = sorted({_json_key(trace.get("abscissa", {}).get("unit")) for group in related for trace in group["traces"]})
        if len(y_units) > 1 or len(x_identities) > 1 or len(x_units) > 1:
            _source_join_issue(self, "comparison_compatibility_separated", "A source-supported comparison label spans incompatible response units or abscissa semantics; subgroups remain separate and no shared depth band is forced.", comparison_basis=basis, comparison_value=json.loads(value), metric_key=metric_key, y_units=[json.loads(unit) for unit in y_units], abscissa_identities=x_identities, x_units=[json.loads(unit) for unit in x_units], representation_count=sum(group["representation_count"] for group in related))
    for (table_id, status, basis), item in sorted(orphan_summary.items()):
        if status == "matched":
            continue
        table = item["table"]
        _source_join_issue(self, "source_metadata_" + status, "Trace source-to-metadata mapping was not uniquely resolved; no comparison group, unit or duplicate identity was inherited.", table_id=table_id, path=table.get("path"), representation_count=item["count"], reason=basis, candidate_metadata_evidence=item["evidence"])
    return groups


def _build_direct_array_groups(self: _Analysis) -> List[Dict[str, Any]]:
    groups = []
    arrays = self.data.get("arrays", []) if isinstance(self.data.get("arrays", []), list) else []
    table_by_id = {str(table.get("id")): table for table in self.tables}
    for table in self.tables:
        table["_array_ids"] = {}
        table["_array_links"] = []
    for array in sorted((item for item in arrays if isinstance(item, Mapping)), key=lambda item: (str(item.get("table_id", "")), str(item.get("id", "")), str(item.get("variable", "")))):
        table_id = array.get("table_id")
        if table_id is not None and str(table_id) in table_by_id:
            table = table_by_id[str(table_id)]
            table["_array_links"].append(array)
            for index, name in enumerate(table["_names"]):
                if _norm(name) == _norm(array.get("variable", "")):
                    table["_array_ids"][index] = array.get("id")
            continue
        values = _array_values(array)
        if values is None:
            if any(word in _case_text(self.case).lower() for word in ("curve", "trace", "曲线")):
                self.issue("array_values_unavailable", "An array has no dereferenceable values or linked table; curve calculations cannot use its shape alone.", details={"array_id": array.get("id"), "variable": array.get("variable"), "shape": _safe(array.get("shape"))})
            continue
        columns = values
        if values and isinstance(values[0], list):
            width = max((len(row) for row in values if isinstance(row, list)), default=0)
            columns = [[row[index] if index < len(row) else None for row in values] for index in range(width)]
        elif not values or not isinstance(values, list):
            continue
        else:
            columns = [values]
        role_fields = {}
        for key, value in array.items():
            role = _metadata_role(key)
            if role and not _missing(value):
                role_fields[role] = _safe(value)
        references = next((array.get(key) for key in ("response_columns", "column_names", "trace_columns", "mat_columns") if isinstance(array.get(key), list)), None)
        external_support = [record for record in self._metadata_records if record["fields"].get("mat_column") not in (None, "") and _source_path_matches(record["fields"].get("source_file"), array.get("path"))]
        has_y_unit = role_fields.get("y_unit") not in (None, "") or any(record["fields"].get("y_unit") not in (None, "") for record in external_support)
        if references is None or not has_y_unit:
            self.issue("direct_array_trace_metadata_required", "Direct numeric array was not flattened into curves: explicit response-column references and linked response units are required.", array.get("path"), array_id=array.get("id"), variable=array.get("variable"), has_column_references=references is not None, has_y_unit=has_y_unit)
            continue
        if len(references) != len(columns):
            self.issue("direct_array_column_reference_mismatch", "Direct array response-column reference count does not match its stored second dimension.", array.get("path"), array_id=array.get("id"), variable=array.get("variable"), references=len(references), columns=len(columns))
            continue
        inline_record = None
        if role_fields:
            inline_record = {"fields": role_fields, "raw_fields": _safe(role_fields), "table_id": array.get("id"), "path": array.get("path"), "sheet": None, "row_index": None, "row_ref": str(array.get("variable")), "evidence": "array:%s variable=%s" % (array.get("id"), array.get("variable")), "identifiers": {role: role_fields[role] for role in _METADATA_ID_ROLES if role_fields.get(role) not in (None, "")}}
        source = dict(array)
        source["_response_trace_count"] = len(columns)
        traces = []
        base = int(_finite_float(array.get("mat_index_base", 0)) or 0)
        axis_reference = role_fields.get("axis_column")
        axis_index = next((index for index, value in enumerate(references) if str(value).strip() == str(axis_reference).strip()), None) if axis_reference not in (None, "") else None
        if axis_reference not in (None, "") and axis_index is None:
            number = _finite_float(axis_reference)
            if number is not None and number.is_integer() and 0 <= int(number) - base < len(columns):
                axis_index = int(number) - base
        if axis_reference not in (None, "") and axis_index is None:
            self.issue("direct_array_axis_reference_unresolved", "Direct array axis_column did not match an explicit column reference or a valid source index; no traces were inferred.", array.get("path"), array_id=array.get("id"), variable=array.get("variable"), axis_reference=axis_reference)
            continue
        for index, column in enumerate(columns):
            y = [_finite_float(value) for value in column[:_MAX_RAW_POINTS_PER_TRACE]]
            if sum(value is not None for value in y) < _MIN_CURVE_POINTS:
                continue
            column_label = str(references[index])
            variable = str(array.get("variable", "array")) + "::" + column_label
            locator = "array:%s variable=%s column=%s index_base=%d" % (array.get("id"), array.get("variable"), column_label, base)
            is_axis = index == axis_index
            traces.append({
                "id": "", "variable": variable, "x": [_finite_float(value) for value in columns[axis_index][:len(y)]] if axis_index is not None and not is_axis else [float(position + base) for position in range(len(y))], "y": y,
                "x_label": str(references[axis_index]) if axis_index is not None else "original array index (%d-based ordinal, not a measured abscissa)" % base, "x_unit": role_fields.get("x_unit") if axis_index is not None else None,
                "y_label": variable, "y_unit": role_fields.get("y_unit"), "index_base": base, "array_id": array.get("id"),
                "evidence": [locator], "entity": {"table_id": array.get("table_id"), "array_id": array.get("id"), "array_variable": array.get("variable"), "path": array.get("path"), "sheet": None, "variable": variable, "source_metadata": _safe(array), "shape": _safe(array.get("shape")), "dtype": array.get("dtype"), "mat_index_base": base, "column_zero_based": index},
                "source_column_zero_based": index, "source_id_candidates": [column_label], "metric_key": _trace_metric_key(variable, {"array_variable": array.get("variable")}, role_fields.get("response_metric")),
                "abscissa": {"identity": "metadata_axis" if axis_index is not None else "ordinal_array:" + str(array.get("id")), "status": "metadata_explicit_abscissa" if axis_index is not None else "unconfirmed_ordinal_not_experimental_abscissa", "basis": "explicit array axis reference" if axis_index is not None else "no measured abscissa in array metadata; original ordinal only", "unit": role_fields.get("x_unit") if axis_index is not None else None, "source_column_zero_based": axis_index},
                "inline_metadata_record": inline_record,
                "x_mode": "metadata_explicit_abscissa" if axis_index is not None else "original_array_index",
            })
        traces = [trace for trace in traces if trace["source_column_zero_based"] != axis_index]
        if traces:
            source["_response_trace_count"] = len(traces)
            groups.append({"source": source, "traces": traces, "routing": "validated direct array with explicit response column/unit metadata", "x_mode": traces[0]["x_mode"]})
    for table in self.tables:
        links = table.get("_array_links", [])
        if not links:
            continue
        metadata = table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}
        variable = metadata.get("variable")
        matches = [array for array in links if variable and _norm(array.get("variable")) == _norm(variable)]
        selected = matches[0] if len(matches) == 1 else links[0] if len(links) == 1 else None
        if selected is not None:
            table["_array_variable"] = selected.get("variable")
            table["_array_index_base"] = selected.get("mat_index_base", metadata.get("mat_index_base", 0))
            table["_array_ids"]["_default"] = selected.get("id")
        elif len(links) > 1:
            table["_array_mapping_ambiguous"] = True
            self.issue("array_table_mapping_ambiguous", "Multiple arrays link to one table without a unique variable mapping; raw columns remain available but joint band comparison is blocked.", table.get("path"), table_id=table.get("id"), array_ids=[array.get("id") for array in links])
    return groups


def _raw_trace_features(trace: Mapping[str, Any]) -> Dict[str, Any]:
    raw_x = list(trace["x"])
    raw_y = list(trace["y"])
    finite_positions = [index for index, value in enumerate(raw_y) if value is not None]
    if not finite_positions:
        return {"n_raw": len(raw_y), "n_finite_y": 0}
    first = finite_positions[0]
    last = finite_positions[-1]
    maximum = max(raw_y[index] for index in finite_positions)
    minimum = min(raw_y[index] for index in finite_positions)
    first_max = next(index for index in finite_positions if raw_y[index] == maximum)
    area_parts = []
    for left, right in zip(range(len(raw_y) - 1), range(1, len(raw_y))):
        if raw_x[left] is not None and raw_x[right] is not None and raw_y[left] is not None and raw_y[right] is not None:
            area_parts.append(0.5 * (raw_y[left] + raw_y[right]) * (raw_x[right] - raw_x[left]))
    finite_pairs = [(raw_x[index], raw_y[index], index) for index in finite_positions if raw_x[index] is not None]
    initial_count = min(20, max(5, int(math.ceil(len(finite_pairs) * 0.1))))
    initial = finite_pairs[:initial_count]
    initial_slope, initial_intercept = _ols([pair[0] for pair in initial], [pair[1] for pair in initial]) if len(initial) >= 2 else (None, None)
    x_values = [value for value in raw_x if value is not None]
    directions = []
    for left, right in zip(x_values, x_values[1:]):
        if right > left:
            directions.append(1)
        elif right < left:
            directions.append(-1)
        else:
            directions.append(0)
    nonzero = set(directions) - {0}
    direction = "increasing" if nonzero == {1} else "decreasing" if nonzero == {-1} else "constant" if not nonzero else "non_monotonic"
    segments = []
    if finite_pairs:
        start = 0
        current_sign = 0
        for position in range(1, len(finite_pairs)):
            difference = finite_pairs[position][0] - finite_pairs[position - 1][0]
            sign = 1 if difference > 0 else -1 if difference < 0 else current_sign
            if current_sign and sign and sign != current_sign:
                if position - start >= 2:
                    segments.append(finite_pairs[start:position])
                start = position - 1
            current_sign = sign or current_sign
        if len(finite_pairs) - start >= 2:
            segments.append(finite_pairs[start:])
    segment_features = []
    for segment in segments[:50]:
        slope, _ = _ols([pair[0] for pair in segment], [pair[1] for pair in segment])
        segment_features.append({
            "start_original_index": segment[0][2], "end_original_index": segment[-1][2], "n": len(segment),
            "x_start": segment[0][0], "x_end": segment[-1][0], "y_start": segment[0][1], "y_end": segment[-1][1],
            "delta": segment[-1][1] - segment[0][1], "xy_slope": slope,
        })
    return {
        "n_raw": len(raw_y), "n_finite_y": len(finite_positions), "n_nonfinite_y": len(raw_y) - len(finite_positions),
        "n_nonfinite_x": len(raw_x) - len(x_values), "duplicate_x_count": len(x_values) - len(set(x_values)),
        "first_finite_original_index": first, "last_finite_original_index": last,
        "raw_start": raw_y[first], "raw_end": raw_y[last], "raw_start_x": raw_x[first], "raw_end_x": raw_x[last],
        "min": minimum, "min_original_index": next(index for index in finite_positions if raw_y[index] == minimum),
        "max": maximum, "max_original_index": first_max,
        "x_at_first_max": raw_x[first_max], "net_change": raw_y[last] - raw_y[first],
        "area_original_order_trapezoid": math.fsum(area_parts), "n_area_segments": len(area_parts),
        "initial_slope": initial_slope, "initial_slope_intercept": initial_intercept, "initial_slope_n": len(initial) if initial_slope is not None else 0,
        "x_direction": direction, "monotonic_direction_segments": segment_features,
        "x_label": trace.get("x_label"), "x_unit": trace.get("x_unit"), "y_unit": trace.get("y_unit"),
        "area_unit": (trace.get("y_unit") + "*(" + trace.get("x_unit") + ")") if trace.get("y_unit") and trace.get("x_unit") else None,
        "slope_unit": (trace.get("y_unit") + "/(" + trace.get("x_unit") + ")") if trace.get("y_unit") and trace.get("x_unit") else None,
    }


def _interpolation_values(curves: Sequence[np.ndarray]) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], List[int]]:
    import numpy as np

    domains = []
    prepared = []
    used = []
    for index, curve in enumerate(curves):
        pairs = [(float(x), float(y)) for x, y in curve if math.isfinite(x) and math.isfinite(y)]
        groups: Dict[float, List[float]] = defaultdict(list)
        direction = 0
        ordered = []
        for x, y in pairs:
            if ordered:
                difference = x - ordered[-1][0]
                new_direction = 1 if difference > 0 else -1 if difference < 0 else direction
                if direction and new_direction and new_direction != direction:
                    return None, None, []
                direction = new_direction or direction
            ordered.append((x, y))
            groups[x].append(y)
        if len(groups) < 2 or direction == 0:
            continue
        unique_x = sorted(groups)
        mean_y = [math.fsum(groups[x]) / len(groups[x]) for x in unique_x]
        prepared.append((index, unique_x, mean_y))
        domains.append((unique_x[0], unique_x[-1]))
        used.append(index)
    if len(prepared) != len(curves) or not domains:
        return None, None, used
    lower = max(domain[0] for domain in domains)
    upper = min(domain[1] for domain in domains)
    if not upper > lower:
        return None, None, used
    grid = np.linspace(lower, upper, _GRID_POINTS)
    interpolated = []
    for _, xs, ys in prepared:
        interpolated.append(np.interp(grid, np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)))
    return grid, np.asarray(interpolated, dtype=float), used


def _modified_band_depth(interpolated: np.ndarray) -> Tuple[np.ndarray, int]:
    import numpy as np

    count = interpolated.shape[0]
    depths = np.zeros(count, dtype=np.int64)
    pair_count = count * (count - 1) // 2
    for left in range(count - 1):
        for right in range(left + 1, count):
            low = np.minimum(interpolated[left], interpolated[right])
            high = np.maximum(interpolated[left], interpolated[right])
            inside = (interpolated >= low) & (interpolated <= high)
            depths += inside.sum(axis=1, dtype=np.int64)
    return depths.astype(float) / float(pair_count * interpolated.shape[1]), pair_count


def _curve_deviation(self: _Analysis, group: Mapping[str, Any], trace: MutableMapping[str, Any], kind: str, deviation: Mapping[str, Any]) -> None:
    self._outlier_counter += 1
    metric = trace["variable"] + " [" + kind + "]"
    observed = deviation.get("observed")
    item = {
        "id": "O%04d" % self._outlier_counter, "entity": _safe(trace["entity"]), "metric": metric,
        "observed": observed, "reference": deviation.get("reference"), "deviation": deviation.get("deviation"),
        "scale": deviation.get("scale"), "score": deviation.get("score"), "threshold": _safe(deviation.get("threshold")),
        "evidence": list(trace["evidence"]), "status": "candidate_for_review", "inclusion": "retained",
        "unit": trace.get("y_unit"), "method": deviation.get("method", "Exploratory functional-data candidate"),
        "curve_group_id": group["id"], "curve_trace_id": trace["id"], "curve_candidate_kind": kind,
    }
    for key in ("fallback", "unique_spacing", "unique_multiple"):
        if key in deviation:
            item[key] = deviation[key]
    self.result["outlier_candidates"].append(item)
    self.fact("derived", "curve_candidate:" + kind, trace["entity"], {key: item.get(key) for key in ("id", "observed", "reference", "deviation", "scale", "score", "threshold", "status", "inclusion")}, trace.get("y_unit"), item["method"], item["evidence"], "Band or distance departure is exploratory and does not diagnose a mechanism; raw trace is retained.")


def _analyze_curve_group(self: _Analysis, group: MutableMapping[str, Any]) -> None:
    traces = group["traces"]
    for trace in traces:
        trace["features"] = _raw_trace_features(trace)
        self.fact("observed", "trace_raw_features:" + trace["variable"], trace["entity"], trace["features"], trace.get("y_unit"), "Raw-order finite-observation features; min/max first occurrence; adjacent-pair oriented trapezoid area; initial OLS uses up to first 20 and at least 5 finite pairs when available", trace["evidence"], "No smoothing, sorting, normalization, yield/load interpretation or image digitization was applied; ordinal x is not a measured abscissa.")
    all_traces = traces
    selected = []
    selected_units = set()
    duplicate_representations = []
    for trace in all_traces:
        unit = trace["independent_unit_id"]
        if unit not in selected_units:
            selected.append(trace)
            selected_units.add(unit)
            trace["depth_eligible"] = True
            trace["depth_exclusion"] = None
        else:
            trace["depth_eligible"] = False
            trace["depth_exclusion"] = "duplicate_representation_of_exactly_joined_independent_unit"
            duplicate_representations.append(trace["id"])
    traces = selected
    group["n_traces"] = len(all_traces)
    group["representation_count"] = len(all_traces)
    group["independent_unit_count"] = len(selected_units)
    group["depth_selection"] = {
        "rule": "exact-join independent_unit_id only: one stable source-order representation per exact joined unit enters functional depth; unmatched representations are never collapsed by curve shape, order or filename",
        "analysis_representation_count": len(traces), "analysis_independent_unit_count": len(selected_units),
        "duplicate_representations_retained_for_description": duplicate_representations,
    }
    group["depth_analysis"] = {"status": "descriptive_only", "reason": "fewer than 5 exactly joined independent units" if len(traces) < 5 else "not computed", "representation_count": len(all_traces), "independent_unit_count": len(selected_units)}
    if len(traces) < 5:
        return
    if group.get("depth_block"):
        group["depth_analysis"] = {"status": "not_computed", "reason": group["depth_block"], "n": len(traces)}
        self.issue("curve_cross_metric_comparison_blocked", "Band comparison was blocked because shared units/metric identity were not established; all raw trace features remain descriptive.", details={"group_id": group["id"], "reason": group["depth_block"], "n": len(traces)})
        return
    if len(traces) > _MAX_MBD_CURVES:
        group["depth_analysis"] = {"status": "not_computed", "reason": "trace count exceeds bounded O(n^2*201) limit", "n": len(traces), "limit": _MAX_MBD_CURVES}
        self.issue("mbd_bounded", "Modified band depth was not computed beyond the deterministic pairwise-work bound; raw trace features remain complete.", details={"group_id": group["id"], "n": len(traces), "limit": _MAX_MBD_CURVES})
        return
    try:
        import numpy as np
        curves = [np.asarray([(float("nan") if x is None else x, float("nan") if y is None else y) for x, y in zip(trace["x"], trace["y"])], dtype=float) for trace in traces]
        grid, interpolated, used = _interpolation_values(curves)
        if grid is None or interpolated is None:
            group["depth_analysis"] = {"status": "not_computed", "reason": "traces do not share a finite, nondegenerate monotonic common x domain after duplicate-x averaging", "n": len(traces)}
            self.issue("mbd_common_domain_unavailable", "No common monotonic interpolation domain; band analysis skipped without reordering raw traces.", details={"group_id": group["id"], "usable_traces": used})
            return
        depths, pair_count = _modified_band_depth(interpolated)
        median_depth = float(np.median(depths))
        central_indices = [index for index, depth in enumerate(depths) if depth >= median_depth]
        central = interpolated[central_indices]
        lower = np.min(central, axis=0)
        upper = np.max(central, axis=0)
        outside = (interpolated < lower) | (interpolated > upper)
        central_midpoint = (lower + upper) / 2.0
        central_half_width = (upper - lower) / 2.0
        expanded_lower = central_midpoint - 1.5 * central_half_width
        expanded_upper = central_midpoint + 1.5 * central_half_width
        expanded_outside = (interpolated < expanded_lower) | (interpolated > expanded_upper)
        metrics = []
        for index in range(len(traces)):
            others = np.delete(interpolated, index, axis=0)
            reference = np.median(others, axis=0)
            difference = interpolated[index] - reference
            centered_difference = (interpolated[index] - np.mean(interpolated[index])) - (reference - np.mean(reference))
            metrics.append({
                "l1": float(np.mean(np.abs(difference))),
                "rms": float(np.sqrt(np.mean(difference ** 2))),
                "amplitude_shift": float(np.mean(difference)),
                "shape_rms_centered": float(np.sqrt(np.mean(centered_difference ** 2))),
            })
        depth_record = {
            "status": "computed", "grid_points": _GRID_POINTS, "common_domain": [float(grid[0]), float(grid[-1])],
            "interpolation": "linear on each original monotonic x with nonfinite pairs omitted and duplicate-x y averaged only for interpolation",
            "definition": "MBD_i = mean over all unordered pairs j<k of the proportion of 201 grid points where y_i lies inclusively between min(y_j,y_k) and max(y_j,y_k); pairs containing i and ties included",
            "pair_count": pair_count, "depth_median": median_depth,
            "central_rule": "depth >= median(MBD), inclusive ties", "central_actual_fraction": len(central_indices) / len(traces),
            "central_trace_ids": [traces[index]["id"] for index in central_indices],
            "raw_order_preserved": True, "smoothing": False, "normalization": False,
            "grid_x": [float(value) for value in grid], "central_lower": [float(value) for value in lower], "central_upper": [float(value) for value in upper],
            "envelope_expansion_factor": 1.5, "envelope_expansion_rule": "per grid point: central midpoint +/- 1.5 * central half-width; raw central-50 excursions are weak flags only, expanded excursions are separate anomaly candidates, not p-values",
            "expanded_lower": [float(value) for value in expanded_lower], "expanded_upper": [float(value) for value in expanded_upper],
            "numpy_version": np.__version__,
        }
        group["depth_analysis"] = depth_record
        group["candidates"] = []
        group["envelope_flags"] = []
        for index, trace in enumerate(traces):
            trace["modified_band_depth"] = float(depths[index])
            trace["central_50_member"] = index in central_indices
            trace["central_envelope_outside_fraction"] = float(np.mean(outside[index]))
            trace["expanded_envelope_outside_fraction"] = float(np.mean(expanded_outside[index]))
            trace["leave_one_out_median_change"] = metrics[index]
            trace["candidate_kinds"] = []
            if np.any(outside[index]):
                trace["candidate_kinds"].append("central_envelope_outside_weak_flag")
                group["envelope_flags"].append({"trace_id": trace["id"], "kind": "central_envelope_outside_weak_flag", "outside_fraction": trace["central_envelope_outside_fraction"], "strength": "weak_descriptive_flag_not_primary_anomaly", "expanded_outside_fraction": trace["expanded_envelope_outside_fraction"]})
            if np.any(expanded_outside[index]):
                trace["candidate_kinds"].append("expanded_1p5_envelope_outside")
                group["candidates"].append({"trace_id": trace["id"], "kind": "expanded_1p5_envelope_outside", "outside_fraction": trace["expanded_envelope_outside_fraction"]})
                observed = trace["expanded_envelope_outside_fraction"]
                _curve_deviation(self, group, trace, "expanded_1p5_envelope_outside", {"observed": observed, "reference": 0.0, "deviation": observed, "scale": None, "score": observed, "threshold": {"value": 1.5, "basis": "per-grid central midpoint +/- 1.5 * central half-width; any interpolated point outside the expanded envelope; descriptive reference rule, not a calibrated probability"}, "method": "1.5-expanded central MBD envelope excursion candidate"})
            for key, kind in (("l1", "l1_leave_one_out_median"), ("rms", "rms_leave_one_out_median"), ("amplitude_shift", "amplitude_direction"), ("shape_rms_centered", "shape_after_vertical_centering")):
                values = [metric[key] for metric in metrics]
                deviation = _robust_deviation(values[index], [value for other, value in enumerate(values) if other != index])
                if deviation["candidate"]:
                    deviation = dict(deviation)
                    deviation["observed"] = values[index]
                    deviation["method"] = "Exploratory across-trace leave-one-out robust screen of " + key + "; amplitude/direction and centered shape are separate features"
                    trace["candidate_kinds"].append(kind)
                    group["candidates"].append({"trace_id": trace["id"], "kind": kind, "observed": values[index], "reference": deviation["reference"], "score": deviation["score"]})
                    _curve_deviation(self, group, trace, kind, deviation)
        self.method("modified_band_depth", "Exact inclusive pairwise modified band depth on a 201-point shared linear-interpolation domain; central 50% uses depth >= the median with ties retained. Leave-one-out median L1/RMS, signed amplitude and vertically centered shape are screened separately.", "Thresholds are exploratory robust screens, not calibrated p-values. Nonmonotonic x, disjoint domains or n<5 prohibit band comparison; raw traces remain descriptive.")
    except Exception as exc:
        group["depth_analysis"] = {"status": "failed", "error": str(exc)}
        self.issue("curve_depth_error", "Band analysis failed; raw features and other branches were retained.", details={"group_id": group["id"], "error": str(exc), "exception": type(exc).__name__})


def _write_curve_csv(self: _Analysis, groups: Sequence[Mapping[str, Any]]) -> None:
    traces = [trace for group in groups for trace in group["traces"]]
    if not traces:
        return
    path = self.work_dir / "analysis_curve_traces.csv"
    temporary = path.with_name(path.name + ".tmp")
    fields = ["group_id", "trace_id", "variable", "source_path", "sheet", "source_join_status", "source_join_method", "metadata_evidence", "comparison_basis", "comparison_value", "independent_unit_id", "representation_id", "depth_eligible", "batch", "channel", "instrument", "source_checksum", "abscissa_status", "x_label", "x_unit", "y_unit", "n_raw", "n_finite_y", "raw_start", "raw_end", "min", "max", "x_at_first_max", "net_change", "area", "initial_slope", "x_direction", "modified_band_depth", "central_50_member", "central_envelope_outside_fraction", "expanded_envelope_outside_fraction", "loo_l1", "loo_rms", "amplitude_shift", "shape_rms_centered", "candidate_kinds", "evidence"]
    try:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            for group in groups:
                for trace in group["traces"]:
                    features = trace["features"]
                    loo = trace.get("leave_one_out_median_change", {})
                    comparison = trace.get("comparison_context") or {}
                    technical = trace.get("technical_context") or {}
                    provenance = trace.get("metadata_provenance") or {}
                    checksum = trace.get("source_checksum") or {}
                    abscissa = trace.get("abscissa") or {}
                    row = {
                        "group_id": group["id"], "trace_id": trace["id"], "variable": trace["variable"],
                        "source_path": trace["entity"].get("path"), "sheet": trace["entity"].get("sheet"),
                        "source_join_status": trace.get("source_join_status"), "source_join_method": trace.get("source_join_method"),
                        "metadata_evidence": provenance.get("evidence"), "comparison_basis": comparison.get("basis"), "comparison_value": _safe(comparison.get("value")),
                        "independent_unit_id": trace.get("independent_unit_id"), "representation_id": trace.get("representation_id"), "depth_eligible": trace.get("depth_eligible"),
                        "batch": technical.get("batch"), "channel": technical.get("channel"), "instrument": technical.get("instrument"),
                        "source_checksum": checksum.get("value"), "abscissa_status": abscissa.get("status"),
                        "x_label": trace.get("x_label"), "x_unit": trace.get("x_unit"), "y_unit": trace.get("y_unit"),
                        "n_raw": features.get("n_raw"), "n_finite_y": features.get("n_finite_y"),
                        "raw_start": features.get("raw_start"), "raw_end": features.get("raw_end"), "min": features.get("min"), "max": features.get("max"),
                        "x_at_first_max": features.get("x_at_first_max"), "net_change": features.get("net_change"),
                        "area": features.get("area_original_order_trapezoid"), "initial_slope": features.get("initial_slope"), "x_direction": features.get("x_direction"),
                        "modified_band_depth": trace.get("modified_band_depth"), "central_50_member": trace.get("central_50_member"),
                        "central_envelope_outside_fraction": trace.get("central_envelope_outside_fraction"), "expanded_envelope_outside_fraction": trace.get("expanded_envelope_outside_fraction"), "loo_l1": loo.get("l1"), "loo_rms": loo.get("rms"),
                        "amplitude_shift": loo.get("amplitude_shift"), "shape_rms_centered": loo.get("shape_rms_centered"),
                        "candidate_kinds": ";".join(trace.get("candidate_kinds", [])), "evidence": " | ".join(trace["evidence"]),
                    }
                    writer.writerow({key: _safe(row.get(key)) for key in fields})
        temporary.replace(path)
        self.artifact(path)
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        self.issue("curve_csv_error", "Could not write trace detail CSV; JSON trace facts remain available.", path, error=str(exc))


def _plot_curve_groups(self: _Analysis, groups: Sequence[Mapping[str, Any]]) -> int:
    plotted = 0
    if not groups:
        return plotted
    try:
        import matplotlib
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except Exception as exc:
        self.issue("curve_plot_dependency", "Matplotlib is unavailable; no PNG was claimed, while calculations remain available.", error=str(exc))
        return plotted
    self.result["methods"].append({"name": "curve_png_preview", "version": matplotlib.__version__, "algorithm": "Matplotlib Agg-backed Figure renders every retained raw trace in original order, with central envelope and candidate overlay when computed.", "limitations": "PNG is a preview, not a replacement for CSV/raw evidence; missing or failed plotting never blocks calculations."})
    self.work_dir.mkdir(parents=True, exist_ok=True)
    for group in groups[:20]:
        path = self.work_dir / ("analysis_" + group["id"].lower() + "_curves.png")
        try:
            figure = Figure(figsize=(12, 7), constrained_layout=True)
            FigureCanvasAgg(figure)
            axis = figure.add_subplot(111)
            for trace in group["traces"]:
                pairs = [(x, y) for x, y in zip(trace["x"], trace["y"]) if x is not None and y is not None]
                if not pairs:
                    continue
                is_candidate = bool(trace.get("candidate_kinds"))
                axis.plot([pair[0] for pair in pairs], [pair[1] for pair in pairs], linewidth=1.25 if is_candidate else 0.65, alpha=0.75 if is_candidate else 0.35, color="#c43c35" if is_candidate else "#3568a8")
            depth = group.get("depth_analysis", {})
            if depth.get("status") == "computed":
                grid = depth["grid_x"]
                axis.fill_between(grid, depth["central_lower"], depth["central_upper"], color="#e1ad01", alpha=0.22, label="central MBD envelope (inclusive ties)")
                axis.plot(grid, depth["central_lower"], color="#9a7600", linewidth=0.8)
                axis.plot(grid, depth["central_upper"], color="#9a7600", linewidth=0.8)
                axis.plot(grid, depth["expanded_lower"], color="#6a5a16", linewidth=0.7, linestyle="--", label="1.5-expanded envelope")
                axis.plot(grid, depth["expanded_upper"], color="#6a5a16", linewidth=0.7, linestyle="--")
                axis.legend(loc="best")
            axis.set_xlabel(str(group["traces"][0].get("x_label") or "original x"))
            axis.set_ylabel("raw values; units per trace (see JSON/CSV)")
            axis.set_title(group["id"] + ": raw traces, central envelope and candidates\nNo smoothing, sorting or normalization")
            axis.grid(True, alpha=0.15)
            figure.savefig(path, format="png", dpi=140)
            figure.clear()
            if not path.is_file() or path.stat().st_size == 0:
                raise OSError("PNG was not created")
            self.artifact(path)
            plotted += 1
        except Exception as exc:
            self.issue("curve_plot_error", "PNG generation failed for one group; computations were not blocked.", path, error=str(exc), matplotlib_version=getattr(matplotlib, "__version__", None))
    if len(groups) > 20:
        self.issue("curve_plot_bounded", "Only the first 20 curve groups received PNG previews; all groups retain JSON/CSV details.", details={"groups": len(groups)})
    return plotted


def _prepare_curve_groups(self: _Analysis) -> List[Dict[str, Any]]:
    if hasattr(self, "_prepared_curve_groups"):
        return self._prepared_curve_groups
    _prepare_source_metadata(self)
    candidates = list(_build_direct_array_groups(self))
    linked_tables = {str(array.get("table_id")) for array in self.data.get("arrays", []) if isinstance(array, Mapping) and array.get("table_id") is not None}
    for table in self.tables:
        try:
            candidates.extend(_build_table_trace_group(self, table, str(table.get("id")) in linked_tables))
        except Exception as exc:
            self.issue("curve_routing_error", "Curve routing failed for one table; generic summaries remain available.", table.get("path"), error=str(exc), exception=type(exc).__name__)
    self._prepared_curve_groups = _regroup_trace_candidates(self, candidates)
    return self._prepared_curve_groups


def _curve_analysis(self: _Analysis) -> None:
    groups = list(_prepare_curve_groups(self))
    bounded = []
    total = 0
    for group in groups:
        for trace in group["traces"]:
            if total < _MAX_CURVES:
                bounded.append(trace)
                total += 1
            else:
                self.issue("curve_count_bounded", "Additional traces were not analyzed after the global trace bound.", details={"limit": _MAX_CURVES, "variable": trace.get("variable")})
                break
        if bounded:
            group["traces"] = bounded
            groups[groups.index(group)]["traces"] = bounded
            bounded = []
        else:
            group["traces"] = []
    groups = [group for group in groups if group["traces"]]
    groups.sort(key=lambda group: (str(group["source"].get("path", "")), str(group["source"].get("sheet", "")), str(group["source"].get("id", "")), str(group["traces"][0]["variable"])))
    output_groups = []
    trace_number = 0
    for group_number, group in enumerate(groups, 1):
        group["id"] = "G%03d" % group_number
        for trace in group["traces"]:
            trace_number += 1
            trace["id"] = "C%04d" % trace_number
        try:
            _analyze_curve_group(self, group)
        except Exception as exc:
            self.issue("curve_feature_error", "Trace feature analysis failed for one group.", details={"group_id": group["id"], "error": str(exc), "exception": type(exc).__name__})
            for trace in group["traces"]:
                trace.setdefault("features", {})
        source = group["source"]
        public_traces = []
        for trace in group["traces"]:
            public_traces.append({key: _safe(value) for key, value in trace.items() if key not in {"x", "y", "source"} | ({"x", "y"} if len(trace.get("x", [])) > 5000 else set())})
        source_files = group.get("source_files", [])
        source_tables = group.get("source_tables", [])
        output_groups.append({
            "id": group["id"], "table_id": source_tables[0] if len(source_tables) == 1 else None,
            "path": source_files[0] if len(source_files) == 1 else None, "sheet": source.get("sheet") if len(source_files) == 1 else None,
            "source_files": source_files, "source_tables": source_tables,
            "comparison_context": _safe(group.get("comparison_context")), "metric_key": group.get("metric_key"), "abscissa": _safe(group.get("abscissa")),
            "technical_contexts": [json.loads(value) for value in group.get("technical_contexts", [])],
            "representation_count": group.get("representation_count", len(group["traces"])), "independent_unit_count": group.get("independent_unit_count"),
            "depth_selection": _safe(group.get("depth_selection")),
            "routing": group["routing"], "x_mode": group.get("x_mode"), "n_traces": len(group["traces"]),
            "depth_analysis": _safe(group.get("depth_analysis")), "candidates": _safe(group.get("candidates", [])), "envelope_flags": _safe(group.get("envelope_flags", [])), "traces": public_traces,
        })
    self.result["curve_analysis"]["groups"] = output_groups
    self.result["curve_analysis"]["source_join_issues"] = _safe(getattr(self, "_source_join_issues", []))
    plotted = _plot_curve_groups(self, groups)
    _write_curve_csv(self, groups)
    computed = sum(group.get("depth_analysis", {}).get("status") == "computed" for group in groups)
    all_traces = [trace for group in groups for trace in group["traces"]]
    coverage = {
        "groups": len(groups), "traces": trace_number, "traces_with_raw_features": sum(bool(trace.get("features")) for trace in all_traces),
        "mbd_groups": computed, "descriptive_only_groups": sum(group.get("depth_analysis", {}).get("status") == "descriptive_only" for group in groups),
        "plotted_groups": plotted, "csv_trace_rows": trace_number if "analysis_curve_traces.csv" in self._artifact_set else 0,
        "representation_count": trace_number, "independent_unit_count": len({trace["independent_unit_id"] for trace in all_traces}),
        "mapped_traces": sum(trace.get("source_join_status") == "matched" for trace in all_traces),
        "unmatched_traces": sum(trace.get("source_join_status") == "unmatched" for trace in all_traces),
        "ambiguous_traces": sum(trace.get("source_join_status") in {"mapping_ambiguous", "mapping_unresolved"} for trace in all_traces),
        "source_join_issue_count": len(getattr(self, "_source_join_issues", [])),
        "grid_points": _GRID_POINTS, "limits": {"max_traces": _MAX_CURVES, "max_mbd_traces_per_group": _MAX_MBD_CURVES, "max_raw_points_per_trace": _MAX_RAW_POINTS_PER_TRACE},
    }
    self.result["curve_analysis"]["coverage"] = coverage
    self.result["coverage"]["series"] = coverage
    self.result["coverage"]["records"].update({key: coverage[key] for key in ("mapped_traces", "unmatched_traces", "ambiguous_traces", "representation_count", "independent_unit_count", "source_join_issue_count")})
    if any(trace.get("x_mode") == "original_row_index" or trace.get("x_mode") == "original_array_index" for group in groups for trace in group["traces"]):
        self.limitation("Some curves use declared original row/array indices because no measured common x column was identified; these indices are not physical units.")


def _decimal_value(value: Any) -> Optional[Decimal]:
    number = _finite_float(value)
    if number is None:
        return None
    try:
        if isinstance(value, str):
            text = value.strip().replace(",", "")
            result = Decimal(text)
        else:
            result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _method_excerpts(text: str, limit: int = 12) -> List[str]:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return []
    sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", cleaned)
    keywords = re.compile(r"\b(?:method|methods|methodology|materials|procedure|experiment|experimental|sample|specimen|measured|measurement|protocol|prepared|calibrated|cross.validation|StratifiedKFold|KFold|LogisticRegression|load_iris)\b|方法|材料|实验步骤|样品|试样|测定|测量|校准", re.I)
    selected = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) >= 25 and keywords.search(sentence):
            selected.append(sentence[:500])
        if len(selected) >= limit:
            break
    if not selected and cleaned:
        selected = [cleaned[:500]]
    return selected


def _audit_reader_formula_cells(self: _Analysis, table: Mapping[str, Any]) -> Tuple[int, int]:
    formula_cells = table.get("formula_cells")
    if not formula_cells:
        return 0, 0
    items = formula_cells.items() if isinstance(formula_cells, Mapping) else enumerate(formula_cells) if isinstance(formula_cells, list) else []
    formulas = 0
    missing = 0
    for position, item in items:
        if isinstance(item, Mapping):
            cell = item.get("cell", item.get("address", item.get("ref", position)))
            formula = item.get("formula", item.get("value"))
            has_formula = bool(item.get("is_formula", False)) or (isinstance(formula, str) and formula.startswith("="))
            cache_present = any(item.get(key) not in (None, "") for key in ("cached_value", "cached", "result"))
            if item.get("has_cached_value") is True:
                cache_present = True
            if item.get("has_cached_value") is False:
                cache_present = False
        else:
            cell = position
            formula = item
            has_formula = isinstance(item, str) and item.startswith("=")
            cache_present = False
        if not has_formula:
            continue
        formulas += 1
        if not cache_present:
            missing += 1
            evidence = _location(table.get("path"), table.get("sheet"), str(cell))
            self.source_anomaly("formula_cache_missing", table.get("path"), [evidence], "quality_violation", sheet=table.get("sheet"), cell=str(cell), formula=formula, cached_value=None, impact="Formula text is present but no cached result was supplied; the value cannot be treated as a verified observed number.")
    return formulas, missing


def _resolve_input_path(self: _Analysis, path: Any) -> Optional[Path]:
    if path in (None, ""):
        return None
    candidate = Path(str(path))
    possibilities = [candidate] if candidate.is_absolute() else [candidate, self.work_dir / candidate]
    for possibility in possibilities:
        try:
            if possibility.is_file():
                return possibility
        except OSError:
            continue
    return None


def _audit_xlsx_archive(self: _Analysis, table: Mapping[str, Any]) -> Tuple[int, int]:
    path = _resolve_input_path(self, table.get("path"))
    if path is None or path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return 0, 0
    try:
        import posixpath
        import zipfile
        import xml.etree.ElementTree as ET

        with zipfile.ZipFile(path) as archive:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            relationships = {rel.attrib.get("Id"): rel.attrib.get("Target") for rel in rels}
            targets = {}
            for sheet in workbook.iter():
                if sheet.tag.rsplit("}", 1)[-1] != "sheet":
                    continue
                name = sheet.attrib.get("name")
                relationship_id = next((value for key, value in sheet.attrib.items() if key.rsplit("}", 1)[-1] == "id"), None)
                target = relationships.get(relationship_id)
                if not target:
                    continue
                if target.startswith("/"):
                    archive_path = target.lstrip("/")
                else:
                    archive_path = posixpath.normpath(posixpath.join("xl", target))
                targets[name] = archive_path
            sheet_name = table.get("sheet")
            if sheet_name not in targets:
                if len(targets) == 1:
                    sheet_name, archive_path = next(iter(targets.items()))
                else:
                    return 0, 0
            else:
                archive_path = targets[sheet_name]
            root = ET.fromstring(archive.read(archive_path))
            formulas = 0
            missing = 0
            for cell in root.iter():
                if cell.tag.rsplit("}", 1)[-1] != "c":
                    continue
                children = {child.tag.rsplit("}", 1)[-1]: child for child in cell}
                formula_node = children.get("f")
                if formula_node is None:
                    continue
                formulas += 1
                value_node = children.get("v")
                cached = None if value_node is None else value_node.text
                if cached not in (None, ""):
                    continue
                missing += 1
                address = cell.attrib.get("r", "unknown")
                evidence = _location(table.get("path"), sheet_name, address)
                self.source_anomaly("formula_cache_missing", table.get("path"), [evidence], "quality_violation", sheet=sheet_name, cell=address, formula=formula_node.text or "", cached_value=None, audit="direct XLSX XML formula/cached-value inspection", impact="No cached <v> result; formula evaluation was not invented.")
                if formulas >= 5000:
                    self.issue("formula_audit_bounded", "Formula audit stopped at 5000 formula cells for this sheet.", table.get("path"), sheet=sheet_name)
                    break
            return formulas, missing
    except Exception as exc:
        self.issue("xlsx_formula_audit_error", "Could not inspect workbook formula XML directly; reader-provided formula metadata remains available.", table.get("path"), error=str(exc), exception=type(exc).__name__)
        return 0, 0


def _source_analysis(self: _Analysis) -> None:
    self.method("source_provenance", "Reader file metadata/issues, workbook formula caches, extracted PDF text and exact numeric workbook matches are retained separately from derived calculations.", "PDF images are never digitized. Exact numeric coincidence is alignment evidence, not proof that a PDF value should replace a workbook value.")
    inputs = self.data.get("inputs", [])
    if isinstance(inputs, Mapping):
        input_items = []
        for path, metadata in inputs.items():
            item = dict(metadata) if isinstance(metadata, Mapping) else {"value": metadata}
            item.setdefault("path", path)
            input_items.append(item)
    elif isinstance(inputs, list):
        input_items = [item for item in inputs if isinstance(item, Mapping)]
    else:
        input_items = []
    for item in sorted(input_items, key=lambda value: str(value.get("path", ""))):
        path = item.get("path")
        entity = {"path": path, "hash": item.get("hash"), "status": item.get("status"), "parser": item.get("parser")}
        self.fact("observed", "input_file", entity, {key: item.get(key) for key in ("hash", "status", "parser", "issues")}, None, "reader-provided file identity/status; no hash recomputation claimed", [path], "Parser status and issues limit every downstream observation from this source.")
        for issue in item.get("issues", []) if isinstance(item.get("issues"), list) else []:
            self.source_anomaly("reader_input_issue", path, [path], "reported", issue=_safe(issue))
    for issue in self.data.get("issues", []) if isinstance(self.data.get("issues"), list) else []:
        self.issue("reader_issue", "Reader-reported issue retained.", details={"issue": _safe(issue)})
    formula_summary = []
    for table in self.tables:
        reader_formulas, reader_missing = _audit_reader_formula_cells(self, table)
        archive_formulas, archive_missing = _audit_xlsx_archive(self, table)
        if reader_formulas or archive_formulas:
            reader_cells = table.get("formula_cells")
            formula_summary.append({"table_id": table.get("id"), "reader_formula_cells": reader_formulas, "reader_missing_caches": reader_missing, "archive_formula_cells": archive_formulas, "archive_missing_caches": archive_missing, "reader_formula_metadata_present": bool(reader_cells)})
            self.fact("observed", "workbook_formula_cache_audit", {"table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet")}, formula_summary[-1], None, "reader formula_cells audit plus direct XLSX XML <f>/<v> inspection when the source path is readable", [_location(table.get("path"), table.get("sheet"))], "Reader and direct XML counts are separate audit views, not additive formula totals; unevaluated formulas remain unverified.")
    numeric_index: Dict[Decimal, List[Dict[str, Any]]] = defaultdict(list)
    numeric_cells = 0
    for table in self.tables:
        for column_index, name in enumerate(table["_names"]):
            for row_index, row in enumerate(table["_rows"]):
                raw = _row_value(row, name, column_index)
                number = _decimal_value(raw)
                if number is None:
                    continue
                numeric_cells += 1
                if numeric_cells > 250000:
                    continue
                bucket = numeric_index[number]
                if len(bucket) < 30:
                    bucket.append({"table_id": table.get("id"), "column": name, "unit": _metric_unit(self.case, name), "value": _finite_float(raw), "evidence": _table_range(table, column_index, row_index)})
    if numeric_cells > 250000:
        self.limitation("Exact PDF/workbook numeric alignment indexed only the first 250000 finite table cells in stable source order.")
    alignments = 0
    documents = self.data.get("documents", []) if isinstance(self.data.get("documents", []), list) else []
    document_pages = 0
    for document in sorted((item for item in documents if isinstance(item, Mapping)), key=lambda item: str(item.get("path", ""))):
        path = document.get("path")
        pages = document.get("pages", []) if isinstance(document.get("pages"), list) else []
        document_pages += len(pages)
        self.fact("source_reported", "document_metadata", {"document_id": document.get("id"), "path": path}, {"total_pages": document.get("total_pages"), "metadata": document.get("metadata"), "issues": document.get("issues")}, None, "reader-provided document metadata; no image interpretation", [path], "Metadata and extracted text do not establish content present only in figures or scans.")
        for issue in document.get("issues", []) if isinstance(document.get("issues"), list) else []:
            self.source_anomaly("document_reader_issue", path, [path], "reported", issue=_safe(issue))
        for page in sorted((item for item in pages if isinstance(item, Mapping)), key=lambda item: int(_finite_float(item.get("page", 0)) or 0)):
            page_number = page.get("page")
            text = page.get("text", "") if isinstance(page.get("text"), str) else ""
            evidence = "%s#page=%s" % (path, page_number)
            for quote in _method_excerpts(text, limit=3):
                self.fact("source_reported", "document_method_excerpt", {"document_id": document.get("id"), "path": path, "page": page_number}, {"short_quote": quote}, None, "short verbatim normalized-whitespace quote from extracted PDF page text", [evidence], "Excerpt selection is keyword-assisted; consult the page for full context.")
            for match in _NUMBER_RE.finditer(text):
                if alignments >= 250:
                    break
                token = match.group(0)
                try:
                    number = Decimal(token)
                except InvalidOperation:
                    continue
                matches = numeric_index.get(number, [])
                if not matches:
                    continue
                units = sorted({item["unit"] for item in matches if item["unit"]})
                alignments += 1
                self.fact("source_reported", "pdf_workbook_exact_numeric_alignment", {"document_id": document.get("id"), "path": path, "page": page_number, "pdf_token": token}, {"pdf_value": float(number), "workbook_matches": matches, "match_count": len(matches), "workbook_values_overridden": False}, units[0] if len(units) == 1 else None, "exact Decimal equality between extracted PDF numeric token and finite parsed workbook cells; no rounding-based match", [evidence] + [item["evidence"] for item in matches[:10]], "Numeric equality can be coincidental and does not prove shared entity/metric; workbook values were not replaced.")
        if alignments >= 250:
            self.issue("pdf_alignment_bounded", "PDF/workbook exact numeric alignment fact limit reached.", path, limit=250)
    other_files = self.data.get("other_files", []) if isinstance(self.data.get("other_files"), list) else []
    for item in sorted((entry for entry in other_files if isinstance(entry, Mapping)), key=lambda entry: str(entry.get("path", ""))):
        path = item.get("path")
        text = item.get("text", "") if isinstance(item.get("text"), str) else ""
        self.fact("observed", "other_text_file", {"path": path}, {"text_characters": len(text), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}, None, "exact UTF-8 text character count and SHA-256 of reader-provided text", [path], "Text may be incomplete relative to its source file; parser issues remain authoritative.")
        for quote in _method_excerpts(text, limit=12):
            self.fact("source_reported", "text_method_excerpt", {"path": path}, {"short_quote": quote}, None, "short normalized-whitespace quote from other_files.text", [path], "Quote is not a substitute for the surrounding protocol.")
    if documents:
        self.limitation("Documents were analyzed only through extracted page text and metadata; no paper figures/images were digitized into pseudo-replicates.")
    self.result["coverage"]["records"].update({"input_files": len(input_items), "documents": len(documents), "document_pages": document_pages, "other_files": len(other_files), "pdf_workbook_exact_alignments": alignments, "formula_audit_views": formula_summary})


def _find_named_column(table: Mapping[str, Any], accepted: Sequence[str]) -> Optional[int]:
    accepted_normalized = {_norm(item) for item in accepted}
    for index, name in enumerate(table["_names"]):
        if _norm(name) in accepted_normalized:
            return index
    return None


def _prediction_schema(table: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    true_index = _find_named_column(table, ("y_true", "true", "true_value", "actual", "actual_class", "target", "ground_truth", "label", "class"))
    if true_index is None:
        return None
    estimator_index = _find_named_column(table, ("estimator", "model", "classifier", "algorithm", "estimator_name"))
    fold_index = _find_named_column(table, ("fold", "fold_id", "cv_fold", "split", "split_id"))
    sample_index = _find_named_column(table, ("sample_index", "sample_id", "row_id", "observation_id", "test_index", "index"))
    pred_map = {}
    for index, name in enumerate(table["_names"]):
        normalized = _norm(name)
        label = None
        if normalized in {"pred", "prediction", "predicted", "ypred", "predictedclass", "predictionlabel"}:
            label = "prediction"
        else:
            match = re.match(r"^(.*?)(?:pred|prediction|predicted|ypred)(?:class|label)?$", normalized)
            if match and match.group(1):
                label = match.group(1).strip("_ ")
        if label is not None and index != true_index:
            pred_map[label] = index
    if not pred_map:
        return None
    if "prediction" in pred_map and estimator_index is None and len(pred_map) == 1:
        pred_map = {table["_names"][index]: index for index in pred_map.values()}
    return {"true_index": true_index, "estimator_index": estimator_index, "fold_index": fold_index, "sample_index": sample_index, "pred_map": pred_map}


def _label_equal(left: Any, right: Any) -> Optional[bool]:
    if _missing(left) or _missing(right):
        return None
    left_number = _finite_float(left)
    right_number = _finite_float(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return str(left).strip().casefold() == str(right).strip().casefold()


def _label_sort_key(value: Any) -> Tuple[int, Any, str]:
    number = _finite_float(value)
    if number is not None:
        return 0, number, ""
    return 1, 0, str(value)


def _confusion(true_values: Sequence[Any], predictions: Sequence[Any]) -> Dict[str, Any]:
    labels = sorted({_json_key(value) for value in list(true_values) + list(predictions) if not _missing(value)}, key=lambda item: _label_sort_key(json.loads(item)))
    decoded = [json.loads(item) for item in labels]
    positions = {item: index for index, item in enumerate(labels)}
    matrix = [[0 for _ in labels] for _ in labels]
    for actual, predicted in zip(true_values, predictions):
        actual_key = _json_key(actual)
        predicted_key = _json_key(predicted)
        if actual_key in positions and predicted_key in positions:
            matrix[positions[actual_key]][positions[predicted_key]] += 1
    return {"labels": decoded, "matrix": matrix, "orientation": "rows=true, columns=predicted; missing labels omitted"}


def _fold_estimator_sort(value: Any) -> Tuple[int, Any, str]:
    return _label_sort_key(value)


def _recompute_prediction_tables(self: _Analysis) -> int:
    self.method("provided_prediction_recompute", "Correctness, fold correct counts, accuracy, label confusion matrices and paired fold/sample comparisons are computed directly from provided per-sample true/predicted labels.", "No sklearn model is fitted for recomputation. Missing labels are excluded only from the scored denominator and explicitly counted; pairing without source sample indices uses within-fold order and is limited accordingly.")
    record_total = 0
    for table in self.tables:
        schema = _prediction_schema(table)
        if schema is None:
            continue
        if schema["fold_index"] is None:
            self.issue("prediction_fold_missing", "Prediction table has no fold/split column; per-sample correctness is recomputed but not represented as five-fold CV.", table.get("path"), table_id=table.get("id"))
        records: Dict[str, Dict[str, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        estimator_labels: Dict[str, Any] = {}
        remaining = max(0, _MAX_PREDICTION_RECORDS - record_total)
        rows_to_process = table["_rows"]
        predicted_columns = max(1, len(schema["pred_map"]))
        if len(rows_to_process) * predicted_columns > remaining:
            allowed_rows = remaining // predicted_columns
            rows_to_process = rows_to_process[:allowed_rows]
            self.issue("prediction_records_bounded", "Only a stable source-order prefix of prediction rows was retained after the global prediction-record budget was reached.", table.get("path"), table_id=table.get("id"), retained_source_rows=allowed_rows, original_source_rows=len(table["_rows"]), budget=_MAX_PREDICTION_RECORDS)
        for row_index, row in enumerate(rows_to_process):
            actual = _row_value(row, table["_names"][schema["true_index"]], schema["true_index"])
            fold_raw = _row_value(row, table["_names"][schema["fold_index"]], schema["fold_index"]) if schema["fold_index"] is not None else None
            fold_key = _json_key(fold_raw)
            sample_raw = _row_value(row, table["_names"][schema["sample_index"]], schema["sample_index"]) if schema["sample_index"] is not None else None
            for predicted_name, predicted_index in sorted(schema["pred_map"].items(), key=lambda item: item[0]):
                if schema["estimator_index"] is not None:
                    estimator_raw = _row_value(row, table["_names"][schema["estimator_index"]], schema["estimator_index"])
                else:
                    estimator_raw = predicted_name
                if _missing(estimator_raw):
                    self.issue("prediction_estimator_missing", "Skipped a prediction row without an estimator label.", table.get("path"), row_ref=_row_ref(table, row_index))
                    continue
                estimator_key = str(estimator_raw)
                estimator_labels[estimator_key] = estimator_raw
                predicted = _row_value(row, table["_names"][predicted_index], predicted_index)
                correct = _label_equal(actual, predicted)
                records[estimator_key][fold_key].append({
                    "sample_raw": sample_raw, "y_true": actual, "y_pred": predicted, "correct": correct,
                    "row_index": row_index, "row_ref": _row_ref(table, row_index),
                    "evidence": _location(table.get("path"), table.get("sheet"), _row_ref(table, row_index)),
                    "entity": _entity(table, row, row_index),
                })
                record_total += 1
        for estimator_key in sorted(records):
            folds = []
            for fold_key in sorted(records[estimator_key], key=lambda item: _fold_estimator_sort(json.loads(item))):
                samples = records[estimator_key][fold_key]
                occurrences = Counter()
                public_samples = []
                for sample in samples:
                    if sample["sample_raw"] not in (None, ""):
                        sample_key = sample["sample_raw"]
                        pair_basis = "source_sample_index"
                    else:
                        position = occurrences[fold_key]
                        occurrences[fold_key] += 1
                        sample_key = position
                        pair_basis = "within_fold_row_order"
                    public_samples.append({
                        "sample_key": _safe(sample_key), "sample_pair_basis": pair_basis,
                        "row_index_zero_based": sample["row_index"], "row_ref": sample["row_ref"],
                        "y_true": _safe(sample["y_true"]), "y_pred": _safe(sample["y_pred"]), "correct": sample["correct"],
                        "evidence": sample["evidence"], "entity": sample["entity"],
                    })
                scored = [sample for sample in public_samples if sample["correct"] is not None]
                correct_count = sum(sample["correct"] is True for sample in scored)
                accuracy = correct_count / len(scored) if scored else None
                fold_value = json.loads(fold_key)
                folds.append({
                    "fold": fold_value, "n": len(public_samples), "n_scored": len(scored), "n_missing_labels": len(public_samples) - len(scored),
                    "correct_count": correct_count, "accuracy": accuracy, "samples": public_samples,
                    "confusion": _confusion([sample["y_true"] for sample in public_samples], [sample["y_pred"] for sample in public_samples]),
                    "metrics_state": "recomputed_from_provided_predictions",
                })
                self.fact("derived", "classification_accuracy", {"table_id": table.get("id"), "path": table.get("path"), "estimator": estimator_labels[estimator_key], "fold": fold_value}, {"correct_count": correct_count, "n_scored": len(scored), "accuracy": accuracy, "n_missing_labels": len(public_samples) - len(scored)}, None, "sum(y_true == y_pred)/n_scored from provided labels", sorted({sample["evidence"] for sample in public_samples})[:200], "This recomputation does not verify how the supplied predictions were fitted or whether train/test splits leaked.")
            self.result["computational_results"]["runs"].append({
                "run_id": "provided:" + str(table.get("id")) + ":" + estimator_key,
                "dataset": "provided prediction table", "estimator": estimator_labels[estimator_key], "source": "provided_predictions",
                "state": "recomputed", "execution": "not_reexecuted", "table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet"),
                "folds": folds, "estimator_params": None, "status": "bounded_prediction_prefix" if len(rows_to_process) < len(table["_rows"]) else "recomputed_with_missing_labels" if any(fold["n_missing_labels"] for fold in folds) else "complete",
            })
    return record_total


def _reported_classification_tables(self: _Analysis) -> None:
    for table in self.tables:
        if _prediction_schema(table) is not None:
            continue
        estimator_index = _find_named_column(table, ("estimator", "model", "classifier", "algorithm", "estimator_name"))
        fold_index = _find_named_column(table, ("fold", "fold_id", "cv_fold", "split", "split_id"))
        accuracy_index = _find_named_column(table, ("accuracy", "acc", "test_accuracy", "accuracy_score"))
        if estimator_index is None or accuracy_index is None:
            continue
        folds = []
        for row_index, row in enumerate(table["_rows"]):
            estimator = _row_value(row, table["_names"][estimator_index], estimator_index)
            accuracy = _finite_float(_row_value(row, table["_names"][accuracy_index], accuracy_index))
            fold = _row_value(row, table["_names"][fold_index], fold_index) if fold_index is not None else None
            if _missing(estimator) or accuracy is None:
                continue
            evidence = _table_range(table, accuracy_index, row_index)
            fold_record = {"fold": _safe(fold), "estimator": _safe(estimator), "reported_accuracy": accuracy, "metrics_state": "source_reported_not_recomputed", "evidence": evidence}
            folds.append(fold_record)
            self.fact("source_reported", "reported_classification_accuracy", _entity(table, row, row_index), {"estimator": _safe(estimator), "fold": _safe(fold), "accuracy": accuracy, "accuracy_unit_from_header": _metric_unit(self.case, table["_names"][accuracy_index])}, _metric_unit(self.case, table["_names"][accuracy_index]), "transcribed finite accuracy value from supplied results table; no recalculation without predictions/correct counts", [evidence], "Reported accuracy is not independently verified and is never presented as a new fit.")
        if folds:
            by_estimator: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for fold in folds:
                by_estimator[str(fold["estimator"])].append(fold)
            if len(by_estimator) == 1:
                run_estimator = next(iter(by_estimator))
                run_folds = folds
            else:
                run_estimator = "multiple; see fold source entities"
                run_folds = folds
            self.result["computational_results"]["runs"].append({
                "run_id": "reported:" + str(table.get("id")), "dataset": "provided aggregate results table", "estimator": _safe(run_estimator),
                "source": "provided_results_table", "state": "source_reported", "execution": "not_reexecuted", "table_id": table.get("id"),
                "path": table.get("path"), "sheet": table.get("sheet"), "folds": run_folds, "status": "reported_only",
            })


_ESTIMATOR_NAMES = {
    "logisticregression": "LogisticRegression", "decisiontreeclassifier": "DecisionTreeClassifier",
    "randomforestclassifier": "RandomForestClassifier", "svc": "SVC", "supportvectorclassifier": "SVC",
    "kneighborsclassifier": "KNeighborsClassifier", "gaussiannb": "GaussianNB",
}
_EST_PARAM_TYPES = {
    "max_iter": "positive_int", "C": "positive_float", "max_depth": "optional_positive_int",
    "min_samples_leaf": "leaf", "n_neighbors": "positive_int", "n_estimators": "positive_int",
    "kernel": "kernel", "random_state": "optional_nonnegative_int",
}


def _parse_parameter(name: str, raw: Any) -> Tuple[Optional[Any], Optional[str]]:
    if name not in _EST_PARAM_TYPES:
        return None, "unsupported parameter %s (supported: %s)" % (name, ", ".join(_EST_PARAM_TYPES))
    kind = _EST_PARAM_TYPES[name]
    if raw is None and kind.startswith("optional_"):
        return None, None
    if isinstance(raw, str):
        text = raw.strip()
        if text in {"None", "none", "null"} and kind.startswith("optional_"):
            return None, None
    else:
        text = str(raw).strip()
    try:
        if kind in {"positive_int", "optional_positive_int", "optional_nonnegative_int"}:
            if isinstance(raw, bool) or not re.fullmatch(r"[-+]?\d+", text):
                return None, "%s requires an integer, got %r" % (name, raw)
            value = int(text)
            if kind == "optional_nonnegative_int":
                valid = value >= 0
            else:
                valid = value > 0
            if not valid:
                return None, "%s is outside its supported positive/nonnegative integer domain" % name
            return value, None
        if kind in {"positive_float", "leaf"}:
            if isinstance(raw, bool):
                return None, "%s requires numeric input" % name
            value = _finite_float(text)
            if value is None:
                return None, "%s requires a finite number" % name
            if kind == "positive_float" and value <= 0:
                return None, "C must be > 0"
            if kind == "leaf":
                if re.fullmatch(r"[-+]?\d+", text):
                    value = int(text)
                    if value < 1:
                        return None, "integer min_samples_leaf must be >= 1"
                elif not 0 < value < 1:
                    return None, "fractional min_samples_leaf must be in (0,1)"
            return value, None
        if kind == "kernel":
            kernel = text.strip("'\"")
            if kernel not in {"linear", "poly", "rbf", "sigmoid"}:
                return None, "kernel must be linear, poly, rbf or sigmoid for this bounded Iris runner"
            return kernel, None
    except (TypeError, ValueError, OverflowError):
        pass
    return None, "could not safely parse %s=%r" % (name, raw)


def _parse_parameter_mapping(parameters: Any, allowed: Optional[Mapping[str, str]] = None) -> Tuple[Dict[str, Any], List[str], bool]:
    allowed = allowed or _EST_PARAM_TYPES
    if parameters is None:
        return {}, ["parameter mapping is missing"], False
    if isinstance(parameters, Mapping):
        parsed = {}
        errors = []
        for key, raw in parameters.items():
            if key not in allowed:
                errors.append("unsupported parameter " + str(key))
                continue
            if allowed is _EST_PARAM_TYPES:
                value, error = _parse_parameter(key, raw)
            else:
                value, error = _parse_split_parameter(key, raw)
            if error:
                errors.append(error)
            else:
                parsed[key] = value
        return parsed, errors, True
    text = str(parameters).strip()
    parsed = {}
    errors = []
    if not text:
        return parsed, errors, True
    for piece in text.split(","):
        piece = piece.strip()
        match = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(.+)", piece)
        if not match:
            errors.append("unsupported positional or malformed parameter segment: " + piece)
            continue
        key, raw = match.group(1), match.group(2).strip()
        if key not in allowed:
            errors.append("unsupported parameter " + key)
            continue
        value, error = _parse_parameter(key, raw) if allowed is _EST_PARAM_TYPES else _parse_split_parameter(key, raw)
        if error:
            errors.append(error)
        else:
            parsed[key] = value
    return parsed, errors, True


def _parse_split_parameter(name: str, raw: Any) -> Tuple[Optional[Any], Optional[str]]:
    if name in {"n_splits"}:
        return _parse_parameter("n_estimators", raw)
    if name == "random_state":
        return _parse_parameter("random_state", raw)
    if name == "shuffle":
        if isinstance(raw, bool):
            return raw, None
        text = str(raw).strip().lower()
        if text in {"true", "false"}:
            return text == "true", None
        return None, "shuffle requires true or false"
    if name == "test_size":
        value = _finite_float(raw)
        if value is None or not 0 < value < 1:
            return None, "test_size requires a finite fraction in (0,1) for this runner"
        return value, None
    return None, "unsupported splitter parameter " + name


def _estimator_specification(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, str):
        parsed = _text_estimators(entry)
        if len(parsed) == 1:
            return {**parsed[0], "structured": True}
        if parsed:
            return {"name": "unresolved", "parse_error": "one structured estimator string must contain exactly one supported constructor"}
        canonical = _ESTIMATOR_NAMES.get(_norm(entry.strip()))
        if canonical:
            return {"name": canonical, "params": {}, "structured": True, "explicit_defaults": True}
        return {"name": "unresolved", "parse_error": "unsupported or malformed structured estimator string"}
    if isinstance(entry, Mapping):
        name = str(entry.get("name", entry.get("class", entry.get("estimator", entry.get("type", "")))))
        canonical = _ESTIMATOR_NAMES.get(_norm(name))
        if not canonical:
            return {"name": "unresolved", "parse_error": "unsupported structured estimator mapping"}
        if "params" in entry or "parameters" in entry:
            params = entry.get("params", entry.get("parameters"))
        elif entry.get("use_defaults", entry.get("default_params", False)) is True:
            params = {}
        else:
            params = None
        return {"name": canonical, "params": params, "scale": entry.get("standardize", entry.get("scale", False)) is True, "structured": True}
    return {"name": "unresolved", "parse_error": "estimator specification must be a string or an object"}


def _structured_iris_configuration(case: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Optional[bool], List[str]]:
    estimators: List[Dict[str, Any]] = []
    splitter = None
    run_flag = None
    parse_errors: List[str] = []

    def visit(node: Any, depth: int) -> None:
        nonlocal splitter, run_flag
        if depth > 12 or not isinstance(node, (Mapping, list)):
            return
        if isinstance(node, list):
            for child in node:
                visit(child, depth + 1)
            return
        for key in ("reexecute", "run_experiment", "execute_experiment"):
            if isinstance(node.get(key), bool):
                run_flag = node[key]
        candidate_estimators = node.get("estimators")
        if not estimators and isinstance(candidate_estimators, list):
            for entry in candidate_estimators:
                specification = _estimator_specification(entry)
                if specification.get("parse_error"):
                    parse_errors.append(specification["parse_error"])
                else:
                    estimators.append(specification)
        for key in ("splitter", "cv", "cross_validation"):
            candidate = node.get(key)
            if splitter is not None:
                continue
            if isinstance(candidate, Mapping):
                name = candidate.get("name", candidate.get("type", candidate.get("class", candidate.get("splitter"))))
                if _norm(name) in {"stratifiedkfold", "kfold", "shufflesplit"}:
                    params = candidate.get("params", candidate.get("parameters", {k: v for k, v in candidate.items() if k not in {"name", "type", "class", "splitter"}}))
                    splitter = {"name": str(name), "params": params}
            elif isinstance(candidate, str) and _norm(candidate.split("(")[0]) in {"stratifiedkfold", "kfold", "shufflesplit"}:
                splitter = {"text": candidate}
        for key in sorted(node, key=lambda item: str(item)):
            visit(node[key], depth + 1)

    visit(case, 0)
    return estimators, splitter, run_flag, parse_errors


def _text_estimators(text: str) -> List[Dict[str, Any]]:
    found = []
    positions = []
    for alias, canonical in _ESTIMATOR_NAMES.items():
        pattern = re.escape(alias) + r"\s*\(([^()]{0,1000})\)"
        for match in re.finditer(pattern, text, re.I):
            positions.append((match.start(), canonical, match.group(1)))
    for _, canonical, parameters in sorted(positions):
        if any(item["name"] == canonical for item in found):
            continue
        found.append({"name": canonical, "params": parameters, "scale": False, "structured": False})
    return found


def _text_splitter(text: str) -> Optional[Dict[str, Any]]:
    matches = []
    for name in ("StratifiedKFold", "KFold", "ShuffleSplit"):
        for match in re.finditer(re.escape(name) + r"\s*\(([^()]{0,1000})\)", text, re.I):
            matches.append((match.start(), name, match.group(1)))
    if not matches:
        return None
    _, name, parameters = sorted(matches)[0]
    return {"name": name, "params": parameters}


def _iris_configuration(self: _Analysis, task_text: str) -> Dict[str, Any]:
    detected = bool(re.search(r"\biris\b|load_iris|鸢尾花", task_text, re.I))
    structured_estimators, structured_splitter, run_flag, structured_errors = _structured_iris_configuration(self.case)
    text_estimators = _text_estimators(task_text)
    text_splitter = _text_splitter(task_text)
    estimators = structured_estimators or text_estimators
    splitter = structured_splitter or text_splitter

    def estimator_signature(specification: Mapping[str, Any]) -> Tuple[str, str]:
        parameters, _, _ = _parse_parameter_mapping(specification.get("params"))
        return str(specification.get("name")), _json_key(parameters)

    def splitter_signature(specification: Mapping[str, Any]) -> Tuple[str, str]:
        if specification.get("text"):
            specification = _text_splitter(specification["text"])
        name = str(specification.get("name", ""))
        allowed = {"n_splits": "positive_int", "random_state": "optional_nonnegative_int", "shuffle": "bool", "test_size": "fraction"}
        parameters, _, _ = _parse_parameter_mapping(specification.get("params"), allowed)
        return _norm(name), _json_key(parameters)

    inconsistent_estimators = bool(structured_estimators and text_estimators and sorted(estimator_signature(item) for item in structured_estimators) != sorted(estimator_signature(item) for item in text_estimators))
    inconsistent_splitter = bool(structured_splitter and text_splitter and splitter_signature(structured_splitter) != splitter_signature(text_splitter))
    negative = bool(re.search(r"do\s+not\s+(?:re[- ]?)?(?:run|execute|fit|train|compute)|no\s+new\s+(?:experiment|model|fit)|without\s+(?:re[- ]?)?(?:execut\w*|fitt\w*|train\w*|run\w*)|(?:不要|不允许|无需|禁止)(?:重新|再次)?(?:运行|训练|计算|拟合)|不(?:需要|必)(?:重新|再次)?(?:运行|训练|计算|拟合)", task_text, re.I))
    invitation = run_flag is True or bool(re.search(r"\b(?:reexecute|re-execute|execute|run|compute|perform|fit|train|evaluate|reproduce)\b.{0,160}\biris\b|\biris\b.{0,160}\b(?:reexecute|re-execute|execute|run|compute|perform|fit|train|evaluate|reproduce)\b|(?:重新|复现|运行|训练|计算|评估).{0,100}(?:Iris|鸢尾花)|(?:Iris|鸢尾花).{0,100}(?:重新|复现|运行|训练|计算|评估)", task_text, re.I))
    config = {"detected": detected, "invitation": invitation and not negative and run_flag is not False, "estimators": [], "splitter": None, "errors": list(structured_errors), "scale_all": bool(re.search(r"StandardScaler|standardiz|standardis|标准化", task_text, re.I)) and not bool(re.search(r"without\s+(?:standardiz|scaling)|不标准化", task_text, re.I))}
    if inconsistent_estimators:
        config["errors"].append("case and task_text specify different estimators/estimator parameters; no winning configuration was chosen")
    if inconsistent_splitter:
        config["errors"].append("case and task_text specify different splitters/split parameters; no winning configuration was chosen")
    if not detected:
        return config
    if len(estimators) != 2 or len({item.get("name") for item in estimators}) != 2:
        config["errors"].append("formal Iris reexecution requires exactly two distinct supported estimator configurations; no estimator was substituted")
    for estimator in estimators[:2]:
        params, errors, present = _parse_parameter_mapping(estimator.get("params"))
        config["errors"].extend(estimator["name"] + ": " + error for error in errors)
        config["estimators"].append({"name": estimator["name"], "params": params, "parameter_configuration_present": present, "scale": estimator.get("scale", False)})
    if not estimators:
        config["errors"].append("no explicit supported estimator constructor/configuration was parsed")
    if splitter is None:
        config["errors"].append("no explicit StratifiedKFold, KFold or ShuffleSplit constructor/configuration was parsed")
    else:
        if splitter.get("text") and not splitter.get("name"):
            splitter = _text_splitter(splitter["text"])
        name = next((canonical for canonical in ("StratifiedKFold", "KFold", "ShuffleSplit") if _norm(canonical) == _norm(splitter.get("name", ""))), None)
        if name is None:
            config["errors"].append("unknown splitter name")
        else:
            allowed = {"n_splits": "positive_int", "random_state": "optional_nonnegative_int"}
            if name in {"StratifiedKFold", "KFold"}:
                allowed["shuffle"] = "bool"
            if name == "ShuffleSplit":
                allowed["test_size"] = "fraction"
            params, errors, present = _parse_parameter_mapping(splitter.get("params"), allowed)
            config["errors"].extend(name + ": " + error for error in errors)
            config["splitter"] = {"name": name, "params": params, "parameter_configuration_present": present}
    for estimator in config["estimators"]:
        if estimator["name"] in {"DecisionTreeClassifier", "RandomForestClassifier"} and estimator["params"].get("random_state") is None:
            config["errors"].append(estimator["name"] + " requires an explicit integer random_state for deterministic formal reexecution")
    split = config["splitter"]
    if split:
        n_splits = split["params"].get("n_splits")
        if n_splits is None:
            config["errors"].append("splitter n_splits was not explicit; no default was substituted")
        elif n_splits != 5:
            config["errors"].append("formal Iris reexecution requires exactly five shared splits, not n_splits=" + str(n_splits))
        if split["name"] == "ShuffleSplit":
            if "test_size" not in split["params"]:
                config["errors"].append("ShuffleSplit test_size was not explicit; no default was substituted")
            if split["params"].get("random_state") is None:
                config["errors"].append("ShuffleSplit requires an explicit random_state")
        elif split["params"].get("shuffle") is True and split["params"].get("random_state") is None:
            config["errors"].append("shuffled K-fold splitting requires an explicit splitter random_state; estimator seeds are not reused")
        elif not split["params"].get("shuffle", False) and split["params"].get("random_state") is not None:
            config["errors"].append("a splitter random_state with shuffle=False is ambiguous/invalid; clarify the requested configuration")
    return config


def _iris_reexecute(self: _Analysis, config: Mapping[str, Any]) -> None:
    limits = self.result["computational_results"]["limits"]
    if not config["detected"]:
        return
    base = {"dataset": "sklearn Iris", "request_detected": True, "reexecution_invited": config["invitation"]}
    if not config["invitation"]:
        limits.append({**base, "state": "not_reexecuted", "reason": "No unambiguous input invitation to solve/run a new experiment was parsed, or reexecution was explicitly prohibited.", "action": "Keep supplied predictions/results separate; request explicit run authorization if new fitting is intended."})
        return
    if config["errors"]:
        limits.append({**base, "state": "blocked_missing_or_invalid_configuration", "configuration_errors": config["errors"], "parsed_estimators": config["estimators"], "parsed_splitter": config["splitter"], "action": "Supply explicit supported estimator constructors (including deterministic seeds where required) and one shared splitter with n_splits plus shuffle/seed/test_size as applicable. No alternative estimators, accuracy values, split parameters or library defaults were guessed."})
        self.issue("iris_configuration_blocked", "Explicit Iris computation was recognized but not reexecuted because its configuration is incomplete, unsupported or ambiguous.", details={"errors": config["errors"], "parsed_estimators": config["estimators"], "parsed_splitter": config["splitter"]})
        return
    try:
        import numpy as np
        import sklearn
        from sklearn.base import clone
        from sklearn.datasets import load_iris
        from sklearn.tree import DecisionTreeClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.svm import SVC
        from sklearn.neighbors import KNeighborsClassifier
        from sklearn.naive_bayes import GaussianNB
        from sklearn.model_selection import StratifiedKFold, KFold, ShuffleSplit
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
    except Exception as exc:
        limits.append({**base, "state": "dependency_failure", "error": str(exc), "action": "Install a compatible numpy/scikit-learn runtime; no reported accuracy was fabricated."})
        self.issue("iris_dependency_error", "scikit-learn Iris reexecution dependencies could not be imported.", error=str(exc))
        return
    classes = {"LogisticRegression": LogisticRegression, "DecisionTreeClassifier": DecisionTreeClassifier, "RandomForestClassifier": RandomForestClassifier, "SVC": SVC, "KNeighborsClassifier": KNeighborsClassifier, "GaussianNB": GaussianNB}
    split_classes = {"StratifiedKFold": StratifiedKFold, "KFold": KFold, "ShuffleSplit": ShuffleSplit}
    try:
        iris = load_iris(as_frame=False)
        x = np.asarray(iris.data)
        y = np.asarray(iris.target)
        target_names = list(iris.target_names)
        feature_names = list(iris.feature_names)
        splitter_name = config["splitter"]["name"]
        split_params = dict(config["splitter"]["params"])
        splitter = split_classes[splitter_name](**split_params)
        splits = []
        split_warnings = []
        with _stdlib_warnings.catch_warnings(record=True) as caught:
            _stdlib_warnings.simplefilter("always")
            for train, test in splitter.split(x, y):
                splits.append((np.asarray(train, dtype=int), np.asarray(test, dtype=int)))
        split_warnings = [{"category": item.category.__name__, "message": str(item.message)} for item in caught]
        expected = config["splitter"]["params"]["n_splits"]
        if len(splits) != expected:
            raise ValueError("splitter produced %d splits, expected explicit n_splits=%d" % (len(splits), expected))
        split_hasher = hashlib.sha256()
        for train, test in splits:
            split_hasher.update(b"train\0")
            split_hasher.update(np.ascontiguousarray(train, dtype=np.int64).tobytes(order="C"))
            split_hasher.update(b"test\0")
            split_hasher.update(np.ascontiguousarray(test, dtype=np.int64).tobytes(order="C"))
        shared_split_id = split_hasher.hexdigest()
        digest = hashlib.sha256()
        digest.update(np.ascontiguousarray(x).tobytes(order="C"))
        digest.update(np.ascontiguousarray(y).tobytes(order="C"))
        digest.update((str(x.shape) + str(x.dtype) + str(y.shape) + str(y.dtype)).encode("utf-8"))
        dataset_record = {
            "loader": "sklearn.datasets.load_iris(as_frame=False)", "sklearn_version": sklearn.__version__, "numpy_version": np.__version__,
            "shape": list(x.shape), "target_shape": list(y.shape), "dtypes": {"X": str(x.dtype), "y": str(y.dtype)},
            "feature_names": feature_names, "target_names": target_names,
            "target_balance": {str(target_names[index]) if index < len(target_names) else str(index): int(np.count_nonzero(y == index)) for index in sorted(set(y.tolist()))},
            "xy_sha256": digest.hexdigest(), "hash_method": "SHA-256 over C-contiguous loaded X bytes, y bytes, then shape/dtype text",
        }
        self.fact("observed", "sklearn_iris_dataset", {"dataset": "sklearn Iris"}, dataset_record, None, "actual load_iris call and in-memory hashing", ["sklearn.datasets.load_iris(as_frame=False); xy_sha256=" + digest.hexdigest()], "The hash identifies this runtime's loaded arrays and dtypes; it is not an external benchmark answer key.")
        limits.append({**base, "state": "reexecuted", "sklearn_version": sklearn.__version__, "dataset_sha256": digest.hexdigest(), "splitter": {"class": splitter_name, "params": split_params, "warnings": split_warnings}, "shared_split_indices": "identical materialized train/test indices supplied independently to every estimator", "shared_split_id": shared_split_id, "seed_policy": "Splitter random_state comes only from splitter configuration; estimator random_state values remain in each estimator configuration and are never copied between roles.", "scaling": "StandardScaler, when requested, occurs inside each training-fold Pipeline only."})
    except Exception as exc:
        limits.append({**base, "state": "split_or_data_failure", "error": str(exc), "action": "Inspect the actual load_iris arrays and explicit splitter configuration."})
        self.issue("iris_split_error", "Iris data loading or split materialization failed; no model results were claimed.", error=str(exc), exception=type(exc).__name__)
        return
    for estimator_spec in config["estimators"]:
        name = estimator_spec["name"]
        params = dict(estimator_spec["params"])
        try:
            estimator = classes[name](**params)
            scale = config["scale_all"] or estimator_spec.get("scale", False)
            model = Pipeline([("standard_scaler", StandardScaler()), ("estimator", clone(estimator))]) if scale else estimator
            run = {
                "run_id": "reexecuted:" + name, "dataset": "sklearn Iris", "dataset_provenance": dataset_record,
                "estimator": name, "requested_params": params, "estimator_params": _safe(estimator.get_params(deep=True)),
                "pipeline_params": _safe(model.get_params(deep=True)), "preprocessing": "Pipeline(StandardScaler -> estimator), fitted within training fold" if scale else "none",
                "splitter": {"class": splitter_name, "params": split_params, "random_state": split_params.get("random_state")}, "shared_split_id": shared_split_id,
                "estimator_random_state": params.get("random_state"), "source": "explicit_task_configuration", "state": "reexecuted", "execution": "reexecuted",
                "folds": [], "warnings": [], "status": "complete", "software": {"sklearn": sklearn.__version__, "numpy": np.__version__},
            }
            for fold_index, (train, test) in enumerate(splits):
                fold = {
                    "fold": fold_index, "fold_index_base": 0, "train_indices_zero_based": [int(value) for value in train],
                    "test_indices_zero_based": [int(value) for value in test], "n_train": int(len(train)), "n_test": int(len(test)),
                    "samples": [], "warnings": [], "exit": "not_run", "error": None,
                }
                try:
                    fitted = clone(model)
                    with _stdlib_warnings.catch_warnings(record=True) as caught:
                        _stdlib_warnings.simplefilter("always")
                        fitted.fit(x[train], y[train])
                        predicted = np.asarray(fitted.predict(x[test]))
                    fold_warnings = [{"category": item.category.__name__, "message": str(item.message)} for item in caught]
                    actual_values = y[test]
                    correct = predicted == actual_values
                    correct_count = int(np.count_nonzero(correct))
                    accuracy = correct_count / len(test) if len(test) else None
                    fold.update({
                        "exit": "success", "n_scored": int(len(test)), "n_missing_labels": 0, "correct_count": correct_count, "accuracy": accuracy,
                        "confusion": _confusion(actual_values.tolist(), predicted.tolist()),
                        "samples": [{"sample_key": int(index), "sample_pair_basis": "physical_iris_row_index_zero_based", "test_position": position, "y_true": int(actual_values[position]), "y_pred": int(predicted[position]), "correct": bool(correct[position])} for position, index in enumerate(test)],
                        "warnings": fold_warnings, "metrics_state": "reexecuted_and_computed_from_new_predictions",
                    })
                    run["warnings"].extend([{"fold": fold_index, **warning} for warning in fold_warnings])
                except Exception as exc:
                    fold.update({"exit": "error", "error": str(exc), "exception": type(exc).__name__, "correct_count": None, "accuracy": None, "metrics_state": "fit_or_predict_failed"})
                    run["status"] = "partial_fit_failure"
                run["folds"].append(fold)
                self.fact("derived", "classification_accuracy", {"dataset": "sklearn Iris", "estimator": name, "fold": fold_index, "run_state": "reexecuted"}, {"correct_count": fold.get("correct_count"), "n_scored": fold.get("n_scored"), "accuracy": fold.get("accuracy"), "exit": fold["exit"], "error": fold.get("error")}, None, "new fold fit then sum(test y_true == predicted)/n_test; shared physical split indices", ["reexecuted run_id=reexecuted:" + name + " fold=" + str(fold_index) + "; dataset_sha256=" + dataset_record["xy_sha256"]], "Fold estimates have overlapping training sets and are not independent replicates; no ordinary t-test or independent-fold CI is valid from this design alone.")
            self.result["computational_results"]["runs"].append(run)
        except Exception as exc:
            self.result["computational_results"]["runs"].append({"run_id": "reexecuted:" + name, "dataset": "sklearn Iris", "estimator": name, "requested_params": params, "state": "reexecuted", "execution": "reexecuted", "status": "configuration_or_clone_failure", "error": str(exc), "folds": []})
            self.issue("iris_estimator_error", "Estimator construction failed before a complete run; other estimator results are retained.", details={"estimator": name, "error": str(exc), "exception": type(exc).__name__})
    self.method("shared_split_iris_reexecution", "Load the actual sklearn Iris arrays once, materialize one explicit splitter, clone each requested estimator for every fit, and calculate predictions/correct counts/accuracy/confusion matrices independently for each fold.", "Determinism depends on explicit requested seeds. Standardization is fold-internal when requested. This is not Dietterich 5x2cv and no nested-CV experiment is run.")
    limits.append({"state": "design_recommendation_not_executed", "recommendation": "If estimator/hyperparameter selection is to be compared as a generalization claim, preregister nested CV with an independent outer loop; this module did not run nested CV or overwrite the requested folds.", "reason": "Model selection on the same test folds optimistically biases estimated performance."})


def _paired_comparisons(self: _Analysis) -> None:
    runs_by_state: Dict[Tuple[str, str], List[Mapping[str, Any]]] = defaultdict(list)
    for run in self.result["computational_results"]["runs"]:
        if run.get("state") in {"recomputed", "reexecuted"} and run.get("folds"):
            key = (run["state"], str(run.get("table_id", "sklearn Iris")))
            runs_by_state[key].append(run)
    for (state, source_key), runs in sorted(runs_by_state.items(), key=lambda item: item[0]):
        runs = sorted(runs, key=lambda run: str(run.get("estimator")))
        if len(runs) < 2:
            continue
        if len(runs) > 2:
            self.issue("paired_comparison_ambiguous", "More than two runs share this state/source; no arbitrary pair was selected for paired comparison.", details={"state": state, "source": source_key, "estimators": [run.get("estimator") for run in runs]})
            continue
        left, right = runs
        left_folds = {_json_key(fold.get("fold")): fold for fold in left["folds"] if fold.get("accuracy") is not None}
        right_folds = {_json_key(fold.get("fold")): fold for fold in right["folds"] if fold.get("accuracy") is not None}
        common = sorted(set(left_folds) & set(right_folds))
        records = []
        wins = losses = ties = 0
        paired_sample_count = disagreements = left_only_correct = right_only_correct = 0
        for key in common:
            a = left_folds[key]
            b = right_folds[key]
            difference = a["accuracy"] - b["accuracy"]
            wins += difference > 0
            losses += difference < 0
            ties += difference == 0
            left_map = {_json_key(sample.get("sample_key")): sample for sample in a.get("samples", [])}
            right_map = {_json_key(sample.get("sample_key")): sample for sample in b.get("samples", [])}
            shared_samples = sorted(set(left_map) & set(right_map))
            pair_basis = sorted({left_map[sample].get("sample_pair_basis") for sample in shared_samples} | {right_map[sample].get("sample_pair_basis") for sample in shared_samples})
            for sample_key in shared_samples:
                sample_a = left_map[sample_key]
                sample_b = right_map[sample_key]
                paired_sample_count += 1
                if _label_equal(sample_a.get("y_pred"), sample_b.get("y_pred")) is False:
                    disagreements += 1
                if sample_a.get("correct") is True and sample_b.get("correct") is False:
                    left_only_correct += 1
                if sample_b.get("correct") is True and sample_a.get("correct") is False:
                    right_only_correct += 1
            records.append({
                "fold": json.loads(key), "left_accuracy": a["accuracy"], "right_accuracy": b["accuracy"],
                "left_correct_count": a["correct_count"], "right_correct_count": b["correct_count"], "n_scored_left": a.get("n_scored"), "n_scored_right": b.get("n_scored"),
                "difference_left_minus_right": difference, "percentage_point_difference": 100.0 * difference,
                "relative_difference": difference / abs(b["accuracy"]) if b["accuracy"] not in (None, 0) else None,
                "left_over_right_ratio": a["accuracy"] / b["accuracy"] if b["accuracy"] not in (None, 0) else None,
                "winner": "left" if difference > 0 else "right" if difference < 0 else "tie",
                "paired_samples_n": len(shared_samples), "sample_pair_basis": pair_basis,
            })
        if not records:
            continue
        differences = [record["difference_left_minus_right"] for record in records]
        ranks = []
        for record in sorted(records, key=lambda item: (-item["left_accuracy"], _json_key(item["fold"]))):
            tied = [candidate for candidate in records if candidate["left_accuracy"] == record["left_accuracy"]]
            average_rank = (sum(sorted([candidate["left_accuracy"] for candidate in records], reverse=True).index(record["left_accuracy"]) + 1 for _ in tied)) / len(tied)
            ranks.append({"fold": record["fold"], "estimator": left.get("estimator"), "rank": average_rank, "ties": len(tied)})
        for record in records:
            tied = [candidate for candidate in records if candidate["right_accuracy"] == record["right_accuracy"]]
            average_rank = (sum(sorted([candidate["right_accuracy"] for candidate in records], reverse=True).index(record["right_accuracy"]) + 1 for _ in tied)) / len(tied)
            ranks.append({"fold": record["fold"], "estimator": right.get("estimator"), "rank": average_rank, "ties": len(tied)})
        left_accuracies = [record["left_accuracy"] for record in records]
        right_accuracies = [record["right_accuracy"] for record in records]
        comparison_evidence = []
        for run in (left, right):
            fold_labels = [fold.get("fold") for fold in run["folds"]]
            if run.get("path"):
                comparison_evidence.append(_location(run.get("path"), run.get("sheet")) + " run_id=" + str(run.get("run_id")) + " paired_folds=" + json.dumps(_safe(fold_labels), ensure_ascii=False))
            else:
                provenance = run.get("dataset_provenance", {})
                comparison_evidence.append(str(provenance.get("loader", "sklearn.datasets.load_iris")) + "; dataset_sha256=" + str(provenance.get("xy_sha256")) + "; run_id=" + str(run.get("run_id")) + "; physical split indices retained per fold")
        comparison = {
            "state": state, "source": source_key, "left_estimator": left.get("estimator"), "right_estimator": right.get("estimator"),
            "metric": "paired per-fold accuracy difference (left - right)", "folds": records, "n_paired_folds": len(records),
            "wins": wins, "losses": losses, "ties": ties,
            "difference_range": [min(differences), max(differences)], "mean_difference": math.fsum(differences) / len(differences), "median_difference": _median(differences),
            "mean_percentage_point_difference": 100.0 * math.fsum(differences) / len(differences),
            "left_accuracy_range": [min(left_accuracies), max(left_accuracies)], "right_accuracy_range": [min(right_accuracies), max(right_accuracies)],
            "fold_ranks_descending_accuracy": sorted(ranks, key=lambda item: (_json_key(item["fold"]), str(item["estimator"]))),
            "paired_samples_n": paired_sample_count, "prediction_disagreements": disagreements,
            "prediction_disagreement_fraction": disagreements / paired_sample_count if paired_sample_count else None,
            "left_only_correct": left_only_correct, "right_only_correct": right_only_correct,
            "method": "same-fold and same-sample pairing; folds are not ordered as time and no t-test/independent CI is calculated",
            "evidence": sorted(comparison_evidence),
            "limitations": ["Cross-validation training sets overlap; five fold scores are not independent experimental replicates.", "Ranks/ranges/win-loss and point differences are descriptive, not calibrated significance tests.", "ShuffleSplit can reuse test samples across folds; paired sample counts are fold-sample occurrences, not unique independent subjects."],
        }
        self.result["computational_results"]["paired_comparison"].append(comparison)
        self.fact("derived", "paired_classification_accuracy_comparison", {"state": state, "source": source_key, "left_estimator": left.get("estimator"), "right_estimator": right.get("estimator")}, {key: comparison[key] for key in ("n_paired_folds", "wins", "losses", "ties", "difference_range", "mean_difference", "median_difference", "mean_percentage_point_difference", "paired_samples_n", "prediction_disagreements", "prediction_disagreement_fraction")}, None, comparison["method"], comparison["evidence"], "Paired CV differences are descriptive; train overlap, reused samples and model-selection bias preclude independent-fold inference.")


def _computational_analysis(self: _Analysis, task_text: str) -> None:
    prediction_records = _recompute_prediction_tables(self)
    _reported_classification_tables(self)
    config = _iris_configuration(self, task_text)
    _iris_reexecute(self, config)
    _paired_comparisons(self)
    self.result["coverage"]["records"]["prediction_records_recomputed"] = prediction_records
    if any(run.get("state") in {"recomputed", "reexecuted"} for run in self.result["computational_results"]["runs"]):
        self.result["coverage"]["limitations"].append("Classification fold comparisons are paired descriptive calculations, not time trends, independent confidence intervals or Dietterich 5x2cv tests.")
    if not self.result["computational_results"]["runs"] and not config["detected"]:
        self.result["computational_results"]["limits"].append({"state": "not_applicable", "reason": "No supported provided-prediction/aggregate classification result table or explicit Iris experiment was detected."})


def analyze(data: dict, case: dict, work_dir: Path, *, task_text: Optional[str] = None) -> dict:
    """Return deterministic facts from the package and the actual optional task statement."""
    engine = _Analysis(data if isinstance(data, Mapping) else {}, case if isinstance(case, Mapping) else {}, Path(work_dir))
    if not isinstance(data, Mapping):
        engine.issue("invalid_data_package", "data was not a mapping; an empty-input result was produced.")
    if not isinstance(case, Mapping):
        engine.issue("invalid_case", "case was not a mapping; no task-specific configuration was inferred.")
    try:
        engine.prepare_tables()
    except Exception as exc:
        engine.tables = []
        engine.issue("table_preparation_error", "Table preparation failed; document/input facts will still be attempted.", error=str(exc), exception=type(exc).__name__)
    all_tables = list(engine.tables)
    for table in all_tables:
        try:
            engine.tables = [table]
            engine.summarize_tables()
        except Exception as exc:
            engine.issue("table_summary_error", "One table summary failed; remaining tables were preserved.", table.get("path"), table_id=table.get("id"), error=str(exc), exception=type(exc).__name__)
        finally:
            engine.tables = all_tables
    try:
        _prepare_curve_groups(engine)
    except Exception as exc:
        engine.issue("curve_preparation_error", "Source/metadata curve preparation failed; generic table summaries remain available and no source matching was guessed.", error=str(exc), exception=type(exc).__name__)
    for table in all_tables:
        for function, kind in ((engine.screen_table_outliers, "outlier_screen_error"), (engine.median_polish_table, "median_polish_error"), (engine.trends_for_table, "trend_analysis_error")):
            try:
                function(table)
            except Exception as exc:
                engine.issue(kind, "One localized table calculation failed; its summary and all other tables remain available.", table.get("path"), table_id=table.get("id"), error=str(exc), exception=type(exc).__name__)
    try:
        _curve_analysis(engine)
    except Exception as exc:
        engine.issue("curve_analysis_error", "The curve branch failed; generic summaries and source/experiment branches remain available.", error=str(exc), exception=type(exc).__name__)
        engine.result["coverage"].setdefault("series", {"groups": 0, "traces": 0, "status": "failed"})
    try:
        _source_analysis(engine)
    except Exception as exc:
        engine.issue("source_analysis_error", "Source auditing encountered a branch failure; already calculated table/curve facts remain valid.", error=str(exc), exception=type(exc).__name__)
    other_task_text = _case_text(data.get("other_files", [])) if isinstance(data, Mapping) else ""
    supplied_task_text = task_text if isinstance(task_text, str) else _case_text(task_text) if task_text is not None else ""
    combined_task_text = supplied_task_text + "\n" + _case_text(case) + "\n" + other_task_text
    try:
        _computational_analysis(engine, combined_task_text)
    except Exception as exc:
        engine.issue("computational_analysis_error", "The computational-experiment branch failed; generic summaries remain available.", error=str(exc), exception=type(exc).__name__)
        engine.result["computational_results"]["limits"].append({"state": "branch_failure", "error": str(exc), "action": "Inspect the recorded error; no replacement scores were inferred."})
    remaining_summaries = Counter(summary.get("table_id") for summary in engine.result["table_summaries"])
    failed_tables = []
    for table in all_tables:
        if remaining_summaries[table.get("id")] > 0:
            remaining_summaries[table.get("id")] -= 1
        else:
            failed_tables.append({"table_id": table.get("id"), "path": table.get("path"), "sheet": table.get("sheet")})
    engine.result["coverage"]["tables"] = {
        "total": len(all_tables), "summarized": len(engine.result["table_summaries"]), "failed": failed_tables,
        "table_ids": [table.get("id") for table in all_tables], "columns": sum(len(table["_names"]) for table in all_tables),
        "numeric_columns": sum(sum(column.get("count_numeric", 0) > 0 for column in summary["columns"]) for summary in engine.result["table_summaries"]),
    }
    engine.result["coverage"]["records"].update({
        "table_rows": sum(len(table["_rows"]) for table in all_tables),
        "facts": len(engine.result["facts"]), "outlier_candidates": len(engine.result["outlier_candidates"]),
        "trend_candidates": len(engine.result["trend_candidates"]), "computational_runs": len(engine.result["computational_results"]["runs"]),
    })
    engine.result["coverage"]["limitations"] = sorted(set(engine.result["coverage"].get("limitations", [])) | engine._limitations)
    if engine.result["issues"]:
        engine.result["coverage"]["limitations"].append("Some issues/insufficient groups or bounded analyses are recorded; inspect issues before interpreting coverage as complete.")
        engine.result["coverage"]["limitations"] = sorted(set(engine.result["coverage"]["limitations"]))
    engine.result["artifacts"] = sorted(engine.result["artifacts"])
    engine.result["methods"] = sorted(engine.result["methods"], key=lambda item: item["name"])
    for key in ("outlier_candidates", "trend_candidates", "source_anomalies"):
        engine.result[key] = sorted(engine.result[key], key=lambda item: str(item.get("id", "")))
    engine.result["computational_results"]["runs"] = sorted(engine.result["computational_results"]["runs"], key=lambda item: str(item.get("run_id", "")))
    engine.result["computational_results"]["paired_comparison"] = sorted(engine.result["computational_results"]["paired_comparison"], key=lambda item: (str(item.get("state")), str(item.get("source")), str(item.get("left_estimator")), str(item.get("right_estimator"))))
    result = _safe(engine.result)
    json.dumps(result, ensure_ascii=False, allow_nan=False)
    return result


__all__ = ["analyze"]
