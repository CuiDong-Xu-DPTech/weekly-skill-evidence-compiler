import math
import re


_FIELDS = {"sampleid": "sampleID", "file": "file", "variable": "variable", "column": "column", "group": "group"}
_REQUIRED = ("sampleID", "file", "variable", "column")
_IDENTIFIER = r"[A-Za-z]\w*"
_FIELD_LHS = re.compile(
    r"^(?P<base>" + _IDENTIFIER + r")\s*(?:\(\s*(?P<index>[1-9]\d*)\s*\))?\s*\.\s*"
    r"(?P<field>[A-Za-z]\w*)$"
)
_PARALLEL_LHS = re.compile(
    r"^(?P<base>" + _IDENTIFIER + r")\s*(?:\(\s*(?P<round>[1-9]\d*)\s*\)|\{\s*(?P<brace>[1-9]\d*)\s*\})?$"
)
_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _finish(statements, buffer, start, end, lines):
    code = "".join(buffer).strip()
    if code:
        statements.append({"code": code, "line": start or end, "end_line": end, "text": "\n".join(lines[(start or end) - 1:end]).strip()})
    buffer.clear()
    return None


def _statements(text):
    lines = text.splitlines()
    statements, buffer = [], []
    quote = None
    depth = 0
    line = 1
    start = None
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\r":
            index += 1
            continue
        if quote:
            if start is None:
                start = line
            buffer.append(char)
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    buffer.append(text[index + 1])
                    index += 2
                    continue
                quote = None
            if char == "\n":
                line += 1
            index += 1
            continue
        if char in "'\"":
            if start is None:
                start = line
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "%":
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        if text.startswith("...", index):
            index += 3
            while index < len(text) and text[index] in " \t":
                index += 1
            if index < len(text) and text[index] == "\n":
                line += 1
                index += 1
                buffer.append(" ")
                continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char == ";" and depth == 0:
            start = _finish(statements, buffer, start, line, lines)
            index += 1
            continue
        if char == "\n":
            if depth == 0:
                start = _finish(statements, buffer, start, line, lines)
            elif buffer:
                buffer.append(" ")
            line += 1
            index += 1
            continue
        if not char.isspace() and start is None:
            start = line
        buffer.append(char)
        index += 1
    _finish(statements, buffer, start, min(line, max(1, len(lines))), lines)
    return statements


def _quoted(token):
    if len(token) < 2 or token[0] not in "'\"" or token[-1] != token[0]:
        return None
    quote = token[0]
    inner = token[1:-1]
    result = []
    index = 0
    while index < len(inner):
        if inner[index] == quote:
            if index + 1 < len(inner) and inner[index + 1] == quote:
                result.append(quote)
                index += 2
                continue
            raise ValueError("unescaped quote in string literal")
        result.append(inner[index])
        index += 1
    return "".join(result)


