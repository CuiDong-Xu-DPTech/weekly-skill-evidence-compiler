import csv
import datetime as _datetime
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess


_MAX_CELLS = 2000000
_MAX_ROWS = 250000
_MAX_TEXT_BYTES = 64 * 1024 * 1024
_MAX_JSON_BYTES = 128 * 1024 * 1024
_MAX_WORKBOOK_BYTES = 256 * 1024 * 1024
_MAX_MAT_BYTES = 256 * 1024 * 1024
_MAX_PDF_BYTES = 512 * 1024 * 1024
_MAX_PDF_PAGES = 5000
_MAX_PDF_CHARS = 50000000
_MAX_MAT_EXPANDED_LEAVES = 10000
_MAX_MAT_EXPANDED_VALUES = 5000000
_MAX_PREVIEW_ITEMS = 20
_DATA_KEYS = {
    "split", "splits", "estimator", "estimators", "record", "records",
    "score", "scores", "result", "results", "observation", "observations",
    "data", "measurement", "measurements", "metric", "metrics", "fold",
    "folds", "prediction", "predictions", "sample", "samples", "target",
    "targets",
}
_SKIP_JSON_KEYS = {"outputcontract", "caseid", "attachments", "attachment", "artifacts", "artifact", "analysisrulesv1", "analysisrules", "qcrules", "modelconfig"}
_REFERENCE_KEYS = {"attachments", "attachment", "artifacts", "artifact", "materials", "material", "files"}
_PATH_KEYS = {"path", "file", "filepath", "file_path", "filename", "uri", "url"}
_TEXT_SUFFIXES = {".m", ".md", ".markdown", ".log", ".text", ".py", ".r", ".sql", ".yaml", ".yml", ".xml"}
_XLSX_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xltm"}
_SENSITIVE_NAMES = {
    ".env", "credentials", "credentials.json", "credential.json", "secrets",
    "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa.pub",
    "id_dsa.pub", "id_ecdsa.pub", "id_ed25519.pub", "known_hosts", "authorized_keys",
}


def _norm_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _kind(path):
    suffix = path.suffix.lower()
    if suffix in _XLSX_SUFFIXES:
        return "xlsx"
    if suffix in {".csv", ".tsv"}:
        return "csv"
    if suffix == ".json":
        return "json"
    if suffix == ".pdf":
        return "pdf"
    if suffix == ".mat":
        return "mat"
    if suffix in _TEXT_SUFFIXES or suffix == ".txt":
        return "text"
    return "other"


def _sensitive(path):
    lowered = [part.lower() for part in path.parts]
    if any(part in {".ssh", ".gnupg"} for part in lowered):
        return True
    name = path.name.lower()
    return (
        name in _SENSITIVE_NAMES
        or name.startswith(".env.")
        or name.startswith(("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"))
        or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".kdbx"}
    )


def _encoding(prefix):
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    return "utf-8-sig"


def _decode(data):
    encoding = _encoding(data[:4])
    return data.decode(encoding, errors="strict"), encoding


def _is_scalar(value):
    return value is None or isinstance(value, (str, int, float, bool))


def _pointer(value):
    return str(value).replace("~", "~0").replace("/", "~1")


def _a1_column(index):
    result = ""
    value = index
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result or "A"


def _header_mapping(columns):
    mapping = {}
    positions = {}
    for index, name in enumerate(columns):
        positions.setdefault(name, []).append(index)
    for name, indices in positions.items():
        mapping[name] = indices[0] if len(indices) == 1 else indices
    return {"row": 1, "mapping": mapping, "column_index_base": 0}


def _plain(value, depth=0):
    if depth > 100:
        return "<maximum nesting depth reached>"
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
        return value.isoformat()
    if isinstance(value, _datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): _plain(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item, depth + 1) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return _plain(value.item(), depth + 1)
        except (TypeError, ValueError):
            pass
    if hasattr(value, "tolist") and callable(value.tolist):
        try:
            return _plain(value.tolist(), depth + 1)
        except (TypeError, ValueError):
            pass
    return str(value)


