"""Declared binding-isotherm regression with source-linked ordinary least squares."""
from __future__ import annotations

import math
import platform
import re
from collections.abc import Mapping, Sequence


_MODEL_ID = "binding_unbound_fraction_one_to_one_eq11"
_OPERATION = "fit_declared_model"
_MAX_ROWS_PER_GROUP = 1000
_MAX_NFEV = 500
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_NUMERIC = re.compile(_NUMBER)
_L0 = re.compile(r"^\s*L\s*_?0\s*=\s*(" + _NUMBER + r")\s*([^\s=]+)\s*$", re.I)
_ERROR = re.compile(
    r"^\s*Random\s+error\s+in\s+([LT])\s*_?0\s*=\s*(" + _NUMBER + r")\s*$",
    re.I,
)
_XLSX_ROW = re.compile(r"^(?P<sheet>.*)!(?P<first>[A-Z]+)(?P<row>\d+):(?P<last>[A-Z]+)(?P<last_row>\d+)$")


class _FitFailure(Exception):
    pass


class _DependencyUnavailable(Exception):
    pass


def _safe(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return _safe(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "tolist") and callable(value.tolist):
        try:
            return _safe(value.tolist())
        except (TypeError, ValueError):
            pass
    return str(value)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        if not _NUMERIC.fullmatch(value.strip()):
            return None
        value = value.strip()
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _fact(kind, metric, entity, value, unit, method, evidence, uncertainty):
    return {
        "kind": kind,
        "metric": metric,
        "entity": entity,
        "value": value,
        "unit": unit,
        "method": method,
        "evidence": list(dict.fromkeys(str(item) for item in evidence if item not in (None, ""))),
        "uncertainty": uncertainty,
    }


def _issue(kind, stage, detail, evidence=(), **details):
    item = {"kind": kind, "stage": stage, "detail": detail}
    if evidence:
        item["evidence"] = list(dict.fromkeys(str(value) for value in evidence if value not in (None, "")))
    item.update(details)
    return item


def _deduplicated_header(value):
    return re.sub(r"__\d+$", "", str(value).strip())


def _header_role(value):
    text = _deduplicated_header(value)
    if re.fullmatch(r"T\s*_?0(?:\s*(?:\([^)]*\)|\[[^]]*\]))?", text, re.I):
        return "T0"
    if re.fullmatch(r"R", text, re.I):
        return "R"
    if re.fullmatch(r"error\s+in\s+R", text, re.I):
        return "error_in_R"
    return None


def _header_unit(value):
    text = _deduplicated_header(value)
    match = re.search(r"(?:\(\s*([^()]+?)\s*\)|\[\s*([^][]+?)\s*\])\s*$", text)
    if not match:
        return None
    return next((item.strip() for item in match.groups() if item and item.strip()), None)


def _unit_key(value):
    if value in (None, ""):
        return None
    return re.sub(r"\s+", "", str(value)).replace("µ", "u").replace("μ", "u").casefold()


def _a1_number(letters):
    result = 0
    for letter in letters:
        result = result * 26 + ord(letter) - 64
    return result


def _a1_letter(index):
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _row_ref(table, index, rows=None):
    refs = table.get("row_refs") or []
    if 0 <= index < len(refs):
        ref = refs[index]
        if isinstance(ref, Mapping):
            ref = next((ref[key] for key in ("ref", "locator", "address", "range", "row") if ref.get(key) is not None), None)
        if ref not in (None, ""):
            return str(ref)
    metadata = table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}
    return str(metadata.get("row_ref") or table.get("name") or table.get("id") or "unknown-row")


def _cell_ref(row_ref, column):
    match = _XLSX_ROW.fullmatch(str(row_ref))
    if not match or int(match.group("row")) != int(match.group("last_row")):
        return "%s | column_zero_based=%d" % (row_ref, column)
    first = _a1_number(match.group("first")) - 1
    last = _a1_number(match.group("last")) - 1
    if not first <= column <= last:
        return "%s | column_zero_based=%d" % (row_ref, column)
    return "%s!%s%s" % (match.group("sheet"), _a1_letter(column), match.group("row"))


def _evidence(table, ref):
    text = str(ref)
    path = str(table.get("path") or "<unknown-path>")
    sheet = table.get("sheet")
    if text.startswith(path):
        return text
    if sheet not in (None, "") and text.startswith(str(sheet) + "!"):
        return path + "#" + text
    if re.match(r"^.*![A-Z]+\d+(?::[A-Z]+\d+)?$", text):
        return path + "#" + text
    return path + ("#" + str(sheet) if sheet not in (None, "") else "") + " | " + text


def _triplets(values):
    roles = [_header_role(value) for value in values]
    result = []
    for index in range(max(0, len(values) - 2)):
        if roles[index:index + 3] == ["T0", "R", "error_in_R"]:
            result.append(index)
    return result