def _items(token):
    result, buffer = [], []
    quote = None
    index = 0
    while index < len(token):
        char = token[index]
        if quote:
            buffer.append(char)
            if char == quote:
                if index + 1 < len(token) and token[index + 1] == quote:
                    buffer.append(token[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
        elif char in "[{(":
            raise ValueError("nested or computed literals are not accepted")
        elif char in ",;" or char.isspace():
            item = "".join(buffer).strip()
            if item:
                result.append(item)
            buffer.clear()
        else:
            buffer.append(char)
        index += 1
    if quote:
        raise ValueError("unterminated string literal")
    item = "".join(buffer).strip()
    if item:
        result.append(item)
    return result


def _literal(code):
    token = code.strip()
    if not token:
        raise ValueError("empty right-hand side")
    string = _quoted(token)
    if string is not None:
        return string
    if _NUMBER.fullmatch(token):
        value = float(token) if any(char in token for char in ".eE") else int(token)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite numeric literal")
        return value
    pairs = {"[": "]", "{": "}"}
    if token[0] in pairs:
        if token[-1] != pairs[token[0]]:
            raise ValueError("unclosed literal array")
        return [_literal(item) for item in _items(token[1:-1])]
    raise ValueError("not a standalone string, numeric, vector, or cell literal")


def _canonical(field):
    return _FIELDS.get(field.lower())


def _slot(slots, namespace, index, field, value, statement):
    key = (namespace, index)
    record = slots.setdefault(key, {})
    slot = {"value": value, "line": statement["line"], "end_line": statement["end_line"], "text": statement["text"]}
    if field in record and record[field]["value"] != value:
        record[field]["conflict"] = True
        record[field]["conflicting_values"] = [record[field]["value"], value]
    else:
        record[field] = slot


def _valid_scalar(value):
    return not isinstance(value, bool) and isinstance(value, (str, int, float)) and (not isinstance(value, float) or math.isfinite(value))


def _valid_column(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value >= 1
    return isinstance(value, list) and bool(value) and all(isinstance(item, int) and not isinstance(item, bool) and item >= 1 for item in value)


def extract_source_curve_mappings(text, source_path=""):
    slots = {}
    issues = []
    for statement in _statements(text):
        code = statement["code"]
        if "=" not in code:
            continue
        lhs, rhs = code.split("=", 1)
        field_match = _FIELD_LHS.fullmatch(lhs.strip())
        parallel_match = _PARALLEL_LHS.fullmatch(lhs.strip())
        field = _canonical(field_match.group("field")) if field_match else _canonical(parallel_match.group("base")) if parallel_match else None
        if not field:
            continue
        try:
            value = _literal(rhs)
        except ValueError as exc:
            issues.append({"kind": "cannot_determine", "line": statement["line"], "detail": "%s: %s" % (statement["text"], exc)})
            continue
        if field_match:
            namespace = "struct:" + field_match.group("base")
            indices = [int(field_match.group("index") or 1)]
            _slot(slots, namespace, indices[0], field, value, statement)
        else:
            namespace = "parallel:indexed_constants"
            explicit_index = parallel_match.group("round") or parallel_match.group("brace")
            values = value if isinstance(value, list) else [value]
            if explicit_index:
                if len(value if isinstance(value, list) else [value]) != 1:
                    issues.append({"kind": "cannot_determine", "line": statement["line"], "detail": "An indexed assignment must contain one scalar: " + statement["text"]})
                    continue
                values = [value[0] if isinstance(value, list) else value]
            for offset, item in enumerate(values):
                index = int(explicit_index) if explicit_index else offset + 1
                _slot(slots, namespace, index, field, item, statement)
    mappings = []
    for (namespace, index), record in sorted(slots.items(), key=lambda item: (item[0][0], item[0][1])):
        invalid = []
        for field, slot in record.items():
            if slot.get("conflict"):
                invalid.append(field + "(conflicting declarations)")
        for field in _REQUIRED:
            if field not in record:
                invalid.append(field + "(missing)")
        if "file" in record and (not isinstance(record["file"]["value"], str) or not record["file"]["value"].strip()):
            invalid.append("file(nonempty string required)")
        if "variable" in record and (not isinstance(record["variable"]["value"], str) or not record["variable"]["value"].strip()):
            invalid.append("variable(nonempty string required)")
        if "sampleID" in record and not _valid_scalar(record["sampleID"]["value"]):
            invalid.append("sampleID(scalar required)")
        if "column" in record and not _valid_column(record["column"]["value"]):
            invalid.append("column(positive integer or integer vector required)")
        if invalid:
            earliest = min(slot["line"] for slot in record.values())
            issues.append({"kind": "cannot_determine", "line": earliest, "detail": "Incomplete explicit mapping %s[%d]: %s" % (namespace, index, ", ".join(sorted(set(invalid))))})
            continue
        mapping = {
            "source_path": str(source_path),
            "declaration": {"form": namespace.split(":", 1)[0], "variable": namespace.split(":", 1)[1] if namespace.startswith("struct:") else None, "index": int(index), "index_base": 1},
            "sampleID": record["sampleID"]["value"],
            "file": record["file"]["value"],
            "variable": record["variable"]["value"],
            "column": record["column"]["value"],
            "group": None,
            "group_status": "undetermined",
            "group_uncertainty": "No explicit usable constant group declaration at the same mapping index",
            "binding_status": "unbound_declared_only",
            "source_note": "Literal declarations only; no filename, shape, count, or similarity matching was applied",
            "evidence": {field: {"line": record[field]["line"], "end_line": record[field]["end_line"], "text": record[field]["text"]} for field in _REQUIRED},
        }
        if "group" in record:
            group = record["group"]["value"]
            if _valid_scalar(group) and (not isinstance(group, str) or group.strip()):
                mapping["group"] = group
                mapping["group_status"] = "explicit_constant"
                mapping.pop("group_uncertainty")
            else:
                mapping["group_uncertainty"] = "Declared group is not a usable nonempty scalar constant"
            mapping["evidence"]["group"] = {"line": record["group"]["line"], "end_line": record["group"]["end_line"], "text": record["group"]["text"], "literal": group}
        mappings.append(mapping)
    return {"mappings": mappings, "issues": issues}