class _Reader:
    def __init__(self, case_path, task_path, input_dir, work_dir):
        self.case_path = Path(case_path).resolve()
        self.task_path = Path(task_path).resolve()
        self.input_root = Path(input_dir).resolve()
        self.work_root = Path(work_dir).resolve()
        self.packet = {
            "inputs": [],
            "tables": [],
            "documents": [],
            "arrays": [],
            "other_files": [],
            "source_curve_mappings": [],
            "issues": [],
            "missing": [],
            "dependencies": self._dependencies(),
        }
        self.inputs = {}
        self.references = set()
        self.case_data = None
        self.table_counter = 0
        self.document_counter = 0
        self.array_counter = 0
        self.missing_truncated = 0

    def _dependencies(self):
        result = {}
        for module_name, distribution in (
            ("openpyxl", "openpyxl"),
            ("fitz", "PyMuPDF"),
            ("scipy", "scipy"),
            ("h5py", "h5py"),
        ):
            try:
                available = importlib.util.find_spec(module_name) is not None
            except (ImportError, ValueError):
                available = False
            try:
                version = importlib.metadata.version(distribution)
            except importlib.metadata.PackageNotFoundError:
                version = None
            result[module_name] = {"available": available, "version": version}
        command = shutil.which("pdftotext")
        result["pdftotext"] = {"available": command is not None, "path": command, "version": None}
        return result

    def issue(self, kind, path, detail, stage, input_entry=None, status=None):
        item = {"kind": kind, "path": str(path), "detail": str(detail), "stage": stage}
        self.packet["issues"].append(item)
        if input_entry is not None:
            local = dict(item)
            local.pop("path", None)
            input_entry["issues"].append(local)
            if status:
                input_entry["status"] = status
        return item

    def _missing(self, path, table_id, row_ref, column, reason):
        if len(self.packet["missing"]) < 10000:
            self.packet["missing"].append({
                "path": str(path), "table_id": table_id, "row_ref": row_ref,
                "column": str(column), "reason": reason,
            })
        else:
            self.missing_truncated += 1

    def scalar(self, value, context=None):
        if hasattr(value, "item") and callable(value.item) and not isinstance(value, (str, bytes)):
            try:
                value = value.item()
            except (TypeError, ValueError):
                pass
        if isinstance(value, float) and not math.isfinite(value):
            if context:
                self._missing(*context, "non_finite_float")
            return None
        if value is None or isinstance(value, (str, bool, int, float)):
            return value
        if isinstance(value, (_datetime.datetime, _datetime.date, _datetime.time)):
            return value.isoformat()
        if isinstance(value, _datetime.timedelta):
            return str(value)
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, complex):
            return str(value)
        if isinstance(value, (list, tuple)):
            return json.dumps(_plain(value), ensure_ascii=False, allow_nan=False)
        return str(value)

    def add_table(self, path, sheet, name, columns, rows, row_refs, headers, metadata, issues=None, formula_cells=None):
        self.table_counter += 1
        table_id = "t%03d" % self.table_counter
        safe_rows = []
        for row_index, row in enumerate(rows):
            ref = row_refs[row_index] if row_index < len(row_refs) else None
            safe_row = []
            for col_index, value in enumerate(row):
                column = columns[col_index] if col_index < len(columns) else str(col_index)
                safe_row.append(self.scalar(value, (path, table_id, ref, column)))
            safe_rows.append(safe_row)
        table = {
            "id": table_id,
            "path": str(path),
            "sheet": sheet,
            "name": str(name),
            "columns": [str(column) for column in columns],
            "rows": safe_rows,
            "row_refs": list(row_refs),
            "headers": headers,
            "metadata": _plain(metadata or {}),
            "issues": _plain(issues or []),
        }
        if formula_cells is not None:
            table["formula_cells"] = _plain(formula_cells)
        self.packet["tables"].append(table)
        return table_id

    def discover(self):
        found = set()
        if not self.input_root.is_dir():
            self.issue("missing_material", self.input_root, "input_dir does not exist or is not a directory", "discover")
            return []
        excluded = {self.case_path, self.task_path, self.input_root / "case.json", self.input_root / "task.md"}
        for directory, dirnames, filenames in os.walk(str(self.input_root), followlinks=False):
            current = Path(directory)
            kept = []
            for name in sorted(dirnames):
                candidate = current / name
                if name in {".git", ".ssh", ".gnupg", "__pycache__"} or candidate.is_symlink():
                    if candidate.is_symlink():
                        resolved = candidate.resolve()
                        if not _within(resolved, self.input_root):
                            self.issue("cannot_determine", candidate, "directory symlink escapes input_dir; not followed", "discover")
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in sorted(filenames):
                path = current / name
                try:
                    resolved = path.resolve(strict=True)
                except OSError as exc:
                    self.issue("runtime_failure", path, str(exc), "discover")
                    continue
                if path.is_symlink():
                    if not _within(resolved, self.input_root):
                        self.issue("cannot_determine", path, "file symlink escapes input_dir; not read", "discover")
                        continue
                if not _within(resolved, self.input_root):
                    self.issue("cannot_determine", path, "resolved path escapes input_dir; not read", "discover")
                    continue
                if resolved in excluded or name.lower() in {"case.json", "task.md"}:
                    continue
                if _sensitive(resolved.relative_to(self.input_root)):
                    continue
                if resolved.is_file():
                    found.add(resolved)
        for path in sorted(self.references):
            if path in excluded or path.name.lower() in {"case.json", "task.md"}:
                continue
            found.add(path)
        return sorted(found, key=lambda item: item.relative_to(self.input_root).as_posix())

    def _reference_candidate(self, raw, pointer):
        value = raw.strip()
        if not value or "\x00" in value or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value) and not value.startswith("file://"):
            return
        if value.startswith("file://"):
            value = value[7:]
        candidates = []
        supplied = Path(value)
        try:
            if supplied.is_absolute():
                resolved = supplied.resolve()
                if _within(resolved, self.input_root):
                    candidates.append(resolved)
                else:
                    parts = supplied.parts
                    if "input" in parts:
                        relative = Path(*parts[parts.index("input") + 1:])
                        candidates.append((self.input_root / relative).resolve())
                    virtual = (self.input_root / value.lstrip("/\\")).resolve()
                    if virtual.exists():
                        candidates.append(virtual)
            else:
                candidates.append((self.input_root / supplied).resolve())
                if _within(self.case_path.parent, self.input_root):
                    candidates.append((self.case_path.parent / supplied).resolve())
        except (OSError, RuntimeError) as exc:
            self.issue("runtime_failure", self.case_path, "Invalid attachment reference %s: %s" % (pointer, exc), "discover")
            return
        if not candidates:
            self.issue("cannot_determine", self.case_path, "Attachment reference %s resolves outside input_dir: %s" % (pointer, value), "discover")
            return
        accepted = False
        missing = []
        for candidate in candidates:
            if not _within(candidate, self.input_root):
                continue
            accepted = True
            if candidate.is_symlink() and not _within(candidate.resolve(), self.input_root):
                self.issue("cannot_determine", candidate, "Referenced symlink escapes input_dir; not read", "discover")
                continue
            if _sensitive(candidate.relative_to(self.input_root)):
                continue
            if candidate.is_file():
                self.references.add(candidate.resolve())
                return
            missing.append(candidate)
        if accepted and missing:
            self.issue("missing_material", missing[0], "Referenced attachment at %s was not found" % pointer, "discover")
        elif not accepted:
            self.issue("cannot_determine", self.case_path, "Attachment reference %s escapes input_dir" % pointer, "discover")

    def collect_references(self, node, pointer="", reference_context=False):
        if isinstance(node, dict):
            for key, value in node.items():
                normalized = _norm_key(key)
                child_pointer = pointer + "/" + _pointer(key)
                if normalized == "outputcontract":
                    continue
                child_context = reference_context or normalized in _REFERENCE_KEYS
                if reference_context and normalized in _PATH_KEYS and isinstance(value, str):
                    self._reference_candidate(value, child_pointer)
                self.collect_references(value, child_pointer, child_context)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                child_pointer = pointer + "/" + str(index)
                if reference_context and isinstance(value, str):
                    self._reference_candidate(value, child_pointer)
                self.collect_references(value, child_pointer, reference_context)

    def read_case(self):
        if not self.case_path.is_file():
            self.issue("missing_material", self.case_path, "case.json is not available", "case_json")
            return
        try:
            if self.case_path.stat().st_size > _MAX_JSON_BYTES:
                self.issue("runtime_failure", self.case_path, "case.json exceeds the safe parsing limit", "case_json")
                return
            with self.case_path.open("rb") as handle:
                text, _ = _decode(handle.read(_MAX_JSON_BYTES + 1))
            self.case_data = json.loads(text)
            if not isinstance(self.case_data, (dict, list)):
                raise ValueError("case JSON root must be an object or array")
            if isinstance(self.case_data, dict):
                case_id = self.case_data.get("case_id", self.case_data.get("id"))
                if _is_scalar(case_id):
                    self.packet["case_id"] = self.scalar(case_id)
            self.collect_references(self.case_data)
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            self.issue("runtime_failure", self.case_path, str(exc), "case_json")
        if not self.task_path.is_file():
            self.issue("missing_material", self.task_path, "task.md is not available", "task")

    def extract_json_tables(self, node, path, name=None, pointer="", parent_key=None, source="json"):
        if isinstance(node, dict):
            normalized_parent = _norm_key(parent_key) if parent_key else None
            if normalized_parent not in {"outputcontract", "caseid"}:
                self._json_dict_table(node, path, name, pointer, normalized_parent, source)
            for key, value in node.items():
                normalized = _norm_key(key)
                if normalized in _SKIP_JSON_KEYS:
                    continue
                if isinstance(value, (dict, list)):
                    self.extract_json_tables(value, path, name, pointer + "/" + _pointer(key), normalized, source)
        elif isinstance(node, list):
            self._json_list_table(node, path, name, pointer, _norm_key(parent_key) if parent_key else None, source)
            for index, value in enumerate(node):
                if isinstance(value, (dict, list)):
                    self.extract_json_tables(value, path, name, pointer + "/" + str(index), None, source)

    def _json_dict_table(self, node, path, name, pointer, parent_key, source):
        if parent_key not in _DATA_KEYS or not node:
            return
        if all(isinstance(value, dict) for value in node.values()):
            columns = ["__key"]
            nested = set()
            for record in node.values():
                for key, value in record.items():
                    if _is_scalar(value):
                        if key not in columns:
                            columns.append(str(key))
                    else:
                        nested.add(str(key))
            if len(columns) == 1:
                return
            rows, refs = [], []
            for key, record in node.items():
                rows.append([key] + [record.get(column) for column in columns[1:]])
                refs.append(pointer + "/" + _pointer(key))
            metadata = {"source": source, "json_pointer": pointer, "key_column": "__key", "key_source": "mapping keys"}
            if nested:
                metadata["nested_keys_not_columns"] = sorted(nested)
            headers = _header_mapping(columns)
            headers["row"] = None
            self.add_table(path, None, pointer or name, columns, rows, refs, headers, metadata)
        elif all(_is_scalar(value) for value in node.values()):
            columns = [str(key) for key in node]
            headers = _header_mapping(columns)
            headers["row"] = None
            self.add_table(path, None, pointer or name, columns, [[node[key] for key in node]], [pointer or "/"], headers, {"source": source, "json_pointer": pointer, "record_mapping": True})
        elif not any(_norm_key(key) in _DATA_KEYS for key in node):
            rows, refs, empty_sequences = [], [], []
            for key, value in node.items():
                item_pointer = pointer + "/" + _pointer(key)
                if isinstance(value, list) and all(_is_scalar(item) for item in value):
                    if not value:
                        empty_sequences.append(str(key))
                    for index, item in enumerate(value):
                        rows.append([str(key), index, item])
                        refs.append(item_pointer + "/" + str(index))
                elif _is_scalar(value):
                    rows.append([str(key), None, value])
                    refs.append(item_pointer)
            if not rows:
                return
            columns = ["__key", "index", "value"]
            headers = _header_mapping(columns)
            headers["row"] = None
            metadata = {
                "source": source, "json_pointer": pointer, "mapping_form": "key/index/value",
                "key_column": "__key", "sequence_index_base": 0,
                "empty_sequence_keys": empty_sequences,
            }
            self.add_table(path, None, pointer or name, columns, rows, refs, headers, metadata)

    def _json_list_table(self, node, path, name, pointer, parent_key, source):
        if not node:
            return
        structural = parent_key in {"resources", "materials", "files", "inputs"}
        if structural:
            return
        columns, rows, refs, metadata = None, [], [], {"source": source, "json_pointer": pointer}
        if all(isinstance(item, dict) for item in node):
            columns, nested = [], set()
            for record in node:
                for key, value in record.items():
                    if _is_scalar(value):
                        if str(key) not in columns:
                            columns.append(str(key))
                    else:
                        nested.add(str(key))
            if not columns:
                return
            missing_keys = set()
            for index, record in enumerate(node):
                rows.append([record.get(column) for column in columns])
                refs.append(pointer + "/" + str(index))
                missing_keys.update(column for column in columns if column not in record)
            if nested:
                metadata["nested_keys_not_columns"] = sorted(nested)
            if missing_keys:
                metadata["missing_keys_filled_with_null"] = sorted(missing_keys)
        elif parent_key in _DATA_KEYS and all(isinstance(item, list) for item in node):
            width = max((len(item) for item in node), default=0)
            if not width or any(not all(_is_scalar(value) for value in item) for item in node):
                return
            columns = ["column_%d" % (index + 1) for index in range(width)]
            metadata["generated_columns"] = True
            for index, item in enumerate(node):
                rows.append(item + [None] * (width - len(item)))
                refs.append(pointer + "/" + str(index))
        elif parent_key in _DATA_KEYS and all(_is_scalar(item) for item in node):
            columns = [str(parent_key or "value")]
            rows = [[item] for item in node]
            refs = [pointer + "/" + str(index) for index in range(len(node))]
        else:
            return
        headers = _header_mapping(columns)
        headers["row"] = None
        self.add_table(path, None, pointer or name or "json", columns, rows, refs, headers, metadata)

    def input_entry(self, path):
        try:
            size = path.stat().st_size
            digest = _sha256(path)
        except OSError as exc:
            self.issue("runtime_failure", path, str(exc), "hash")
            return None
        entry = {
            "path": str(path), "name": path.name, "sha256": digest, "size": int(size),
            "kind": _kind(path), "status": "read", "parser": "sha256", "issues": [],
        }
        self.packet["inputs"].append(entry)
        self.inputs[str(path)] = entry
        return entry

    def read_file(self, path, entry):
        kind = entry["kind"]
        if kind == "xlsx":
            self.read_xlsx(path, entry)
        elif kind == "csv" or (kind == "text" and path.suffix.lower() == ".txt"):
            self.read_csv(path, entry)
        elif kind == "json":
            self.read_json(path, entry)
        elif kind == "pdf":
            self.read_pdf(path, entry)
        elif kind == "mat":
            self.read_mat(path, entry)
        elif kind == "text":
            self.read_text(path, entry)
        else:
            self.read_other(path, entry)

    def read_json(self, path, entry):
        entry["parser"] = "stdlib.json"
        try:
            size = path.stat().st_size
            if size > _MAX_JSON_BYTES:
                self.issue("runtime_failure", path, "JSON exceeds the safe parsing limit; content not loaded", "json", entry, "partial")
                return
            with path.open("rb") as handle:
                raw = handle.read(_MAX_JSON_BYTES + 1)
            text, encoding = _decode(raw)
            value = json.loads(text)
            before = len(self.packet["tables"])
            self.extract_json_tables(value, path, path.name, source="json_file")
            entry["metadata"] = {"encoding": encoding, "tables_extracted": len(self.packet["tables"]) - before}
            if len(self.packet["tables"]) == before:
                self.packet["other_files"].append({
                    "path": str(path), "name": path.name, "sha256": entry["sha256"],
                    "text": text, "metadata": {"encoding": encoding, "json_table_candidates": 0},
                })
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            self.issue("runtime_failure", path, str(exc), "json", entry, "failed")

    def read_csv(self, path, entry):
        entry["parser"] = "stdlib.csv"
        issues = []
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                prefix = handle.read(65536)
            encoding = _encoding(prefix)
            if b"\x00" in prefix and encoding != "utf-16":
                raise UnicodeError("NUL bytes indicate that this is not a supported delimited text file")
            sample = prefix.decode(encoding, errors="strict")
            forced = "\t" if path.suffix.lower() == ".tsv" else None
            delimiter = forced or ","
            hint = None
            try:
                hint = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
                if forced is None and hint:
                    delimiter = hint
            except csv.Error:
                pass
            records = []
            truncated = False
            cells = 0
            with path.open("r", encoding=encoding, errors="strict", newline="") as handle:
                reader = csv.reader(handle, delimiter=delimiter)
                previous_end = 0
                for row in reader:
                    end = reader.line_num
                    start = previous_end + 1
                    previous_end = end
                    cells += len(row)
                    if len(records) >= _MAX_ROWS or cells > _MAX_CELLS:
                        truncated = True
                        break
                    records.append((start, end, row))
            if path.suffix.lower() == ".txt":
                nonempty = [record for record in records if any(field.strip() for field in record[2])]
                widths = [len(record[2]) for record in nonempty]
                reliable = bool(hint) and len(nonempty) >= 2 and widths and max(widths) >= 2
                if reliable:
                    expected = max(set(widths), key=widths.count)
                    reliable = widths.count(expected) / float(len(widths)) >= 0.8
                if not reliable:
                    self.read_text(path, entry)
                    return
            if not records:
                self.issue("cannot_determine", path, "No delimited records were found", "csv", entry, "partial")
                return
            header_index = next((index for index, record in enumerate(records) if any(field != "" for field in record[2])), None)
            if header_index is None:
                self.issue("cannot_determine", path, "No non-empty header row was found", "csv", entry, "partial")
                return
            header_start, header_end, header = records[header_index]
            width = max([len(header)] + [len(record[2]) for record in records[header_index + 1:]])
            columns = list(header) + ["column_%d" % (index + 1) for index in range(len(header), width)]
            if any(name == "" for name in header):
                issues.append("Blank header cells retained at zero-based indices %s" % [i for i, name in enumerate(header) if name == ""])
            duplicates = sorted({name for name in header if header.count(name) > 1})
            if duplicates:
                issues.append("Duplicate header names retained: %s" % duplicates)
            if len(header) < width:
                issues.append("Generated names for %d data columns absent from the header" % (width - len(header)))
            rows, refs, blank_lines = [], [], []
            malformed = []
            for start, end, row in records[header_index + 1:]:
                ref = "%s:%d" % (path, start) if start == end else "%s:%d-%d" % (path, start, end)
                if not row:
                    blank_lines.append(ref)
                    continue
                if len(row) != len(header):
                    malformed.append({"row_ref": ref, "fields": len(row), "header_fields": len(header)})
                rows.append(row + [None] * (width - len(row)))
                refs.append(ref)
            if malformed:
                issues.append("Rows with missing or excess fields: %s" % json.dumps(malformed[:100], ensure_ascii=False))
                if len(malformed) > 100:
                    issues.append("Malformed-row details truncated: %d additional rows" % (len(malformed) - 100))
            if truncated:
                issues.append("Delimited input truncated at row or cell safety limit")
            headers = _header_mapping(columns)
            headers["row"] = header_start
            headers["end_line"] = header_end
            metadata = {
                "encoding": encoding, "delimiter": delimiter, "delimiter_hint": hint,
                "header_end_line": header_end, "blank_line_refs": blank_lines,
                "malformed_row_count": len(malformed), "truncated": truncated,
                "source_note": "Unconverted CSV fields; structurally missing trailing fields are null",
            }
            table_issues = list(issues)
            table_id = self.add_table(path, None, path.name, columns, rows, refs, headers, metadata, table_issues)
            for detail in issues:
                item = self.issue("cannot_determine" if not truncated else "runtime_failure", path, detail, "csv", entry, "partial" if truncated else None)
                item["table_id"] = table_id
        except (OSError, UnicodeError, csv.Error) as exc:
            self.issue("runtime_failure", path, str(exc), "csv", entry, "partial" if 'records' in locals() and records else "failed")

    def read_xlsx(self, path, entry):
        if not self.packet["dependencies"]["openpyxl"]["available"]:
            self.issue("runtime_failure", path, "openpyxl is unavailable; workbook not parsed", "xlsx", entry, "partial")
            return
        try:
            if path.stat().st_size > _MAX_WORKBOOK_BYTES:
                self.issue("runtime_failure", path, "Workbook exceeds the safe in-memory parsing limit", "xlsx", entry, "partial")
                return
            openpyxl = importlib.import_module("openpyxl")
            entry["parser"] = "openpyxl %s" % getattr(openpyxl, "__version__", "unknown")
            workbook = openpyxl.load_workbook(str(path), data_only=False, read_only=False, keep_links=False)
            try:
                cached = openpyxl.load_workbook(str(path), data_only=True, read_only=False, keep_links=False)
            except Exception as exc:
                cached = None
                self.issue("runtime_failure", path, "Formula-cache view could not be opened: %s" % exc, "xlsx_cache", entry, "partial")
            for sheet_index, worksheet in enumerate(workbook.worksheets):
                cached_sheet = cached[worksheet.title] if cached is not None and worksheet.title in cached.sheetnames else None
                self._xlsx_sheet(path, entry, worksheet, cached_sheet, sheet_index)
            if cached is not None:
                cached.close()
            workbook.close()
        except Exception as exc:
            self.issue("runtime_failure", path, "%s: %s" % (type(exc).__name__, exc), "xlsx", entry, "partial" if self.packet["tables"] else "failed")

    def _xlsx_sheet(self, path, entry, worksheet, cached_sheet, sheet_index):
        sheet = worksheet.title
        issues = []
        declared_rows = int(worksheet.max_row or 0)
        declared_columns = int(worksheet.max_column or 0)
        read_rows = min(declared_rows, _MAX_ROWS)
        read_columns = min(declared_columns, max(1, _MAX_CELLS // max(1, read_rows)))
        truncated = read_rows < declared_rows or read_columns < declared_columns
        values = []
        formula_values = []
        formula_cells = []
        formula_truncated = 0
        missing_cache = 0
        missing_cache_formulas = []
        for row_index in range(1, read_rows + 1):
            cached_row, formula_row = [], []
            for column_index in range(1, read_columns + 1):
                cell = worksheet.cell(row=row_index, column=column_index)
                cached_cell = cached_sheet.cell(row=row_index, column=column_index) if cached_sheet is not None else None
                value = cell.value
                cached_value = cached_cell.value if cached_cell is not None else (None if cell.data_type == "f" else value)
                if cell.data_type == "f":
                    formula = value if isinstance(value, str) else getattr(value, "text", str(value))
                    if len(formula_cells) < 300:
                        formula_cells.append({"cell": cell.coordinate, "formula": formula, "cached_value": self.scalar(cached_value)})
                    else:
                        formula_truncated += 1
                    if cached_value is None:
                        missing_cache += 1
                        missing_cache_formulas.append({"cell": cell.coordinate, "formula": formula})
                    formula_row.append(value)
                    cached_row.append(cached_value)
                else:
                    formula_row.append(value)
                    cached_row.append(value)
            values.append(cached_row)
            formula_values.append(formula_row)
        last_row = 0
        last_column = 0
        for row_index in range(read_rows):
            for column_index in range(read_columns):
                if formula_values[row_index][column_index] is not None:
                    last_row = max(last_row, row_index + 1)
                    last_column = max(last_column, column_index + 1)
        values = [row[:last_column] for row in values[:last_row]]
        formula_values = [row[:last_column] for row in formula_values[:last_row]]
        header_row = next((index + 1 for index, row in enumerate(formula_values[:10]) if any(value is not None and (not isinstance(value, str) or value.strip()) for value in row)), None)
        if header_row is None:
            header_row = 1 if last_row else None
        width = last_column
        header = formula_values[header_row - 1] if header_row and header_row <= len(formula_values) else []
        columns = ["" if value is None else str(value) for value in header]
        empty_sheet = last_row == 0 and last_column == 0
        if len(columns) < width:
            columns.extend("column_%d" % (index + 1) for index in range(len(columns), width))
        if any(name == "" for name in columns):
            issues.append("Blank worksheet header cells retained")
        duplicates = sorted({name for name in columns if columns.count(name) > 1})
        if duplicates:
            issues.append("Duplicate worksheet headers retained: %s" % duplicates)
        rows, refs = [], []
        if header_row:
            for index in range(header_row, len(values)):
                row = values[index] + [None] * (width - len(values[index]))
                rows.append(row)
                refs.append("%s!A%d:%s%d" % (sheet, index + 1, _a1_column(max(1, width)), index + 1))
        headers = _header_mapping(columns)
        headers["row"] = header_row
        hidden_columns = [key for key, dimension in worksheet.column_dimensions.items() if dimension.hidden]
        hidden_rows = [int(key) for key, dimension in worksheet.row_dimensions.items() if dimension.hidden]
        merged = [str(item) for item in worksheet.merged_cells.ranges]
        coverage = []
        for column_index, name in enumerate(columns):
            nonempty = sum(1 for row in rows if column_index < len(row) and row[column_index] is not None and row[column_index] != "")
            coverage.append({"index": column_index, "name": name, "nonempty_data_cells": nonempty})
        metadata = {
            "sheet_index": sheet_index, "sheet_state": str(worksheet.sheet_state),
            "empty_sheet": empty_sheet, "declared_dimension": worksheet.calculate_dimension(),
            "declared_max_row": declared_rows, "declared_max_column": declared_columns,
            "last_nonempty_row": last_row, "last_nonempty_column": last_column,
            "hidden_columns": hidden_columns, "hidden_rows": hidden_rows,
            "merged_ranges": merged, "merged_non_anchor_cells": "retained as null",
            "coverage": coverage, "formula_cache_missing": missing_cache,
            "formula_cache_missing_formulas": missing_cache_formulas,
            "formula_recalculated": False,
            "formula_cell_count": len(formula_cells) + formula_truncated,
            "formula_cells_limit": 300, "formula_cells_returned": len(formula_cells),
            "formula_cells_complete": formula_truncated == 0,
            "formula_cells_truncated": formula_truncated, "truncated": truncated,
            "source_note": "data_only cache used for formula results; missing caches remain null and formulas are not recalculated",
        }
        if truncated:
            issues.append("Worksheet truncated at row or cell safety limit")
        if formula_truncated:
            issues.append("formula_cells list limited to 300; %d additional formulas omitted from formula_cells; their main-table cached values remain available" % formula_truncated)
        table_id = self.add_table(path, sheet, "%s::%s" % (path.name, sheet), columns, rows, refs, headers, metadata, issues, formula_cells)
        for detail in issues:
            item = self.issue("cannot_determine" if not truncated else "runtime_failure", path, "%s: %s" % (sheet, detail), "xlsx", entry, "partial" if truncated else None)
            item["table_id"] = table_id
        if missing_cache:
            coordinates = [item["cell"] for item in missing_cache_formulas]
            item = self.issue(
                "cannot_determine", path,
                "%s: %d formula cells have no cached value at %s; formulas were not recalculated" % (sheet, missing_cache, coordinates),
                "xlsx_cache", entry, "partial",
            )
            item["table_id"] = table_id
            item["formula_cells"] = missing_cache_formulas

    def read_pdf(self, path, entry):
        if path.stat().st_size > _MAX_PDF_BYTES:
            self.issue("runtime_failure", path, "PDF exceeds the safe parsing limit", "pdf", entry, "partial")
            return
        if self.packet["dependencies"]["fitz"]["available"]:
            try:
                self._pdf_fitz(path, entry)
                return
            except Exception as exc:
                self.issue("runtime_failure", path, "PyMuPDF failed: %s; trying pdftotext" % exc, "pdf_fitz", entry, "partial")
        self._pdf_command(path, entry)

    def _new_document(self, path, total_pages, parser):
        self.document_counter += 1
        document = {
            "id": "d%03d" % self.document_counter, "path": str(path), "total_pages": int(total_pages),
            "pages": [], "metadata": {"parser": parser}, "status": "read", "issues": [],
        }
        self.packet["documents"].append(document)
        return document

    def _write_page(self, document, page_number, text):
        target = self.work_root / "pdf_text" / ("%s-p%03d.txt" % (document["id"], page_number))
        if _within(target.resolve(), self.input_root):
            document["issues"].append({"page": page_number, "kind": "evidence_write_blocked", "detail": "work_dir is inside input_dir"})
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            return str(target)
        except OSError as exc:
            document["issues"].append({"page": page_number, "kind": "evidence_write_failed", "detail": str(exc)})
            return None

    def _pdf_page(self, document, page_number, text, bbox=None):
        page = {"page": int(page_number), "text": text}
        if bbox is not None:
            page["bbox"] = _plain(bbox)
        evidence = self._write_page(document, page_number, text)
        if evidence:
            page["evidence_path"] = evidence
        if len(text.strip()) < 40:
            page["ocr_needed"] = True
            page["issues"] = ["ocr_needed"]
            document["issues"].append({"page": int(page_number), "kind": "ocr_needed", "detail": "Empty or low-text page; OCR was not assumed"})
            document["status"] = "partial"
        document["pages"].append(page)

    def _pdf_fitz(self, path, entry):
        fitz = importlib.import_module("fitz")
        entry["parser"] = "PyMuPDF %s" % (getattr(fitz, "__version__", None) or getattr(fitz, "VersionBind", "unknown"))
        handle = fitz.open(str(path))
        try:
            if getattr(handle, "needs_pass", False):
                self.issue("runtime_failure", path, "Encrypted PDF requires a password", "pdf", entry, "partial")
                return
            document = self._new_document(path, handle.page_count, entry["parser"])
            raw_metadata = dict(handle.metadata or {})
            document["metadata"].update({str(key): self.scalar(value) for key, value in raw_metadata.items()})
            chars = 0
            for index in range(handle.page_count):
                if index >= _MAX_PDF_PAGES or chars > _MAX_PDF_CHARS:
                    document["status"] = "partial"
                    document["issues"].append({"kind": "truncated", "detail": "PDF page/text safety limit reached"})
                    entry["status"] = "partial"
                    break
                page = handle.load_page(index)
                text = page.get_text("text", sort=False)
                chars += len(text)
                rect = page.rect
                self._pdf_page(document, index + 1, text, [rect.x0, rect.y0, rect.x1, rect.y1])
            ocr_pages = [item["page"] for item in document["issues"] if item.get("kind") == "ocr_needed"]
            if ocr_pages:
                entry["status"] = "partial"
                self.issue("cannot_determine", path, "OCR needed for low-text pages: %s" % ocr_pages, "pdf_ocr", entry, "partial")
        finally:
            handle.close()

    def _pdf_command(self, path, entry):
        command = self.packet["dependencies"]["pdftotext"]["path"]
        if not command:
            self.issue("runtime_failure", path, "Neither PyMuPDF nor pdftotext is available", "pdf", entry, "partial")
            return
        directory = self.work_root / "pdf_text"
        if _within(directory.resolve(), self.input_root):
            self.issue("runtime_failure", path, "Refusing to write PDF evidence inside input_dir", "pdf", entry, "partial")
            return
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / (".pdftotext-%d.tmp" % os.getpid())
            entry["parser"] = "pdftotext (version not queried)"
            completed = subprocess.run(
                [command, "-layout", "-enc", "UTF-8", str(path), str(temporary)],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=20, check=False,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError("pdftotext exit %d: %s" % (completed.returncode, detail))
            if temporary.stat().st_size > _MAX_PDF_CHARS * 4:
                raise RuntimeError("pdftotext output exceeds the safe text limit")
            text = temporary.read_text(encoding="utf-8", errors="strict")
            chunks = text.split("\f")
            if chunks and chunks[-1] == "":
                chunks.pop()
            document = self._new_document(path, len(chunks), entry["parser"])
            document["metadata"]["title"] = None
            chars = 0
            for index, chunk in enumerate(chunks):
                if index >= _MAX_PDF_PAGES or chars > _MAX_PDF_CHARS:
                    document["status"] = "partial"
                    document["issues"].append({"kind": "truncated", "detail": "PDF page/text safety limit reached"})
                    entry["status"] = "partial"
                    break
                chars += len(chunk)
                self._pdf_page(document, index + 1, chunk)
            ocr_pages = [item["page"] for item in document["issues"] if item.get("kind") == "ocr_needed"]
            if ocr_pages:
                self.issue("cannot_determine", path, "OCR needed for low-text pages: %s" % ocr_pages, "pdf_ocr", entry, "partial")
        except (OSError, UnicodeError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.issue("runtime_failure", path, str(exc), "pdftotext", entry, "partial")
        finally:
            try:
                if 'temporary' in locals() and temporary.exists():
                    temporary.unlink()
            except OSError:
                pass

    def _array(self, path, variable, shape, dtype, numeric, table_id=None, **extra):
        self.array_counter += 1
        item = {
            "id": "a%03d" % self.array_counter, "path": str(path), "name": str(variable), "variable": str(variable),
            "shape": [int(value) for value in shape], "dtype": str(dtype), "mat_index_base": 1,
            "numeric": bool(numeric), "table_id": table_id,
        }
        item.update(_plain(extra))
        self.packet["arrays"].append(item)
        return item

    def read_mat(self, path, entry):
        try:
            with path.open("rb") as handle:
                prefix = handle.read(1024)
            if b"MATLAB 7.3" in prefix:
                self._mat_hdf5(path, entry)
            else:
                self._mat_legacy(path, entry)
        except Exception as exc:
            self.issue("runtime_failure", path, "%s: %s" % (type(exc).__name__, exc), "mat", entry, "partial" if self.packet["arrays"] else "failed")

    def _mat_legacy(self, path, entry):
        if not self.packet["dependencies"]["scipy"]["available"]:
            self.issue("runtime_failure", path, "scipy is unavailable; legacy MAT file not parsed", "mat_legacy", entry, "partial")
            return
        if path.stat().st_size > _MAX_MAT_BYTES:
            self.issue("runtime_failure", path, "MAT file exceeds the safe parsing limit", "mat_legacy", entry, "partial")
            return
        scipy_io = importlib.import_module("scipy.io")
        numpy = importlib.import_module("numpy")
        entry["parser"] = "scipy.io %s" % getattr(importlib.import_module("scipy"), "__version__", "unknown")
        inventory = list(scipy_io.whosmat(str(path)))
        total_cells = sum(int(math.prod(item[1])) for item in inventory)
        loaded = {}
        if total_cells <= _MAX_CELLS * 3:
            raw = scipy_io.loadmat(str(path), squeeze_me=False, struct_as_record=False)
            loaded.update({key: value for key, value in raw.items() if not key.startswith("__")})
            self.packet["mat_header"] = self.scalar(raw.get("__header__", b""))
        for name, shape, mat_class in inventory:
            shape = tuple(int(value) for value in shape)
            count = int(math.prod(shape)) if shape else 1
            numeric_class = mat_class in {"double", "single", "logical", "int8", "uint8", "int16", "uint16", "int32", "uint32", "int64", "uint64"}
            if len(shape) > 2 or count > _MAX_CELLS:
                self._array(path, name, shape, mat_class, numeric_class, status="partial" if count > _MAX_CELLS else "read", source_class=mat_class, summary_only=True, reason="numeric array has more than two dimensions" if len(shape) > 2 else "array exceeds table cell limit")
                if count > _MAX_CELLS:
                    entry["status"] = "partial"
                continue
            if name not in loaded:
                raw = scipy_io.loadmat(str(path), variable_names=[name], squeeze_me=False, struct_as_record=False)
                value = raw.get(name)
            else:
                value = loaded[name]
            if value is None:
                self.issue("runtime_failure", path, "MAT variable was not loaded: %s" % name, "mat_legacy", entry, "partial")
                continue
            array = numpy.asarray(value)
            numeric = bool(numpy.issubdtype(array.dtype, numpy.number) or numpy.issubdtype(array.dtype, numpy.bool_))
            if numeric and array.ndim == 2:
                table_id = self._array_table(path, name, array)
                self._array(path, name, array.shape, array.dtype, True, table_id, source_class=mat_class, original_shape=list(array.shape))
            elif mat_class in {"cell", "struct"}:
                self._expand_legacy_container(numpy, path, entry, name, array, mat_class)
            else:
                summary = self._preview(array)
                self._array(path, name, array.shape, array.dtype, numeric, source_class=mat_class, summary=summary, summary_only=True)

    def _expansion_failure(self, path, entry, state, variable, value, reason, chain, numeric=False, **extra):
        state["failure_count"] += 1
        failure = {"variable": variable, "reason": reason, "mat_position_chain": chain, "mat_index_base": 1}
        failure.update(_plain(extra))
        if len(state["failure_details"]) < 200:
            state["failure_details"].append(failure)
        shape = getattr(value, "shape", ())
        dtype = getattr(value, "dtype", type(value).__name__)
        try:
            shape = tuple(int(item) for item in shape)
        except TypeError:
            shape = ()
        self._array(
            path, variable, shape, dtype, numeric, name=variable,
            source_variable=state["source_variable"], mat_position_chain=chain,
            expansion_status="failed", failure_reason=reason, summary_only=True, **extra
        )
        entry["status"] = "partial"

    def _expand_legacy_container(self, numpy, path, entry, name, array, mat_class):
        state = {
            "source_variable": name, "container_class": mat_class, "numeric_tables": 0,
            "non_numeric_summaries": 0, "container_elements": 0, "numeric_values": 0,
            "failure_count": 0, "failure_details": [],
        }
        parent = self._array(
            path, name, array.shape, array.dtype, False, name=name, source_class=mat_class,
            summary=self._preview(array), summary_only=False, expansion_status="in_progress",
        )
        self._expand_legacy_node(numpy, path, entry, state, array, name, [], 0)
        parent["expansion"] = {
            key: value for key, value in state.items() if key not in {"source_variable", "container_class"}
        }
        parent["expansion"]["status"] = "partial" if state["failure_count"] else "complete"
        parent["expansion_status"] = parent["expansion"]["status"]
        parent["status"] = parent["expansion_status"]
        if state["failure_count"]:
            detail = "%s: %d cell/struct values could not be expanded; first failures: %s" % (
                name, state["failure_count"], state["failure_details"][:20],
            )
            item = self.issue("cannot_determine", path, detail, "mat_expand", entry, "partial")
            item["variable"] = name
            item["failure_count"] = state["failure_count"]
            item["failures"] = state["failure_details"]

    def _expand_legacy_node(self, numpy, path, entry, state, value, variable, chain, depth):
        if depth > 4:
            self._expansion_failure(path, entry, state, variable, value, "nested cell/struct depth exceeds 4", chain)
            return
        array = numpy.asarray(value)
        numeric = bool(numpy.issubdtype(array.dtype, numpy.number) or numpy.issubdtype(array.dtype, numpy.bool_))
        if numeric:
            count = int(math.prod(array.shape)) if array.shape else 1
            if array.ndim != 2:
                self._expansion_failure(path, entry, state, variable, array, "numeric value is not two-dimensional", chain, True)
                return
            if count > _MAX_CELLS or state["numeric_values"] + count > _MAX_MAT_EXPANDED_VALUES:
                self._expansion_failure(path, entry, state, variable, array, "numeric expansion exceeds the per-array or per-container cell limit", chain, True)
                return
            table_id = self._array_table(
                path, variable, array, source_variable=state["source_variable"],
                mat_position_chain=chain, container_class=state["container_class"],
            )
            self._array(
                path, variable, array.shape, array.dtype, True, table_id, name=variable,
                source_variable=state["source_variable"], mat_position_chain=chain,
                original_shape=list(array.shape), expansion_status="expanded_numeric_2d",
            )
            state["numeric_tables"] += 1
            state["numeric_values"] += count
            return
        if hasattr(value, "_fieldnames") or isinstance(value, dict):
            fields = value._fieldnames if hasattr(value, "_fieldnames") else list(value)
            for field in fields:
                child = getattr(value, field, None) if hasattr(value, "_fieldnames") else value[field]
                self._expand_legacy_node(
                    numpy, path, entry, state, child, "%s.%s" % (variable, field),
                    chain + [{"field": str(field)}], depth + 1,
                )
            return
        if isinstance(value, numpy.ndarray) and value.dtype == numpy.dtype("O"):
            count = int(math.prod(value.shape)) if value.shape else 1
            if count and state["container_elements"] + count > _MAX_MAT_EXPANDED_LEAVES:
                self._expansion_failure(path, entry, state, variable, array, "container element budget exhausted; remaining elements not traversed", chain)
                return
            if value.shape == ():
                self._expand_legacy_node(numpy, path, entry, state, value.item(), variable, chain, depth + 1)
                return
            values = value.reshape(-1)
            struct_array = count > 0 and all(hasattr(item, "_fieldnames") or isinstance(item, dict) for item in values)
            for indices in numpy.ndindex(value.shape):
                item = value[indices]
                position = [int(index) + 1 for index in indices]
                if struct_array:
                    child_variable = "%s(%s)" % (variable, ",".join(str(index) for index in position))
                    container = "struct"
                else:
                    child_variable = "%s{%s}" % (variable, ",".join(str(index) for index in position))
                    container = "cell"
                state["container_elements"] += 1
                self._expand_legacy_node(
                    numpy, path, entry, state, item, child_variable,
                    chain + [{"container": container, "indices": position, "index_base": 1}], depth + 1,
                )
            return
        self._array(
            path, variable, array.shape, array.dtype, False, name=variable,
            source_variable=state["source_variable"], mat_position_chain=chain,
            summary=self._preview(value), summary_only=True, expansion_status="summarized_non_numeric",
        )
        state["non_numeric_summaries"] += 1

    def _mat_hdf5(self, path, entry):
        if not self.packet["dependencies"]["h5py"]["available"]:
            self.issue("runtime_failure", path, "h5py is unavailable; MATLAB 7.3 HDF5 file not parsed", "mat_hdf5", entry, "partial")
            return
        h5py = importlib.import_module("h5py")
        numpy = importlib.import_module("numpy")
        entry["parser"] = "h5py %s" % getattr(h5py, "__version__", "unknown")
        with h5py.File(str(path), "r") as handle:
            datasets, struct_groups = [], []
            def collect(name, item):
                if isinstance(item, h5py.Dataset):
                    datasets.append((name, item))
                elif isinstance(item, h5py.Group):
                    matlab_class = item.attrs.get("MATLAB_class", b"")
                    if isinstance(matlab_class, bytes):
                        matlab_class = matlab_class.decode("ascii", errors="strict")
                    if str(matlab_class) == "struct":
                        struct_groups.append((name, item))
            handle.visititems(collect)
            resolved_targets = set()
            for name, dataset in datasets:
                if h5py.check_dtype(ref=dataset.dtype) is h5py.Reference:
                    resolved_targets.update(self._expand_hdf5_reference_array(h5py, numpy, handle, path, entry, name, dataset))
            for name, dataset in datasets:
                shape = tuple(int(value) for value in dataset.shape)
                count = int(math.prod(shape)) if shape else 1
                reference_type = h5py.check_dtype(ref=dataset.dtype)
                is_reference = reference_type is not None
                numeric = not is_reference and bool(numpy.issubdtype(dataset.dtype, numpy.number) or numpy.issubdtype(dataset.dtype, numpy.bool_))
                if name.startswith("#refs#/") and name in resolved_targets:
                    continue
                if is_reference:
                    if reference_type is not h5py.Reference:
                        self._array(path, name, shape, dataset.dtype, False, status="partial", reference_type=str(reference_type), summary_only=True, reason="only direct HDF5 object references are supported")
                        entry["status"] = "partial"
                elif numeric and len(shape) == 2 and count <= _MAX_CELLS:
                    array = dataset[()]
                    table_id = self._array_table(path, name, array)
                    self._array(path, name, shape, dataset.dtype, True, table_id, original_shape=list(shape))
                elif len(shape) > 2:
                    self._array(path, name, shape, dataset.dtype, numeric, summary_only=True, reason="numeric array has more than two dimensions")
                elif count <= 10000 and not numeric:
                    value = dataset[()]
                    self._array(path, name, shape, dataset.dtype, False, summary=self._preview(value), summary_only=True)
                else:
                    self._array(path, name, shape, dataset.dtype, numeric, status="partial", summary_only=True, reason="array exceeds extraction limit")
                    entry["status"] = "partial"
            if struct_groups:
                for name, group in struct_groups:
                    self._array(path, name, (), "HDF5 group", False, name=name, status="partial", fields=list(group.keys()), summary_only=True, reason="HDF5 MATLAB struct groups require layout-specific interpretation and were not expanded")
                item = self.issue("cannot_determine", path, "HDF5 MATLAB struct groups were not expanded: %s" % [name for name, _ in struct_groups], "mat_hdf5_struct", entry, "partial")
                item["groups"] = [name for name, _ in struct_groups]

    def _expand_hdf5_reference_array(self, h5py, numpy, handle, path, entry, name, dataset):
        state = {
            "source_variable": name, "container_class": "hdf5_object_references",
            "numeric_tables": 0, "non_numeric_summaries": 0, "container_elements": 0,
            "numeric_values": 0, "failure_count": 0, "failure_details": [],
        }
        parent = self._array(path, name, dataset.shape, dataset.dtype, False, name=name, reference_type="HDF5 object reference", reference_resolution="one direct dataset hop only", summary_only=False, expansion_status="in_progress")
        count = int(math.prod(dataset.shape)) if dataset.shape else 1
        if count > _MAX_MAT_EXPANDED_LEAVES:
            self._expansion_failure(path, entry, state, name, dataset, "reference container exceeds the element budget", [])
        else:
            references = numpy.asarray(dataset[()], dtype=object)
            for indices in numpy.ndindex(references.shape):
                reference = references[indices] if indices else references.item()
                position = [int(index) + 1 for index in indices] or [1]
                variable = "%s{%s}" % (name, ",".join(str(index) for index in position))
                chain = [{"container": "cell", "indices": position, "index_base": 1}]
                state["container_elements"] += 1
                if not bool(reference):
                    self._expansion_failure(path, entry, state, variable, reference, "null HDF5 object reference", chain, container_shape=list(dataset.shape), container_dtype=str(dataset.dtype))
                    continue
                try:
                    target = handle[reference]
                except Exception as exc:
                    self._expansion_failure(path, entry, state, variable, reference, "object reference could not be resolved: %s" % exc, chain)
                    continue
                target_path = target.name
                if not isinstance(target, h5py.Dataset):
                    self._expansion_failure(path, entry, state, variable, target, "only references directly targeting a dataset are supported", chain, reference_target=target_path)
                    continue
                if h5py.check_dtype(ref=target.dtype) is not None:
                    self._expansion_failure(path, entry, state, variable, target, "reference chains are not followed", chain, reference_target=target_path)
                    continue
                numeric = bool(numpy.issubdtype(target.dtype, numpy.number) or numpy.issubdtype(target.dtype, numpy.bool_))
                count_target = int(math.prod(target.shape)) if target.shape else 1
                if not numeric:
                    self._expansion_failure(path, entry, state, variable, target, "reference target is not numeric", chain, reference_target=target_path)
                    continue
                if len(target.shape) != 2:
                    self._expansion_failure(path, entry, state, variable, target, "reference target is not two-dimensional", chain, True, reference_target=target_path)
                    continue
                if count_target > _MAX_CELLS or state["numeric_values"] + count_target > _MAX_MAT_EXPANDED_VALUES:
                    self._expansion_failure(path, entry, state, variable, target, "reference target exceeds the numeric expansion budget", chain, True, reference_target=target_path)
                    continue
                array = target[()]
                table_id = self._array_table(path, variable, array, source_variable=name, reference_target=target_path, mat_position_chain=chain, container_class="hdf5_object_references")
                self._array(path, variable, target.shape, target.dtype, True, table_id, name=variable, source_variable=name, reference_target=target_path, original_shape=list(target.shape), mat_position_chain=chain, expansion_status="expanded_numeric_2d")
                resolved_target = target_path.lstrip("/")
                state.setdefault("resolved_targets", set()).add(resolved_target)
                state["numeric_tables"] += 1
                state["numeric_values"] += count_target
        resolved_targets = state.pop("resolved_targets", set())
        parent["expansion"] = {key: value for key, value in state.items() if key not in {"source_variable", "container_class"}}
        parent["expansion"]["status"] = "partial" if state["failure_count"] else "complete"
        parent["expansion_status"] = parent["expansion"]["status"]
        parent["status"] = parent["expansion_status"]
        if state["failure_count"]:
            item = self.issue("cannot_determine", path, "%s: %d HDF5 reference values could not be expanded; first failures: %s" % (name, state["failure_count"], state["failure_details"][:20]), "mat_hdf5_expand", entry, "partial")
            item["variable"] = name
            item["failure_count"] = state["failure_count"]
            item["failures"] = state["failure_details"]
        return resolved_targets

    def _array_table(self, path, variable, array, **provenance):
        shape = [int(value) for value in array.shape]
        columns = ["column_%d" % (index + 1) for index in range(shape[1])]
        values = array.tolist()
        rows = [value if isinstance(value, list) else [value] for value in values]
        refs = ["%s::%s[%d,1:%d]" % (path, variable, index + 1, shape[1]) for index in range(shape[0])]
        headers = _header_mapping(columns)
        headers["row"] = None
        metadata = {"variable": variable, "original_shape": shape, "orientation": "stored axes preserved; no transpose", "mat_index_base": 1}
        metadata.update(provenance)
        return self.add_table(path, None, "%s::%s" % (Path(path).name, variable), columns, rows, refs, headers, metadata)

    def _preview(self, value, depth=0):
        if depth > 3:
            return "<maximum preview depth reached>"
        if hasattr(value, "dtype") and hasattr(value, "shape") and hasattr(value, "tolist"):
            count = int(math.prod(value.shape)) if value.shape else 1
            preview = {"kind": "array", "shape": [int(item) for item in value.shape], "dtype": str(value.dtype), "item_count": count}
            if count <= _MAX_PREVIEW_ITEMS:
                preview["values"] = _plain(value.tolist())
            else:
                try:
                    flat = value.reshape(-1)
                    preview["first_values"] = _plain(flat[:_MAX_PREVIEW_ITEMS].tolist())
                    preview["preview_truncated"] = True
                except Exception:
                    preview["preview_truncated"] = True
            return preview
        if hasattr(value, "_fieldnames"):
            fields = [str(item) for item in value._fieldnames]
            preview = {"kind": "matlab_struct", "fields": fields}
            if len(fields) <= _MAX_PREVIEW_ITEMS:
                preview["values"] = {field: self._preview(getattr(value, field, None), depth + 1) for field in fields}
            return preview
        if isinstance(value, dict):
            return {str(key): self._preview(item, depth + 1) for key, item in list(value.items())[:_MAX_PREVIEW_ITEMS]}
        if isinstance(value, (list, tuple)):
            return [self._preview(item, depth + 1) for item in value[:_MAX_PREVIEW_ITEMS]]
        if _is_scalar(value) or isinstance(value, bytes):
            return self.scalar(value)
        return str(value)

    def read_text(self, path, entry):
        entry["parser"] = "stdlib.text"
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                raw = handle.read(_MAX_TEXT_BYTES + 1)
            truncated = len(raw) > _MAX_TEXT_BYTES
            if truncated:
                raw = raw[:_MAX_TEXT_BYTES]
            text, encoding = _decode(raw)
            lines = [{"line": index + 1, "text": line} for index, line in enumerate(text.splitlines())]
            item = {
                "path": str(path), "name": path.name, "sha256": entry["sha256"], "text": text,
                "lines": lines, "metadata": {"encoding": encoding, "truncated": truncated},
            }
            self.packet["other_files"].append(item)
            if truncated:
                self.issue("runtime_failure", path, "Text content truncated at the safe byte limit", "text", entry, "partial")
        except (OSError, UnicodeError) as exc:
            self.issue("runtime_failure", path, str(exc), "text", entry, "failed")

    def read_matlab_mapping(self, path, entry):
        other = next((item for item in reversed(self.packet["other_files"]) if item["path"] == str(path)), None)
        if other is None:
            return
        try:
            module_name = (__package__ + ".matlab_mapping") if __package__ else "matlab_mapping"
            parser = importlib.import_module(module_name)
            result = parser.extract_source_curve_mappings(other["text"], str(path))
            ids = []
            for mapping in result["mappings"]:
                mapping["id"] = "m%03d" % (len(self.packet["source_curve_mappings"]) + 1)
                self.packet["source_curve_mappings"].append(mapping)
                ids.append(mapping["id"])
            other["metadata"]["source_curve_mapping_ids"] = ids
            other["metadata"]["source_curve_mapping_issue_count"] = len(result["issues"])
            for detail in result["issues"]:
                item = self.issue("cannot_determine", path, detail["detail"], "matlab_mapping", entry)
                item["line"] = int(detail["line"])
        except Exception as exc:
            self.issue("runtime_failure", path, "MATLAB constant mapping extraction failed: %s" % exc, "matlab_mapping", entry, "partial")

    def read_other(self, path, entry):
        try:
            with path.open("rb") as handle:
                prefix = handle.read(65536)
            if b"\x00" not in prefix:
                try:
                    _decode(prefix)
                except UnicodeError:
                    return
                self.read_text(path, entry)
        except OSError as exc:
            self.issue("runtime_failure", path, str(exc), "other", entry, "failed")

    def run(self):
        self.read_case()
        files = self.discover()
        if self.case_data is not None:
            try:
                self.extract_json_tables(self.case_data, self.case_path, self.case_path.name, source="case_json")
            except (ValueError, TypeError, RecursionError) as exc:
                self.issue("runtime_failure", self.case_path, str(exc), "case_json_tables")
        for path in files:
            entry = self.input_entry(path)
            if entry is None:
                continue
            try:
                self.read_file(path, entry)
                if path.suffix.lower() == ".m":
                    self.read_matlab_mapping(path, entry)
            except MemoryError:
                self.issue("runtime_failure", path, "Memory limit reached; file extraction stopped", "read", entry, "partial")
            except Exception as exc:
                self.issue("runtime_failure", path, "%s: %s" % (type(exc).__name__, exc), "read", entry, "partial")
        if self.missing_truncated:
            self.packet["missing_truncated"] = self.missing_truncated
        return _plain(self.packet)


def discover_and_read(case_path: Path, task_path: Path, input_dir: Path, work_dir: Path):
    return _Reader(case_path, task_path, input_dir, work_dir).run()