def _row_list(table):
    rows = table.get("rows")
    return rows if isinstance(rows, list) else []


def _row_values(row):
    return list(row) if isinstance(row, (list, tuple)) else []


def _cell(values, index):
    return values[index] if index < len(values) else None


def _nearest_l0_row(rows, header_index):
    if header_index is None:
        return None
    for index in range(header_index - 1, -1, -1):
        values = _row_values(rows[index])
        if any(isinstance(value, str) and _L0.fullmatch(value) for value in values):
            return index
    return None


def _metadata_header_rows(table):
    metadata = table.get("metadata") if isinstance(table.get("metadata"), Mapping) else {}
    result = []

    def visit(node):
        if isinstance(node, Mapping):
            values = next((node[key] for key in ("values", "cells") if isinstance(node.get(key), list)), None)
            ref = next((node[key] for key in ("row_ref", "ref", "locator") if node.get(key) not in (None, "")), None)
            if values is not None and ref is not None:
                result.append((values, str(ref)))
            for value in node.values():
                if isinstance(value, (Mapping, list, tuple)):
                    visit(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                if isinstance(value, (Mapping, list, tuple)):
                    visit(value)

    visit(metadata)
    return result


def _error_records(rows_with_refs):
    records = []
    for values, ref in rows_with_refs:
        for column, value in enumerate(values):
            if not isinstance(value, str):
                continue
            match = _ERROR.fullmatch(value)
            if match:
                fraction = _number(match.group(2))
                if fraction is not None:
                    records.append({
                        "variable": match.group(1).upper() + "0",
                        "fraction": fraction,
                        "label": value,
                        "column_zero_based": column,
                        "row_ref": ref,
                    })
    return records


def _group_errors(records, start_column, table):
    result = {}
    for variable in ("L0", "T0"):
        candidates = [item for item in records if item["variable"] == variable]
        same_column = [item for item in candidates if item["column_zero_based"] == start_column]
        selected = []
        if same_column and len({item["fraction"] for item in same_column}) == 1:
            selected = same_column
        elif candidates and len({item["fraction"] for item in candidates}) == 1:
            selected = candidates
        result[variable] = {
            "fraction": selected[0]["fraction"] if selected else None,
            "records": selected,
            "ambiguous": bool(candidates) and not selected,
            "evidence": [_evidence(table, _cell_ref(item["row_ref"], item["column_zero_based"])) for item in selected],
        }
    return result


def _sheet_selected(table, rule):
    selectors = rule.get("sheet_selector")
    if selectors in (None, ""):
        return True
    if isinstance(selectors, str):
        selectors = [selectors]
    if not isinstance(selectors, Sequence):
        return False
    sheet = str(table.get("sheet") or "")
    return any(sheet.casefold() == str(selector).strip().casefold() for selector in selectors)


def _dimensionless_response_unit(rule):
    definition = rule.get("definition")
    if not isinstance(definition, str):
        return None
    compact = re.sub(r"\s+", "", definition).replace("**", "^")
    declares_ratio = re.search(r"(?:^|[;,.])R=", compact) is not None
    if declares_ratio and all(token in compact for token in ("Kd", "T0", "L0", "sqrt", "/")):
        return "1"
    return None


def _groups_for_table(table, rule):
    rows = _row_list(table)
    header_rows = [(index, _triplets(_row_values(row))) for index, row in enumerate(rows)]
    header_rows = [(index, starts) for index, starts in header_rows if starts]
    source_rows = rows
    if not header_rows:
        starts = _triplets(table.get("columns") or [])
        if starts:
            header_rows = [(None, starts)]
    groups = []
    for header_position, (header_index, starts) in enumerate(header_rows):
        next_header = header_rows[header_position + 1][0] if header_position + 1 < len(header_rows) else None
        data_stop = next_header if next_header is not None else len(source_rows)
        data_start = header_index + 1 if header_index is not None else 0
        header_values = _row_values(rows[header_index]) if header_index is not None else list(table.get("columns") or [])
        header_ref = _row_ref(table, header_index) if header_index is not None else str(table.get("headers") or "declared-header")
        l0_index = _nearest_l0_row(rows, header_index)
        if header_index is not None:
            preceding = [( _row_values(rows[index]), _row_ref(table, index)) for index in range(header_index)]
        else:
            preceding = _metadata_header_rows(table)
        for start in starts:
            sheet = str(table.get("sheet") or table.get("name") or table.get("path") or "worksheet")
            l0_label = _cell(_row_values(rows[l0_index]), start) if l0_index is not None else None
            l0_match = _L0.fullmatch(l0_label) if isinstance(l0_label, str) else None
            l0_ref = _row_ref(table, l0_index) if l0_index is not None else header_ref
            l0_value = _number(l0_match.group(1)) if l0_match else None
            l0_unit = l0_match.group(2).strip() if l0_match else None
            raw_label = str(l0_label) if l0_label not in (None, "") else "unresolved L0 header"
            group_label = "%s | %s" % (sheet, raw_label)
            entity = {
                "table_id": table.get("id"),
                "sheet": table.get("sheet"),
                "group_label": group_label,
                "block_start_column_zero_based": start,
                "block_columns_zero_based": [start, start + 1, start + 2],
                "header_row_ref": header_ref,
                "L0_header": raw_label,
                "T0_header": _cell(header_values, start),
                "R_header": _cell(header_values, start + 1),
                "error_in_R_header": _cell(header_values, start + 2),
            }
            records = []
            excluded = []
            counts = {"missing_or_nonfinite_T0": 0, "missing_or_nonfinite_R": 0, "unsupported_negative_T0": 0}
            for row_index in range(data_start, data_stop):
                values = _row_values(source_rows[row_index])
                raw = [_cell(values, start + offset) for offset in range(3)]
                if all(value is None or isinstance(value, str) and not value.strip() for value in raw):
                    continue
                ref = _row_ref(table, row_index)
                t0 = _number(raw[0])
                observed = _number(raw[1])
                reason = None
                if t0 is None:
                    reason = "missing_or_nonfinite_T0"
                elif observed is None:
                    reason = "missing_or_nonfinite_R"
                elif t0 < 0:
                    reason = "unsupported_negative_T0"
                item = {
                    "row_index_zero_based": row_index,
                    "row_ref": ref,
                    "T0_ref": _cell_ref(ref, start),
                    "R_ref": _cell_ref(ref, start + 1),
                    "T0": t0,
                    "observed_R": observed,
                    "source_error_in_R_retained_not_used": _safe(raw[2]),
                }
                if reason:
                    counts[reason] += 1
                    item["exclusion_reason"] = reason
                    excluded.append(item)
                else:
                    records.append(item)
            l0_errors = _group_errors(_error_records(preceding), start, table)
            groups.append({
                "table": table,
                "entity": entity,
                "group_label": group_label,
                "L0": l0_value,
                "L0_unit": l0_unit,
                "L0_evidence": _evidence(table, _cell_ref(l0_ref, start)),
                "T0_unit": _header_unit(_cell(header_values, start)),
                "R_unit": _dimensionless_response_unit(rule),
                "source_errors": l0_errors,
                "records": records,
                "excluded": excluded,
                "exclusion_counts": counts,
                "total_rows": len(records) + len(excluded),
            })
    return groups


def _declared_source(rules):
    return rules.get("source") if isinstance(rules, Mapping) and isinstance(rules.get("source"), Mapping) else {}


def _expand_rules(rules):
    if isinstance(rules, Mapping):
        candidates = rules.get("operations") if isinstance(rules.get("operations"), list) else [rules]
    elif isinstance(rules, list):
        candidates = rules
    else:
        return []
    expanded = []

    def visit(items):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            nested = item.get("operations")
            if isinstance(nested, list):
                visit(nested)
            elif item.get("operation") == _OPERATION and item.get("model_id") == _MODEL_ID:
                expanded.append(item)

    visit(candidates)
    return expanded


def _page_reference(document, number):
    return str(document.get("path") or "<unknown-path>") + "#page=" + str(number)


def _source_evidence(data, rule, rules, sheet):
    documents = [item for item in data.get("documents") or [] if isinstance(item, Mapping)]
    model_refs = []
    basis = str(rule.get("basis") or "")
    match = re.search(r"([^\s,;]+\.pdf)#page=(\d+)", basis, re.I)
    if match:
        filename = match.group(1).rsplit("/", 1)[-1].casefold()
        number = int(match.group(2))
        for document in documents:
            if str(document.get("path") or "").rsplit("/", 1)[-1].casefold() == filename:
                pages = {int(page.get("page")) for page in document.get("pages") or [] if _number(page.get("page")) is not None}
                if number in pages:
                    model_refs.append(_page_reference(document, number))
    if not model_refs:
        for document in documents:
            for page in document.get("pages") or []:
                text = str(page.get("text") or "")
                if "(11)" in text and "unbound ligand" in text and "one-to-one" in text:
                    model_refs.append(_page_reference(document, page.get("page")))
    method_refs = []
    source = _declared_source(rules)
    declared_pages = source.get("method_pages") if isinstance(source.get("method_pages"), Mapping) else {}
    selected_pages = declared_pages.get(sheet)
    if selected_pages is not None:
        if not isinstance(selected_pages, list):
            selected_pages = [selected_pages]
        model_paths = {ref.split("#page=", 1)[0] for ref in model_refs}
        targets = [document for document in documents if not model_paths or str(document.get("path")) in model_paths]
        for document in targets:
            available = {int(page.get("page")) for page in document.get("pages") or [] if _number(page.get("page")) is not None}
            for number in selected_pages:
                if _number(number) is not None and int(number) in available:
                    method_refs.append(_page_reference(document, int(number)))
    source_fractions = set()
    for table in data.get("tables") or []:
        if not isinstance(table, Mapping) or str(table.get("sheet") or "").casefold() != str(sheet).casefold():
            continue
        for row in _row_list(table):
            for value in _row_values(row):
                match = _ERROR.fullmatch(value) if isinstance(value, str) else None
                if match:
                    fraction = _number(match.group(2))
                    if fraction is not None:
                        source_fractions.add(fraction)
    if not method_refs:
        candidates = []
        for document in documents:
            pages = [page for page in document.get("pages") or [] if isinstance(page, Mapping)]
            for index, page in enumerate(pages):
                text = str(page.get("text") or "")
                if re.search(re.escape(str(sheet)), text, re.I) and re.search(r"experiment|binding isotherm", text, re.I):
                    candidates.append((document, pages, index, page, text))
        primary = []
        if source_fractions:
            for candidate in candidates:
                tokens = [_number(match.group(0)) for match in _NUMERIC.finditer(candidate[4])]
                if all(fraction in tokens for fraction in source_fractions):
                    primary.append(candidate)
        if len(primary) == 1:
            document, pages, index, page, text = primary[0]
            method_refs.append(_page_reference(document, page.get("page")))
            if index > 0:
                previous = pages[index - 1]
                previous_text = str(previous.get("text") or "")
                if re.search(re.escape(str(sheet)), previous_text, re.I) and re.search(r"experiment|binding isotherm", previous_text, re.I):
                    method_refs.append(_page_reference(document, previous.get("page")))
        elif not source_fractions:
            for document, pages, index, page, text in candidates:
                if re.search(r"nominal\s+L0|total\s+ligand\s+concentration|real\s+experiments", text, re.I):
                    method_refs.append(_page_reference(document, page.get("page")))
    code_refs = []
    for item in data.get("other_files") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("text"), str):
            continue
        lines = item["text"].splitlines()
        starts = [index for index, line in enumerate(lines) if re.match(r"\s*def\s+model\s*\(", line)]
        for start in starts:
            excerpt = lines[start:start + 8]
            joined = "\n".join(excerpt)
            if all(token in joined for token in ("term1", "K_d", "T_0", "L_0", "np.sqrt", "return")):
                return_offset = next(index for index, line in enumerate(excerpt) if re.match(r"\s*return\s+", line))
                excerpt = excerpt[:return_offset + 1]
                code_refs.append(str(item.get("path") or item.get("name") or "source-code") + ":L%d-L%d (cited only; not executed)" % (start + 1, start + len(excerpt)))
    return list(dict.fromkeys(model_refs + method_refs)), list(dict.fromkeys(code_refs))


