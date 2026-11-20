"""Bounded, evidence-linked analytical-method recalculations using only the stdlib."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict


_MAX_ROWS = 10000
_MAX_METRICS = 32
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
_MISSING = {"", "na", "n/a", "nan", "none", "null", "missing", "."}
_CALIBRATION = re.compile(r"calibrat|standard[ _-]*curve", re.I)
_PRECISION = re.compile(r"repeatability|precision|reproducibility", re.I)
_RECOVERY = re.compile(r"recovery|spike[ _-]*recovery", re.I)
_ID = re.compile(r"^(?:sample|specimen|subject)(?:_id|_identifier)?$|^sid$", re.I)
_LOD = re.compile(r"\bLOD\b|limit\s+of\s+detection", re.I)
_LOQ = re.compile(r"\bLOQ\b|limit\s+of\s+quant(?:ification|itation)", re.I)


def _name(value):
    return re.sub(r"[^a-z0-9%µμ./]+", "_", str(value).strip().lower()).strip("_")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in _MISSING:
            return None
        if not re.fullmatch(_NUMBER, text):
            return None
        value = text
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    return value if value is None or isinstance(value, (str, int, bool)) else str(value)


def _unit(header):
    text = str(header)
    bracket = re.search(r"[\[(]([^\])]+)[\])]", text)
    if bracket:
        return bracket.group(1).strip() or None
    atoms = r"(?:mg|kg|ug|µg|μg|ng|pg|g|mol|mmol|umol|µmol|μmol|nmol|l|ml|ul|µl|μl|m|cm|mm|s|min|h)"
    match = re.search(r"(?i)(?<![a-z0-9])(%|percent|a\.?u\.?|absorbance[ _-]*units?|response[ _-]*units?|" + atoms + r"(?:[ _-]*(?:/|per)[ _-]*" + atoms + r")?)(?![a-z0-9])", text)
    return match.group(1) if match else None


def _same_unit(left, right, allow_missing=True):
    if not allow_missing and (left is None or right is None):
        return False
    def norm(item):
        if item is None:
            return None
        return re.sub(r"[\s._-]+", "", item.lower().replace("µ", "u").replace("μ", "u").replace("per", "/").replace("absorbanceunits", "au").replace("responseunits", "au")).replace("a.u.", "au")
    return norm(left) == norm(right)


def _evidence(table, index=None):
    path = str(table.get("path", "<unknown-path>"))
    sheet = table.get("sheet")
    base = path + ("#" + str(sheet) if sheet not in (None, "") else "")
    refs = table.get("row_refs") or []
    if index is not None and index < len(refs):
        return base + " | " + str(refs[index])
    return base + (" | header=" + str(table.get("headers")) if index is None else " | row=" + str(index + 1))


def _fact(kind, metric, entity, value, unit, method, evidence, uncertainty):
    return {"kind": kind, "metric": metric, "entity": _safe(entity), "value": _safe(value),
            "unit": unit, "method": method, "evidence": evidence, "uncertainty": uncertainty}


def _id_index(table, names):
    found = [i for i, name in enumerate(names) if _ID.fullmatch(_name(name))]
    return found[0] if len(found) == 1 else None


def _numeric_indices(table, names, indices=None):
    rows = table.get("rows") or []
    indices = range(len(names)) if indices is None else indices
    return [i for i in indices if any(_number(row[i]) is not None for row in rows if i < len(row))]


def _id_map(table, id_index, issues):
    result, raw_values = {}, []
    for index, row in enumerate(table.get("rows") or []):
        value = row[id_index] if id_index < len(row) else None
        if value is None or isinstance(value, (dict, list)) or str(value).strip() == "":
            issues.append({"kind": "cannot_determine", "stage": "domain_pairing", "detail": "Pairing rejected: every selected row needs a non-empty scalar sample ID.", "evidence": [_evidence(table, index)]})
            return None
        key = type(value).__name__ + ":" + str(value)
        raw_values.append(key)
        result.setdefault(key, []).append(index)
    duplicates = [key for key, count in Counter(raw_values).items() if count > 1]
    if duplicates:
        issues.append({"kind": "quality_violation", "stage": "domain_pairing", "detail": "Pairing rejected because sample IDs are not unique: " + repr(duplicates[:10]), "evidence": [_evidence(table)]})
        return None
    return {key: positions[0] for key, positions in result.items()}


def _cal_supported(table, documents, xi, yi):
    sheet = str(table.get("sheet") or table.get("name") or table.get("path") or "")
    support = bool(_CALIBRATION.search(sheet))
    evidence = []
    names = table.get("columns") or []
    x_name, y_name = _name(names[xi]), _name(names[yi])
    for document in documents:
        for page in document.get("pages") or []:
            text = str(page.get("text") or "")
            normalized = _name(text)
            if _CALIBRATION.search(text) and x_name in normalized and y_name in normalized:
                support = True
                evidence.append(str(document.get("path")) + "#page=" + str(page.get("page")))
    return support, evidence


def _calibration(data, facts, issues):
    fits = []
    for table in (data.get("tables") or [])[:1000]:
        rows = table.get("rows") or []
        names = [str(item) for item in table.get("columns") or []]
        if len(rows) > _MAX_ROWS:
            continue
        xs = [i for i, name in enumerate(names) if re.search(r"nominal|concentration|\bconc\b|standard[ _-]*(?:level|amount)", name, re.I) and not re.search(r"found|measured|back[ -]*calculated|calculated|unknown", name, re.I)]
        ys = [i for i, name in enumerate(names) if re.search(r"absorbance|peak[ _-]*(?:area|height)|response|signal|fluorescence|intensity", name, re.I)]
        xs, ys = _numeric_indices(table, names, xs), _numeric_indices(table, names, ys)
        if len(xs) != 1 or len(ys) != 1 or xs[0] == ys[0]:
            if _CALIBRATION.search(str(table.get("sheet") or table.get("name") or table.get("path") or "")):
                issues.append({"kind": "cannot_determine", "stage": "domain_calibration", "detail": "No unique compatible concentration/response numeric column pair; calibration-like labels alone were not sufficient.", "evidence": [_evidence(table)]})
            continue
        xi, yi = xs[0], ys[0]
        supported, document_evidence = _cal_supported(table, data.get("documents") or [], xi, yi)
        if not supported:
            continue
        points = [(index, _number(row[xi]), _number(row[yi])) for index, row in enumerate(rows) if xi < len(row) and yi < len(row) and _number(row[xi]) is not None and _number(row[yi]) is not None]
        if len(points) < 3 or len({point[1] for point in points}) < 2 or len({point[2] for point in points}) < 2:
            issues.append({"kind": "cannot_determine", "stage": "domain_calibration", "detail": "OLS requires at least 3 paired rows, at least 2 distinct x values, and non-constant y.", "evidence": [_evidence(table, p[0]) for p in points]})
            continue
        n = len(points)
        xm = math.fsum(p[1] for p in points) / n
        ym = math.fsum(p[2] for p in points) / n
        sxx = math.fsum((p[1] - xm) ** 2 for p in points)
        syy = math.fsum((p[2] - ym) ** 2 for p in points)
        slope = math.fsum((p[1] - xm) * (p[2] - ym) for p in points) / sxx
        intercept = ym - slope * xm
        details = []
        for index, x, y in points:
            residual = y - (intercept + slope * x)
            details.append({"row_ref": str((table.get("row_refs") or [])[index]), "x": x, "observed_y": y, "fitted_y": intercept + slope * x, "residual_y": residual, "back_calculated_x": (y - intercept) / slope if slope != 0 else None})
        r2 = 1.0 - math.fsum(item["residual_y"] ** 2 for item in details) / syy
        evidence = [_evidence(table, index) for index, _, _ in points] + document_evidence
        entity = {"table_id": table.get("id"), "sheet": table.get("sheet"), "x_column": names[xi], "y_column": names[yi]}
        method = "Unweighted OLS with intercept over all %d listed paired rows: b=sum((x-xbar)(y-ybar))/sum((x-xbar)^2); a=ybar-b*xbar; R^2=1-sum(residual^2)/sum((y-ybar)^2); x_back=(y-a)/b. No row means, weighting, logs, or index substitution." % n
        facts.append(_fact("derived", "calibration_ols", entity, {"n": n, "slope": slope, "intercept": intercept, "r_squared": r2}, None, method, evidence, "R-squared describes this unweighted fit, not trueness, accuracy, or method validity. Units are taken only from native headers."))
        facts.append(_fact("derived", "calibration_row_recalculation", entity, {"residual_unit": _unit(names[yi]), "back_calculated_x_unit": _unit(names[xi]), "rows": details}, None, method + " residual=y-(a+b*x); back-calculated values use the x unit.", [_evidence(table, p[0]) for p in points], "Back-calculated x is omitted wherever slope is zero. Repeated levels were not averaged; extrapolation and independent validation are not established."))
        fits.append({"n": n, "slope": slope, "x_unit": _unit(names[xi]), "y_unit": _unit(names[yi]), "table_id": table.get("id"), "evidence": evidence})
    return fits


def _sample_sd(values):
    mean = math.fsum(values) / len(values)
    return math.sqrt(max(0.0, math.fsum((item - mean) ** 2 for item in values) / (len(values) - 1)))


def _precision(data, facts, issues):
    count = 0
    for table in data.get("tables") or []:
        rows, names = table.get("rows") or [], [str(x) for x in table.get("columns") or []]
        if not rows or len(rows) > _MAX_ROWS or not _PRECISION.search(str(table.get("sheet") or table.get("name") or "")):
            continue
        group_cols = [i for i, name in enumerate(names) if re.search(r"nominal|concentration|(?:^|[ _])level(?:$|[ _])", name, re.I)]
        if len(group_cols) > 1:
            issues.append({"kind": "cannot_determine", "stage": "domain_precision", "detail": "Multiple possible level columns; across-level pooling was not performed.", "evidence": [_evidence(table)]})
            continue
        metrics = _numeric_indices(table, names, [i for i, name in enumerate(names) if i not in group_cols and i != _id_index(table, names) and not re.search(r"(?:^|_)(?:rsd|cv|mean|sd|standard_deviation)(?:_|$)|recovery", name, re.I)])[:_MAX_METRICS]
        for metric in metrics:
            groups = defaultdict(list)
            for index, row in enumerate(rows):
                value = _number(row[metric]) if metric < len(row) else None
                if value is not None:
                    key = str(row[group_cols[0]]) if group_cols and group_cols[0] < len(row) else None
                    groups[key].append((index, value))
            for level, selected in groups.items():
                if len(selected) < 2:
                    continue
                values = [item[1] for item in selected]
                mean = math.fsum(values) / len(values)
                sd = _sample_sd(values)
                evidence = [_evidence(table, item[0]) for item in selected]
                entity = {"table_id": table.get("id"), "sheet": table.get("sheet"), "metric_column": names[metric], "level": level}
                facts.append(_fact("derived", "operational_precision_mean_sd", entity, {"n": len(values), "mean": mean, "sample_sd_n_minus_1": sd}, _unit(names[metric]), "Arithmetic mean and sample SD sqrt(sum((x-mean)^2)/(n-1)) over exactly the listed numeric rows, separately per explicit level where present.", evidence, "Operational within-selected-records dispersion only; preparation versus injection/scan repeats and biological independence cannot be inferred."))
                if mean != 0:
                    facts.append(_fact("derived", "operational_precision_rsd", entity, {"rsd_percent": 100.0 * sd / mean}, "%", "RSD=100*sample_sd/arithmetic_mean; no absolute-value substitution for a negative mean.", evidence, "Not validated precision or a source-defined acceptance assessment."))
                else:
                    issues.append({"kind": "cannot_determine", "stage": "domain_precision", "detail": "RSD is undefined because the selected-record mean is zero.", "evidence": evidence})
                count += 1
    return count


def _recovery(data, facts, issues):
    count = 0
    for table in data.get("tables") or []:
        rows, names = table.get("rows") or [], [str(x) for x in table.get("columns") or []]
        if not rows or len(rows) > _MAX_ROWS:
            continue
        resulted = False
        nominal = [i for i, n in enumerate(names) if re.match(r"(?:nominal|expected)(?:_|$)", _name(n))]
        found = [i for i, n in enumerate(names) if re.match(r"(?:found|measured)(?:_|$)", _name(n))]
        pairs = [(n, f) for n in nominal for f in found if _same_unit(_unit(names[n]), _unit(names[f]), False)]
        if len(pairs) == 1:
            ni, fi = pairs[0]
            details = []
            for index, row in enumerate(rows):
                denominator, numerator = _number(row[ni]), _number(row[fi])
                if denominator is not None and numerator is not None:
                    if denominator == 0:
                        issues.append({"kind": "quality_violation", "stage": "domain_recovery", "detail": "Recovery ratio skipped: nominal denominator is zero.", "evidence": [_evidence(table, index)]})
                    else:
                        details.append({"row_ref": str((table.get("row_refs") or [])[index]), "nominal": denominator, "found": numerator, "recovery_percent": 100.0 * numerator / denominator})
            if details:
                facts.append(_fact("derived", "nominal_found_recovery", {"table_id": table.get("id"), "nominal_column": names[ni], "found_column": names[fi]}, details, "%", "Within each listed original row: 100*found/nominal; zero denominators excluded and flagged. Compatible native units required.", [_evidence(table, index) for index, row in enumerate(rows) if _number(row[ni]) not in (None, 0) and _number(row[fi]) is not None], "Ratio recovery only; this is not a blank-subtracted or independently validated recovery estimate."))
                count += len(details)
                resulted = True
        elif len(pairs) > 1:
            issues.append({"kind": "cannot_determine", "stage": "domain_recovery", "detail": "Ambiguous nominal/found matches; recovery ratios not chosen automatically.", "evidence": [_evidence(table)]})
            resulted = True
        added = [i for i, n in enumerate(names) if re.match(r"(?:added|spike_added|amount_added)(?:_|$)", _name(n))]
        spiked = [i for i, n in enumerate(names) if re.match(r"spiked_(?!added|nominal|amount)", _name(n))]
        unspiked = [i for i, n in enumerate(names) if re.match(r"unspiked_", _name(n))]
        triples = [(a, s, u) for a in added for s in spiked for u in unspiked if _same_unit(_unit(names[a]), _unit(names[s]), False) and _same_unit(_unit(names[a]), _unit(names[u]), False)]
        if len(triples) > 1:
            issues.append({"kind": "cannot_determine", "stage": "domain_recovery", "detail": "Ambiguous added/spiked/unspiked columns; subtraction recovery was not chosen automatically.", "evidence": [_evidence(table)]})
            resulted = True
        elif len(triples) == 1:
            resulted = True
            ai, si, ui = triples[0]
            id_index = _id_index(table, names)
            id_map = _id_map(table, id_index, issues) if id_index is not None else None
            if id_map is not None:
                details, used = [], []
                for index, row in enumerate(rows):
                    a, s, u = _number(row[ai]), _number(row[si]), _number(row[ui])
                    if a is not None and s is not None and u is not None:
                        if a == 0:
                            issues.append({"kind": "quality_violation", "stage": "domain_recovery", "detail": "Subtraction recovery skipped: added denominator is zero.", "evidence": [_evidence(table, index)]})
                        else:
                            details.append({"sample_id": row[id_index], "spiked": s, "unspiked": u, "added": a, "recovery_percent": 100.0 * (s - u) / a})
                            used.append(index)
                if details:
                    facts.append(_fact("derived", "spiked_unspiked_subtraction_recovery", {"table_id": table.get("id"), "added_column": names[ai], "spiked_column": names[si], "unspiked_column": names[ui]}, details, "%", "Within the same explicit sample-ID row and equal native units: 100*(spiked-unspiked)/added; zero added values excluded and flagged.", [_evidence(table, index) for index in used], "No cross-sheet or cross-sample subtraction, default acceptance interval, or independent-preparation claim."))
                    count += len(details)
        if _RECOVERY.search(str(table.get("sheet") or table.get("name") or "")) and not resulted:
            issues.append({"kind": "cannot_determine", "stage": "domain_recovery", "detail": "Recovery not recomputed: no unambiguous explicit same-row nominal/found or added/spiked/unspiked fields with matched sample IDs and equal native units. Source_reported values are not promoted to recovery measurements or blank-subtracted without source support.", "evidence": [_evidence(table)]})
    return count


def _role_column(name):
    normalized = _name(name)
    for role, words in (("candidate", ("candidate", "test", "proposed")), ("reference", ("reference", "control"))):
        for word in words:
            match = re.match("^(?:" + word + r")_(.+)$|^(.+)_" + word + "$", normalized)
            if match:
                return role, next(item for item in match.groups() if item is not None)
    return None


def _sheet_role(table):
    normalized = _name(table.get("sheet") or "")
    if re.search(r"(?:^|_)(?:candidate|test|proposed)(?:_|$)", normalized):
        return "candidate"
    if re.search(r"(?:^|_)(?:reference|control)(?:_|$)", normalized):
        return "reference"
    return None


def _pair_result(facts, issues, candidate, reference, ci, ri, label):
    if len(candidate.get("rows") or []) > _MAX_ROWS or len(reference.get("rows") or []) > _MAX_ROWS:
        issues.append({"kind": "cannot_determine", "stage": "domain_pairing", "detail": "Pairing skipped because a selected table exceeds the 10000-row method bound.", "evidence": [_evidence(candidate), _evidence(reference)]})
        return 0
    cn, rn = [str(x) for x in candidate.get("columns") or []], [str(x) for x in reference.get("columns") or []]
    cid, rid = _id_index(candidate, cn), _id_index(reference, rn)
    if cid is None or rid is None or not _same_unit(_unit(cn[ci]), _unit(rn[ri])):
        return 0
    cmap, rmap = _id_map(candidate, cid, issues), _id_map(reference, rid, issues)
    if cmap is None or rmap is None:
        return 0
    keys = sorted(set(cmap) & set(rmap))
    unmatched = sorted(set(cmap) ^ set(rmap))
    if unmatched:
        issues.append({"kind": "cannot_determine", "stage": "domain_pairing", "detail": "Unmatched sample IDs were not cross-paired: " + repr(unmatched[:20]), "evidence": [_evidence(candidate), _evidence(reference)]})
    details, entity_by_key = [], {}
    for key in keys:
        cr, rr = candidate["rows"][cmap[key]], reference["rows"][rmap[key]]
        cv, rv = _number(cr[ci]), _number(rr[ri])
        if cv is not None and rv is not None:
            details.append({"sample_id": cr[cid], "candidate": cv, "reference": rv, "difference": cv - rv, "percent_of_reference": 100.0 * (cv - rv) / rv if rv != 0 else None})
            entity_by_key[key] = (cr[cid], cmap[key], rmap[key])
    if not details:
        return 0
    differences = [item["difference"] for item in details]
    ordered = sorted(differences)
    middle = len(ordered) // 2
    median = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
    used_keys = [key for key in keys if key in entity_by_key]
    evidence = [_evidence(candidate, entity_by_key[key][1]) for key in used_keys] + [_evidence(reference, entity_by_key[key][2]) for key in used_keys]
    entity = {"comparison": label, "candidate_metric": cn[ci], "reference_metric": rn[ri], "candidate_table": candidate.get("id"), "reference_table": reference.get("id")}
    facts.append(_fact("derived", "paired_candidate_minus_reference", entity, {"n": len(details), "signed_mean": math.fsum(differences) / len(differences), "median": median, "range": [min(differences), max(differences)], "sample_sd": _sample_sd(differences) if len(differences) > 1 else None, "pairs": details}, _unit(cn[ci]), "Per identical stable sample ID: Candidate-Reference in original units; signed mean, median, min/max, and descriptive sample SD (n-1). No p-value or equivalence test.", evidence, "Descriptive pairing only; unmatched IDs were excluded, zero-reference percentages are undefined, and no acceptance/tolerance interval is implied. High correlation would not establish agreement."))
    percentages = [{"sample_id": item["sample_id"], "percent": item["percent_of_reference"]} for item in details if item["percent_of_reference"] is not None]
    if percentages:
        facts.append(_fact("derived", "paired_difference_percent_of_reference", entity, percentages, "%", "For each nonzero Reference: 100*(Candidate-Reference)/Reference on the same sample ID and row basis.", evidence, "Percentages have a zero-reference exclusion and carry no wider equivalency or acceptance interpretation."))
    return len(details)


def _pairing(data, facts, issues):
    tables = data.get("tables") or []
    count = 0
    for table in tables:
        names = [str(x) for x in table.get("columns") or []]
        roles = defaultdict(list)
        for index, name in enumerate(names):
            role = _role_column(name)
            if role:
                roles[(role[0], role[1])].append(index)
        signatures = {key[1] for key in roles if key[0] == "candidate"} & {key[1] for key in roles if key[0] == "reference"}
        for signature in sorted(signatures):
            if len(roles[("candidate", signature)]) == len(roles[("reference", signature)]) == 1:
                count += _pair_result(facts, issues, table, table, roles[("candidate", signature)][0], roles[("reference", signature)][0], "same_table")
    by_path = defaultdict(lambda: defaultdict(list))
    for table in tables:
        role = _sheet_role(table)
        if role:
            by_path[str(table.get("path"))][role].append(table)
    for path, groups in by_path.items():
        candidates, references = groups["candidate"], groups["reference"]
        if len(candidates) != 1 or len(references) != 1:
            if candidates or references:
                issues.append({"kind": "cannot_determine", "stage": "domain_pairing", "detail": "Complementary-sheet pairing requires exactly one candidate/test/proposed and one reference/control table per source path.", "evidence": [path]})
            continue
        candidate, reference = candidates[0], references[0]
        cn, rn = [str(x) for x in candidate.get("columns") or []], [str(x) for x in reference.get("columns") or []]
        common = sorted(set(cn) & set(rn))[:_MAX_METRICS]
        matched = 0
        for name in common:
            ci, ri = cn.index(name), rn.index(name)
            if _ID.fullmatch(_name(name)) or not _numeric_indices(candidate, cn, [ci]) or not _numeric_indices(reference, rn, [ri]):
                continue
            matched += _pair_result(facts, issues, candidate, reference, ci, ri, "complementary_sheets")
        if not matched:
            issues.append({"kind": "cannot_determine", "stage": "domain_pairing", "detail": "No paired comparison: metric, compatible units, or unique non-empty matching sample IDs were not confirmed.", "evidence": [_evidence(candidate), _evidence(reference)]})
        count += matched
    return count


def _document_unit_present(text, unit):
    if unit is None:
        return False
    normalized = _name(text)
    normalized_unit = _name(unit).replace("_per_", "/")
    return normalized_unit in normalized or normalized_unit.replace("/", "_per_") in normalized


def _lod_loq(data, fits, facts):
    pages = 0
    number = _NUMBER
    for document in data.get("documents") or []:
        for page in document.get("pages") or []:
            text = str(page.get("text") or "")
            if not (_LOD.search(text) or _LOQ.search(text) or re.search(r"3\s*[.]\s*3|sigma|σ", text, re.I)):
                continue
            pages += 1
            match = _LOD.search(text) or _LOQ.search(text) or re.search(r"3\s*[.]\s*3|sigma|σ", text, re.I)
            start = max(0, match.start() - 160)
            quote = re.sub(r"\s+", " ", text[start:start + 650]).strip()
            evidence = str(document.get("path")) + "#page=" + str(page.get("page"))
            lod_formula = re.search(r"3\s*[.]\s*3\s*(?:\*|x|×)?\s*(?:sigma|σ)\s*/\s*(?:slope|\bS\b)", text, re.I)
            loq_formula = re.search(r"10\s*(?:\*|x|×)?\s*(?:sigma|σ)\s*/\s*(?:slope|\bS\b)", text, re.I)
            sigma_match = re.search(r"(?:\bsigma\b|σ|standard\s+deviation\s+of\s+(?:the\s+)?(?:blank|response|residual)[a-z ]*)\s*(?:=|:)\s*(" + number + r")", text, re.I)
            slope_match = re.search(r"(?:\bslope\b|\bS\b)\s*(?:=|:)\s*(" + number + r")", text, re.I)
            calculated = False
            if (lod_formula or loq_formula) and sigma_match and slope_match:
                sigma, slope = float(sigma_match.group(1)), float(slope_match.group(1))
                for fit in fits:
                    if slope != 0 and math.isclose(slope, fit["slope"], rel_tol=1e-9, abs_tol=0.0) and _document_unit_present(text, fit["x_unit"]) and _document_unit_present(text, fit["y_unit"]):
                        value = {}
                        if lod_formula:
                            value["LOD"] = 3.3 * sigma / slope
                        if loq_formula:
                            value["LOQ"] = 10.0 * sigma / slope
                        facts.append(_fact("derived", "lod_loq_sigma_over_slope", {"document_id": document.get("id"), "page": page.get("page"), "calibration_table": fit["table_id"]}, value, fit["x_unit"], "Applied only the page-explicit 3.3*sigma/S and/or 10*sigma/S formula to the page-explicit sigma and slope; slope matched the listed OLS within rel_tol=1e-9 and native x/y units were present on the same page. " + sigma_match.group(0) + "; " + slope_match.group(0), [evidence] + fit["evidence"], "Formula inputs, unit linkage, and calibration identity are explicit; no default multiplier, fabricated sigma, or acceptance decision is used."))
                        calculated = True
                        break
            facts.append(_fact("source_reported", "lod_loq_source_context", {"document_id": document.get("id"), "page": page.get("page")}, {"quote": quote, "recalculation": "derived_above_with_explicit_inputs" if calculated else "not_estimable_from_supplied_matching_inputs"}, None, "Verbatim contiguous PDF page excerpt with whitespace normalized; not a bulk numeric alignment or an inferred reported result.", [evidence], "When not estimable, no default 3.3/10 multiplier, substituted sigma/slope, or guessed LOD/LOQ is supplied. Consult the cited page for its full method context."))
    return pages


def analyze_domain(data, case):
    """Return source-bounded method facts; ``case`` is identity/context, never data."""
    data = data if isinstance(data, dict) else {}
    facts, issues = [], []
    fits = _calibration(data, facts, issues)
    precision = _precision(data, facts, issues)
    recovery = _recovery(data, facts, issues)
    paired = _pairing(data, facts, issues)
    lod_pages = _lod_loq(data, fits, facts)
    methods = [
        {"name": "domain_unweighted_calibration_ols", "algorithm": "Explicit concentration/response columns and calibration/method support; all selected actual rows, intercept OLS, R-squared, residuals, and nonzero-slope back-calculation.", "limitations": "No weighting, transforms, row averaging, or accuracy/validation inference."},
        {"name": "domain_operational_precision_recovery", "algorithm": "Level-aware mean, sample SD and RSD; explicit same-row nominal/found recovery or unique-ID, equal-unit spiked/unspiked/added subtraction recovery only.", "limitations": "No independent-replicate claim, unconfirmed subtraction recovery, default recovery interval, or pharmacopoeial cutoff."},
        {"name": "domain_paired_method_difference", "algorithm": "Candidate minus Reference only for matching unique stable sample IDs and compatible metrics/units.", "limitations": "Descriptive differences, not significance, correlation-driven agreement, or equivalence."},
        {"name": "domain_lod_loq_evidence", "algorithm": "Page-anchored source context and tightly matched explicit sigma/slope formula inputs only.", "limitations": "Otherwise not estimable; min/max values are never used as LOD/LOQ."},
    ]
    coverage = {
        "facts": len(facts), "calibration_fits": len(fits), "calibration_rows": sum(fit["n"] for fit in fits),
        "precision_groups": precision, "recovery_rows": recovery, "paired_rows": paired,
        "paired_comparisons": sum(fact["metric"] == "paired_candidate_minus_reference" for fact in facts),
        "lod_loq_pages": lod_pages, "rows_per_table_limit": _MAX_ROWS, "issues": len(issues),
        "limitations": ["Only declared bounded method recalculations are performed; other tables remain outside this domain module.", "Units and equations are not inferred from drug identity or generic acceptance templates."],
    }
    return _safe({"facts": facts, "issues": issues, "methods": methods, "coverage": coverage})