def _binding_domain(records, l0):
    violations = []
    for record in records:
        ratio = max(0.0, 1.0 - record["T0"] / l0)
        if record["observed_R"] >= ratio:
            continue
        violations.append({
            "row_ref": record["row_ref"], "R_ref": record["R_ref"],
            "T0": record["T0"], "observed_R": record["observed_R"],
            "lower_bound_R": ratio, "deficit_R_minus_lower_bound": ratio - record["observed_R"],
        })
    violations.sort(key=lambda item: (item["deficit_R_minus_lower_bound"], item["row_ref"]), reverse=True)
    return {
        "scope": "Algebraic feasibility only under the declared Eq11 and the exact reported positive L0; not proof of calibration or a measured physical fault.",
        "lower_bound": "For T0<L0, R=L/L0 >= 1-T0/L0 as Kd approaches zero. The fraction is also bounded by [0,1].",
        "R_unit": "1",
        "unit_note": "R and bound deficits are dimensionless; L0, T0 and fitted Kd retain the compatible source concentration units.",
        "criterion": "nonnegative parameter and nonnegative-concentration model constraints, not an exploratory outlier threshold, p-value or source-protocol QC rule",
        "count_R_below_required_lower_bound": len(violations),
        "count_R_above_one": sum(record["observed_R"] > 1.0 for record in records),
        "count_R_below_zero": sum(record["observed_R"] < 0.0 for record in records),
        "largest_deficit": violations[0] if violations else None,
        "violations": violations,
        "retained_all_rows": True,
        "cause_status": "candidate_for_review; does not prove a bad sample, instrument failure or any particular root cause",
    }


def _fit(group):
    try:
        import numpy as np
        from scipy.optimize import least_squares
    except (ImportError, ModuleNotFoundError) as exc:
        raise _DependencyUnavailable(str(exc)) from exc

    versions = {"python": platform.python_version(), "numpy": str(np.__version__), "scipy": str(__import__("scipy").__version__)}
    l0 = group["L0"]
    records = group["records"]
    if l0 is None or l0 <= 0:
        raise _FitFailure("A finite positive L0 parsed from the block's own header is required.")
    if group["T0_unit"] is None or group["L0_unit"] is None or _unit_key(group["T0_unit"]) != _unit_key(group["L0_unit"]):
        raise _FitFailure("L0 and T0 need explicitly compatible native units; no unit conversion or unit borrowing is performed.")
    if group["total_rows"] > _MAX_ROWS_PER_GROUP:
        raise _FitFailure("The group exceeds the 1000-row method bound; it was not silently truncated or subsampled.")
    if len(records) < 3 or len({item["T0"] for item in records}) < 2:
        raise _FitFailure("At least three finite T0/R pairs and varying T0 are required; no fit was performed.")

    t0 = np.asarray([item["T0"] for item in records], dtype=float)
    observed = np.asarray([item["observed_R"] for item in records], dtype=float)

    def residual(kd_values):
        kd = float(kd_values[0])
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            term1 = (kd + t0 - l0) / (2.0 * l0)
            fitted = -term1 + np.sqrt(term1 ** 2 + kd / l0)
        if not np.all(np.isfinite(fitted)):
            raise FloatingPointError("Non-finite Eq11 fitted response")
        return observed - fitted

    scale = max(l0, float(np.max(t0)))
    lower = max(float(np.finfo(float).tiny), scale * 1e-12)
    upper = scale * 1e12
    if not (math.isfinite(lower) and math.isfinite(upper) and 0.0 < lower < upper):
        raise _FitFailure("Finite data-driven positive Kd bounds could not be constructed.")
    first = max(l0 * 1e-6, lower * 10.0)
    last = min(scale * 1e3, upper / 10.0)
    if not (math.isfinite(first) and math.isfinite(last) and lower < first < last < upper):
        first = math.exp((2.0 * math.log(lower) + math.log(upper)) / 3.0)
        last = math.exp((math.log(lower) + 2.0 * math.log(upper)) / 3.0)
    initials = [float(value) for value in np.geomspace(first, last, num=9)]
    attempts = []
    converged = []
    for initial in initials:
        try:
            result = least_squares(
                residual,
                [initial],
                bounds=([lower], [upper]),
                method="trf",
                x_scale=scale,
                ftol=1e-12,
                xtol=1e-12,
                gtol=1e-12,
                max_nfev=_MAX_NFEV,
            )
            values = np.asarray(result.fun, dtype=float)
            sse = math.fsum(float(value) ** 2 for value in values)
            attempt = {
                "initial_Kd": initial,
                "converged": bool(result.success),
                "status": int(result.status),
                "message": str(result.message),
                "nfev": int(result.nfev),
                "SSE": sse,
            }
            attempts.append(attempt)
            if result.success and np.all(np.isfinite(result.x)) and np.all(np.isfinite(result.jac)) and math.isfinite(sse):
                converged.append((sse, initial, result))
        except (ValueError, FloatingPointError, OverflowError) as exc:
            attempts.append({"initial_Kd": initial, "converged": False, "error": type(exc).__name__ + ": " + str(exc)})
    if not converged:
        failure = _FitFailure("None of the deterministic positive-Kd initializations converged; no Kd estimate is reported.")
        failure.attempts = attempts
        failure.versions = versions
        raise failure

    sse, initial, best = min(converged, key=lambda item: (item[0], item[1]))
    kd = float(best.x[0])
    fitted_array = observed - np.asarray(best.fun, dtype=float)
    fitted = [float(value) for value in fitted_array]
    residuals = [float(value) for value in best.fun]
    details = []
    for index, record in enumerate(records):
        details.append({
            "row_ref": record["row_ref"],
            "T0_ref": record["T0_ref"],
            "R_ref": record["R_ref"],
            "T0": record["T0"],
            "observed_R": record["observed_R"],
            "fitted_R": fitted[index],
            "residual_observed_minus_fitted": residuals[index],
        })
    ordered = sorted(details, key=lambda item: (item["T0"], item["row_ref"]))
    sign_runs = []
    for item in ordered:
        value = item["residual_observed_minus_fitted"]
        sign = "positive" if value > 0 else "negative" if value < 0 else "zero"
        if sign_runs and sign_runs[-1]["sign"] == sign:
            run = sign_runs[-1]
            run["n"] += 1
            run["last_row_ref"] = item["row_ref"]
            run["T0_end"] = item["T0"]
        else:
            sign_runs.append({
                "sign": sign,
                "n": 1,
                "first_row_ref": item["row_ref"],
                "last_row_ref": item["row_ref"],
                "T0_start": item["T0"],
                "T0_end": item["T0"],
            })
    mean = math.fsum(record["observed_R"] for record in records) / len(records)
    sst = math.fsum((record["observed_R"] - mean) ** 2 for record in records)
    descriptive = 1.0 - sse / sst if math.isfinite(sst) and sst > 0.0 else None
    active_boundary = bool(getattr(best, "active_mask", [0])[0] != 0)
    active_boundary = active_boundary or math.isclose(kd, lower, rel_tol=1e-7, abs_tol=lower * 1e-7)
    active_boundary = active_boundary or math.isclose(kd, upper, rel_tol=1e-7, abs_tol=upper * 1e-7)
    jac = np.asarray(best.jac, dtype=float).reshape(-1)
    jtj = math.fsum(float(value) ** 2 for value in jac)
    ci_status = "estimated"
    ci_reason = None
    standard_error = None
    interval_low = None
    interval_high = None
    if active_boundary:
        ci_status = "unavailable_parameter_at_boundary"
        ci_reason = "A local covariance interval is not reported at an active parameter bound."
    elif not math.isfinite(jtj) or jtj <= float(np.finfo(float).tiny):
        ci_status = "unavailable_ill_conditioned_jacobian"
        ci_reason = "J^T J is non-finite or numerically singular; its inverse was not replaced by zero."
    else:
        variance = sse / (len(records) - 1) / jtj
        if not math.isfinite(variance) or variance <= 0.0:
            ci_status = "unavailable_nonpositive_or_nonfinite_local_variance"
            ci_reason = "The local variance is non-finite or zero; standard_error and interval endpoints remain null."
        else:
            standard_error = math.sqrt(variance)
            interval_low = kd - standard_error
            interval_high = kd + standard_error
    minimum = min(details, key=lambda item: (item["residual_observed_minus_fitted"], item["row_ref"]))
    maximum = max(details, key=lambda item: (item["residual_observed_minus_fitted"], item["row_ref"]))
    result = {
        "Kd_est": kd,
        "standard_error": standard_error,
        "interval_low": interval_low,
        "interval_high": interval_high,
        "interval_coverage": 0.683,
        "local_interval_estimable": standard_error is not None,
        "ci_status": ci_status,
        "ci_unavailable_reason": ci_reason,
        "converged": True,
        "optimizer_status": int(best.status),
        "optimizer_message": str(best.message),
        "selected_initial_Kd": initial,
        "nfev": int(best.nfev),
        "n": len(records),
        "distinct_T0": len({item["T0"] for item in records}),
        "total_rows": group["total_rows"],
        "excluded_rows": len(group["excluded"]),
        "exclusion_counts": group["exclusion_counts"],
        "excluded_row_details": group["excluded"],
        "SSE": sse,
        "SST": sst,
        "descriptive_1_minus_SSE_over_SST": descriptive,
        "free_parameter_count": 1,
        "local_covariance_residual_dof": len(records) - 1,
        "dof_basis": "len(records) - free_parameter_count; fixed L0 and a fit-invariant T0=0 anchor do not reduce the free-parameter count, and residual independence is still an assumption to check rather than a claim proven by this arithmetic",
        "Kd_lower_bound": lower,
        "Kd_upper_bound": upper,
        "parameter_at_bound": active_boundary,
        "jacobian_transpose_jacobian": jtj,
        "max_nfev_per_initialization": _MAX_NFEV,
        "initialization_count": len(initials),
        "optimizer_attempts": attempts,
        "R_outside_closed_unit_interval_retained": sum(not 0.0 <= record["observed_R"] <= 1.0 for record in records),
        "error_in_R_used_as_weight": False,
        "outlier_filter_applied": False,
        "software": versions,
    }
    residual_result = {
        "n": len(records),
        "residual_definition": "observed_R_minus_fitted_R",
        "SSE": sse,
        "SST": sst,
        "descriptive_1_minus_SSE_over_SST": descriptive,
        "rows_ordered_as_source": details,
        "residual_sign_runs_by_increasing_T0": sign_runs,
        "residual_extremes_for_review": {"most_negative": minimum, "most_positive": maximum},
        "outlier_filter_applied": False,
        "error_in_R_used_as_residual_or_weight": False,
        "response_domain": _binding_domain(records, l0),
    }
    return result, residual_result


def analyze_models(data: dict, rules: list | dict) -> dict:
    """Fit only the declared Eq11 operation in actual independent worksheet blocks."""
    data = data if isinstance(data, dict) else {}
    matched_rules = _expand_rules(rules)
    facts, issues, groups = [], [], []
    seen = set()
    tables = [table for table in data.get("tables") or [] if isinstance(table, Mapping)]
    selected_tables = set()
    for rule in matched_rules:
        for position, table in enumerate(tables):
            if not _sheet_selected(table, rule):
                continue
            selected_tables.add(position)
            for group in _groups_for_table(table, rule):
                key = (str(table.get("id") or position), group["entity"]["header_row_ref"], group["entity"]["block_start_column_zero_based"])
                if key not in seen:
                    seen.add(key)
                    groups.append(group)
    if matched_rules and not groups:
        issues.append(_issue(
            "cannot_determine",
            "scientific_models.discovery",
            "The declared Eq11 operation matched no selected worksheet block with actual adjacent T0, R, and error in R headers; no fit was performed.",
        ))

    coverage_groups = []
    versions = {}
    dependency_failure = None
    base_method = (
        "Eq11: term1=(Kd+T0-L0)/(2*L0); R=-term1+sqrt(term1^2+Kd/L0). "
        "One fixed source L0 per independent column block; one positive Kd estimated by deterministic bounded multi-init "
        "scipy.optimize.least_squares (ordinary unweighted residual observed_R-fitted_R, max_nfev=500 per init). "
        "All finite nonnegative T0 and finite R pairs are retained without an R-range or outlier filter."
    )
    for group in groups:
        table = group["table"]
        entity = group["entity"]
        source_refs, code_refs = _source_evidence(data, next(rule for rule in matched_rules if _sheet_selected(table, rule)), rules, str(table.get("sheet") or ""))
        row_evidence = [_evidence(table, record["row_ref"]) for record in group["records"]]
        error_evidence = [ref for variable in ("L0", "T0") for ref in group["source_errors"][variable]["evidence"]]
        group_entry = {
            "group_label": group["group_label"],
            "sheet": table.get("sheet"),
            "table_id": table.get("id"),
            "n": len(group["records"]),
            "total_rows": group["total_rows"],
            "excluded_rows": len(group["excluded"]),
            "L0": group["L0"],
            "source_units": {"L0": group["L0_unit"], "T0_and_Kd": group["T0_unit"], "R": group["R_unit"]},
            "fit_status": "pending",
            "ci_status": "pending",
        }
        if group["L0"] is not None:
            facts.append(_fact(
                "observed",
                "binding_L0",
                entity,
                group["L0"],
                group["L0_unit"],
                "Parsed the complete L0 = value unit label at the exact block-start column in the nearest preceding L0-label row; no rounded PDF value substitution or row-order ID matching.",
                [group["L0_evidence"]],
                "This is the workbook's experimental L0 identity, not an independent calibration or a fitted quantity.",
            ))
        else:
            issues.append(_issue("cannot_determine", "scientific_models.L0", "The block lacks an exact parseable L0 = value unit label at its own header column; Kd cannot be fit.", [group["L0_evidence"]], group_label=group["group_label"]))
        error_values = {variable: group["source_errors"][variable]["fraction"] for variable in ("L0", "T0")}
        facts.append(_fact(
            "observed",
            "binding_source_random_error_fractions",
            entity,
            error_values,
            None,
            "Retained the source's explicit Random error in L0/T0 labels as separate fractional metadata. These values do not enter the ordinary fit.",
            error_evidence + source_refs,
            "These source random-error labels are not standard deviations for R weights, not independently re-estimated systematic-error confidence intervals, and not an ACI calculation. Missing or ambiguous labels remain null.",
        ))
        for variable in ("L0", "T0"):
            if group["source_errors"][variable]["ambiguous"]:
                issues.append(_issue("cannot_determine", "scientific_models.error_metadata", "Multiple source error fractions could not be uniquely linked to this block; no value was assigned or used as a weight.", error_evidence, group_label=group["group_label"], variable=variable))

        try:
            fit, residual = _fit(group)
            versions.update(fit["software"])
            facts.append(_fact(
                "derived",
                "binding_unbound_fraction_one_to_one_eq11_Kd_fit",
                entity,
                fit,
                group["T0_unit"],
                base_method + " Local approximately central-normal one-parameter covariance: SE=sqrt(SSE/(n-1)/(J^T J)); 68.3% interval=Kd-SE to Kd+SE when estimable. The unit on this fact applies to Kd, its SE, bounds and interval endpoints; no concentration-unit conversion is performed.",
                [group["L0_evidence"]] + source_refs + code_refs + row_evidence,
                "Independent ordinary-NLS recomputation, not a paper-reported interval. The local interval is not an exact frequentist guarantee, bootstrap, or ACI; it can underestimate systematic concentration errors and assumes the declared model. Boundary or non-estimable covariance leaves SE/endpoints null.",
            ))
            facts.append(_fact(
                "derived",
                "binding_unbound_fraction_one_to_one_eq11_residuals",
                entity,
                residual,
                group["R_unit"],
                "Residual=observed R-fitted R at every fitted source row. SSE=sum(residual^2); SST=sum((observed_R-mean_R)^2); descriptive_1_minus_SSE_over_SST=1-SSE/SST only when SST>0. Residual sign runs are ordered by numeric T0; source-order rows and extremes retain exact row/cell references for review.",
                source_refs + code_refs + row_evidence,
                "The descriptive SSE/SST fraction is not a paper-reported R-squared, validation score, or ACI. Sign runs and extremes expose possible systematic residual structure but are not an anomaly detector, smoothing result, independent calibration, or causal diagnosis. R values outside 0 to 1 and all signs remain included.",
            ))
            group_entry["fit_status"] = "completed"
            group_entry["ci_status"] = fit["ci_status"]
            group_entry["converged"] = fit["converged"]
            group_entry["Kd_est"] = fit["Kd_est"]
            if not fit["local_interval_estimable"]:
                issues.append(_issue("cannot_determine", "scientific_models.local_covariance", fit["ci_unavailable_reason"], [group["L0_evidence"]] + row_evidence, group_label=group["group_label"], ci_status=fit["ci_status"], Kd_est=fit["Kd_est"]))
        except _DependencyUnavailable as exc:
            dependency_failure = str(exc)
            group_entry["fit_status"] = "unavailable"
            group_entry["ci_status"] = "unavailable_no_fit"
        except _FitFailure as exc:
            group_entry["fit_status"] = "failed"
            group_entry["ci_status"] = "unavailable_no_fit"
            details = {"group_label": group["group_label"]}
            if hasattr(exc, "attempts"):
                details["optimizer_attempts"] = exc.attempts
            if hasattr(exc, "versions"):
                versions.update(exc.versions)
            issues.append(_issue("cannot_determine", "scientific_models.fit", str(exc), [group["L0_evidence"]] + row_evidence, **details))
        except Exception as exc:
            group_entry["fit_status"] = "failed"
            group_entry["ci_status"] = "unavailable_no_fit"
            issues.append(_issue("runtime_failure", "scientific_models.fit", type(exc).__name__ + ": " + str(exc), [group["L0_evidence"]] + row_evidence, group_label=group["group_label"]))
        coverage_groups.append(group_entry)

    if dependency_failure is not None:
        issues.append(_issue(
            "runtime_dependency_unavailable",
            "scientific_models.fit",
            "NumPy/SciPy could not be imported for nonlinear regression; affected groups are unavailable and no Kd, SE, or interval is fabricated. Stdlib header parsing and observed L0/error facts remain available: " + dependency_failure,
            details={"unavailable_groups": [item["group_label"] for item in coverage_groups if item["fit_status"] == "unavailable"]},
        ))

    response_unit = _dimensionless_response_unit(matched_rules[0]) if matched_rules else None
    method = {
        "name": _MODEL_ID,
        "algorithm": base_method + " Best converged initialization is selected only by lowest SSE of the same declared model. Local 68.3% interval uses Kd plus/minus one local standard error; per-row residuals and sign runs are reported without masking.",
        "versions": versions or {"python": platform.python_version(), "numpy": None, "scipy": None},
        "source_evidence": "Actual worksheet row/cell references, rule-declared or content-located original PDF pages, and text-only line anchors for matching source model code; source code is never executed.",
        "response_unit": response_unit,
        "response_unit_basis": "Response unit is 1 only when the supplied definition explicitly declares R as an equation in Kd, T0, L0 and sqrt; R is not used to infer or convert Kd units.",
        "limitations": "Not a bootstrap ACI/PCI computation. The source error in R column is neither an SD weight nor a fitted residual. No robust z/MAD mask, model competition, independent R calibration, or systematic-error confidence interval is implemented; local covariance may underestimate systematic uncertainty.",
    }
    completed = sum(item["fit_status"] == "completed" for item in coverage_groups)
    failed = sum(item["fit_status"] == "failed" for item in coverage_groups)
    unavailable = sum(item["fit_status"] == "unavailable" for item in coverage_groups)
    coverage = {
        "operation": _OPERATION if matched_rules else None,
        "model_id": _MODEL_ID if matched_rules else None,
        "requested_matching_operations": len(matched_rules),
        "selected_tables": len(selected_tables),
        "group_labels": [item["group_label"] for item in coverage_groups],
        "groups": coverage_groups,
        "fit_completed": completed,
        "fit_failed": failed,
        "fit_unavailable": unavailable,
        "source_units": [item["source_units"] | {"group_label": item["group_label"]} for item in coverage_groups],
        "total_rows": sum(item["total_rows"] for item in coverage_groups),
        "fitted_rows": sum(item["n"] for item in coverage_groups if item["fit_status"] == "completed"),
        "excluded_rows": sum(item["excluded_rows"] for item in coverage_groups),
        "rows_per_group_limit": _MAX_ROWS_PER_GROUP,
        "ACI_computed": False,
        "bootstrap_computed": False,
        "limitations": [
            "ACI is not computed: an independently specified ACI workflow and complete systematic-error confidence inputs are not implemented here; source random-error labels are not promoted to those inputs.",
            "The local 68.3% covariance interval is not ACI, bootstrap, or an exact frequency guarantee and can underestimate systematic errors.",
            "Only operation=fit_declared_model with model_id=binding_unbound_fraction_one_to_one_eq11 is analyzed; other operations receive no fit.",
        ],
        "issues": len(issues),
    }
    result = {"facts": facts, "issues": issues, "methods": [method] if matched_rules else [], "coverage": coverage}
    return _safe(result)
