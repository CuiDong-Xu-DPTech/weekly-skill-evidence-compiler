"""Deterministic, compact partial reasoning seeds from an existing analysis."""
from __future__ import annotations

import copy
import re


FIT_OPERATIONS = {"fit_declared_model", "binding_model", "nonlinear_declared_model"}
NESTED_OPERATION = "group_summary_nested_treatment_contrast"
FIT_VALUE_KEYS = (
    "Kd_est", "standard_error", "interval_low", "interval_high", "interval_coverage",
    "local_interval_estimable", "ci_status", "ci_unavailable_reason", "converged",
    "n", "distinct_T0", "total_rows", "excluded_rows", "exclusion_counts", "SSE", "SST",
    "descriptive_1_minus_SSE_over_SST", "parameter_at_bound", "error_in_R_used_as_weight",
    "outlier_filter_applied", "free_parameter_count", "local_covariance_residual_dof", "dof_basis",
)


def _copy(value):
    return copy.deepcopy(value)


def _maps(items):
    return [item for item in items or [] if isinstance(item, dict)]


def _pick(source, keys):
    return {key: _copy(source[key]) for key in keys if key in source}


def _without(source, excluded):
    return {key: _copy(value) for key, value in source.items() if key not in excluded}


def _identity(value):
    if not isinstance(value, dict):
        return {}
    return _pick(value, (
        "table_id", "path", "sheet", "name", "group_label", "L0", "L0_header",
        "T0_header", "R_header", "error_in_R_header", "pair_unit_columns",
        "experimental_unit_columns", "dataset", "estimator", "fold", "run_state",
    ))


def _without_rows(value):
    if not isinstance(value, dict):
        return _copy(value)
    return _without(value, {
        "raw_records", "source_records", "optimizer_attempts", "excluded_row_details",
        "pair_treatment_count", "rows_ordered_as_source", "residual_sign_runs_by_increasing_T0",
    })


class _Seed:
    def __init__(self, case, analysis):
        self.case = case if isinstance(case, dict) else {}
        self.analysis = analysis if isinstance(analysis, dict) else {}
        self.facts = _maps(self.analysis.get("facts"))
        self.anomaly_records = _maps(self.analysis.get("source_anomalies"))
        self.rules = _maps(self.analysis.get("declared_rule_results"))
        self.fact_by_id = {item["id"]: item for item in self.facts if isinstance(item.get("id"), str)}
        self.known_ids = set(self.fact_by_id)
        self.known_ids.update(item["id"] for item in self.anomaly_records if isinstance(item.get("id"), str))
        self.trends = []
        self.anomalies = []
        self.rechecks = []
        self.followups = []
        self.uncertainties = []
        self.commands = []
        self.rule_coverage = []
        self.contexts = set()
        self.has_specialized = False
        self.table_facts = [item for item in self.facts if item.get("metric") == "table_shape"]

    def refs(self, *values):
        result = []
        for value in values:
            candidates = value if isinstance(value, list) else [value]
            for item in candidates:
                if isinstance(item, dict):
                    item = item.get("id") or (item.get("evidence") or [None])[0]
                if not isinstance(item, str) or not item or item in result:
                    continue
                if item in self.known_ids or not re.fullmatch(r"F\d{2,6}", item):
                    result.append(item)
        return result or ["work/analysis.json"]

    def fact_refs(self, facts):
        return self.refs([item for item in facts if isinstance(item.get("id"), str)])

    def finding(self, statement, evidence, **fields):
        item = {"statement": statement, "evidence": self.refs(evidence)}
        item.update(fields)
        return item

    def first_ref(self, facts=None):
        if facts:
            refs = self.fact_refs(facts)
            if refs != ["work/analysis.json"]:
                return refs[0]
        if self.anomaly_records:
            return self.refs(self.anomaly_records[0])[0]
        return self.refs(self.facts)[0]

    def configs(self, operation):
        return [_copy(item) for item in _maps(self.case.get("analysis_rules_v1"))
                if item.get("operation") == operation]

    def add_uncertainty(self, statement, evidence, **fields):
        item = {"statement": statement, "evidence": self.refs(evidence)}
        item.update(fields)
        self.uncertainties.append(item)

    def mapping_for_rule(self, rule):
        selections = rule.get("selections", {})
        if not isinstance(selections, dict):
            return {}
        return {
            "operation": _copy(selections.get("operation")),
            "resolved_tables": [
                _pick(table, ("table_id", "path", "sheet", "header_row", "columns"))
                for table in _maps(selections.get("resolved_tables"))
            ],
        }

    def compact_rule(self, rule):
        compact = _without(rule, {"selections", "pair_treatment_count", "tables"})
        compact["tables"] = [_without_rows(table) for table in _maps(rule.get("tables"))]
        mapping = self.mapping_for_rule(rule)
        if mapping:
            compact["raw_input_mapping"] = mapping
        return compact

    def result_facts(self, result):
        return [self.fact_by_id[item] for item in result.get("fact_ids", []) if item in self.fact_by_id]

    def add_rule_recheck(self, result, cases):
        operation = result.get("operation", "unknown_declared_operation")
        facts = self.result_facts(result)
        anomaly_ids = [item.get("id") for item in self.anomaly_records
                       if item.get("declared_rule") == operation and item.get("id")]
        recheck = self.finding(
            "Recheck the executed declared %s calculation against its original configuration and every listed case." % operation,
            [item.get("id") for item in facts[:1]] + anomaly_ids or self.first_ref(),
            kind="executed_formula_configuration_and_case_coverage",
            operation=operation,
            status=_copy(result.get("status")),
            declared_configuration=self.configs(operation),
            cases=cases,
            complete_fact_records={"count": len(result.get("fact_ids", [])),
                                   "pointer": "work/analysis.json#/facts"},
            optional_reproduction="Use the existing pipeline's declared-rule execution; do not substitute an unconfigured calculator or interpret this recheck as a new experiment.",
        )
        self.rechecks.append(recheck)
        self.rule_coverage.append({"operation": operation, "status": _copy(result.get("status")),
                                   "coverage": _copy(cases), "fact_count": len(result.get("fact_ids", [])),
                                   "fact_id_sample": [item.get("id") for item in facts[:2]],
                                   "full_records": "work/analysis.json#/declared_rule_results"})
        self.has_specialized = True
        self.contexts.add(operation)

    def add_fit(self):
        for result in self.rules:
            if result.get("operation") not in FIT_OPERATIONS:
                continue
            coverage = result.get("coverage", {})
            groups = _maps(coverage.get("groups"))
            fit_facts = [item for item in self.facts
                         if item.get("declared_rule") in FIT_OPERATIONS and
                         (str(item.get("metric", "")).endswith("_fit") or
                          (isinstance(item.get("value"), dict) and "Kd_est" in item["value"]))]
            random_facts = [item for item in result_facts_for_fit(self.facts, result)]
            used = set()
            for index, group in enumerate(groups):
                match = self.match_group(fit_facts, group, used)
                if match is not None:
                    used.add(id(match))
                refs = [match.get("id")] if match else []
                matched_random = [item for item in random_facts if self.same_group(item.get("entity"), group)]
                refs.extend(item.get("id") for item in matched_random)
                value = _pick(match.get("value", {}), FIT_VALUE_KEYS) if match else None
                finding = self.finding(
                    "Declared-equation fit for %s; this is a group result, not a causal trend." % group.get("group_label", "unlabeled group"),
                    refs or self.first_ref(),
                    finding_type="declared_group_fit",
                    group=_copy(group),
                    fit_result=_copy(value),
                    fit_fact_id=match.get("id") if match else None,
                    unit=_copy(match.get("unit")) if match else _copy(group.get("source_units", {}).get("T0_and_Kd")),
                    equation_and_local_uncertainty_method=_copy(match.get("method")) if match else None,
                    local_uncertainty=_copy(match.get("uncertainty")) if match else "Fit unavailable; no value or interval inferred.",
                    source_random_error_fractions=[
                        {"fact_id": item.get("id"), "value": _copy(item.get("value")),
                         "unit": _copy(item.get("unit")), "uncertainty": _copy(item.get("uncertainty"))}
                        for item in matched_random
                    ],
                    interval_scope="Local fit uncertainty only; source random-error fractions do not replace ACI/PCI or bootstrap uncertainty.",
                    full_records="work/analysis.json#/facts",
                )
                domain_fact = next((item for item in self.facts if item.get("declared_rule") in FIT_OPERATIONS and
                                    str(item.get("metric", "")).endswith("_residuals") and
                                    self.same_group(item.get("entity"), group) and
                                    isinstance(item.get("value"), dict) and item["value"].get("response_domain")), None)
                if domain_fact is not None:
                    domain = domain_fact["value"]["response_domain"]
                    finding["response_domain"] = _copy(domain)
                    if domain.get("count_R_below_required_lower_bound"):
                        self.anomalies.append(self.finding(
                            "%s: observed R is below the feasible lower bound under the declared model and source L0; this is a model/data mismatch needing review, not proof of a bad sample. All observations are retained." % group.get("group_label", "unlabeled group"),
                            [domain_fact["id"]] + [item["R_ref"] for item in domain["violations"]],
                            kind="model_feasible_range_mismatch", group=_copy(group),
                            observed=_copy(domain["violations"]), reference=_copy(domain["lower_bound"]),
                            deviation=_copy(domain["largest_deficit"]), unit=_copy(domain["R_unit"]),
                            threshold_basis=_copy(domain["criterion"]), status=_copy(domain["cause_status"]),
                            inclusion="retained in the primary fit; no clipping or deletion"))
                self.trends.append(finding)
                if match is None:
                    self.anomalies.append(self.finding(
                        "Declared fit group remains unresolved: %s" % group.get("group_label", index),
                        self.first_ref(), kind="unresolved_fit_group", group=_copy(group),
                        status="unresolved_not_zero"))
            unrepresented = [item for item in fit_facts if id(item) not in used]
            for item in unrepresented:
                self.anomalies.append(self.finding(
                    "A fit fact could not be bound to a declared coverage group.",
                    [item.get("id")], kind="unbound_fit_fact", entity=_identity(item.get("entity")),
                    actual_fact_id=item.get("id"), status="unresolved_not_merged_by_guessed_label"))
            cases = _without(coverage, {"groups"})
            cases["groups"] = _copy(groups)
            self.add_rule_recheck(result, cases)
            self.add_uncertainty(
                "Local covariance intervals are not independent systematic ACI/PCI, bootstrap intervals, or proof of a physical mechanism.",
                [item.get("id") for item in fit_facts] or self.first_ref(),
                fit_status=_pick(coverage, ("ACI_computed", "bootstrap_computed", "limitations")))
            self.followups.append({
                "statement": "If systematic ACI/PCI inference is required, first obtain the source-defined independent units, systematic-error model and required confidence inputs; the local fit alone does not falsify a root cause.",
                "execution": "proposed_not_executed", "evidence_gap": _copy(coverage.get("limitations", [])),
                "evidence": self.refs([item.get("id") for item in fit_facts] or self.first_ref()),
            })

    @staticmethod
    def same_group(entity, group):
        if not isinstance(entity, dict):
            return False
        for key in ("group_label", "table_id", "sheet"):
            if group.get(key) is not None and entity.get(key) != group.get(key):
                return False
        return any(group.get(key) is not None for key in ("group_label", "table_id", "sheet"))

    def match_group(self, fit_facts, group, used):
        for item in fit_facts:
            if id(item) not in used and self.same_group(item.get("entity"), group):
                return item
        return None

    def add_paired_and_ratios(self):
        for result in self.rules:
            operation = result.get("operation")
            if operation not in ("paired_replicate_range", "derived_ratio_audit"):
                continue
            cases = [self.compact_rule(rule) for rule in _maps(result.get("coverage", {}).get("rules"))]
            self.add_rule_recheck(result, cases)
            facts = self.result_facts(result)
            self.trends.append(self.finding(
                "Executed %s coverage is reported without inventing a temporal or causal trend." % operation,
                [item.get("id") for item in facts[:2]] or self.first_ref(),
                finding_type="observed_operation_coverage_not_time_trend", operation=operation,
                coverage=_copy(cases)))
            for item in facts:
                if operation == "paired_replicate_range" and item.get("kind") == "unresolved":
                    self.anomalies.append(self.finding(
                        "Incomplete, extra or non-comparable declared replicate group remains unresolved.",
                        [item.get("id")], kind="unresolved_declared_pair",
                        entity=_copy(item.get("entity")), value=_without_rows(item.get("value")),
                        unit=_copy(item.get("unit")), status="unresolved_not_averaged"))
            if operation == "derived_ratio_audit":
                for case in cases:
                    for table in case.get("tables", []):
                        undefined = table.get("undefined_zero_denominator", 0)
                        if undefined:
                            unresolved = [item for item in facts if item.get("kind") == "unresolved"]
                            sample = [item.get("id") for item in unresolved[:2]]
                            self.anomalies.append(self.finding(
                                "Zero denominators leave ratios undefined; no zero or one is imputed.",
                                sample or self.first_ref(), kind="zero_denominator_ratio_coverage",
                                table=_identity(table), undefined_zero_denominator=undefined,
                                unresolved_fact_count=len(unresolved), unresolved_fact_id_sample=sample,
                                full_unresolved_records="work/analysis.json#/facts",
                                zero_denominator_policy=self._zero_denominator_policy(),
                                raw_recompute_records="work/analysis.json#/facts"))

    def _zero_denominator_policy(self):
        for rule in self.configs("derived_ratio_audit"):
            if "zero_denominator_policy" in rule:
                return _copy(rule.get("zero_denominator_policy"))
        return "undefined_missing_not_zero"

    def add_declared_qc(self):
        for item in self.anomaly_records:
            value = item.get("value")
            if not isinstance(value, dict) or value.get("status") != "quality_violation":
                continue
            self.anomalies.append(self.finding(
                "Source-declared QC threshold violation; retained as source evidence, not an exploratory outlier or physical cause.",
                [item.get("id")] + item.get("evidence", []), kind="source_declared_quality_violation",
                qc_class="declared_source_QC_not_derived_screen", source_anomaly_id=item.get("id"),
                declared_rule=_copy(item.get("declared_rule")), entity=_copy(item.get("entity")),
                metric=_copy(item.get("metric")), unit=_copy(item.get("unit")), value=_without_rows(value),
                method=_copy(item.get("method")), source_evidence=_copy(item.get("evidence", [])),
                uncertainty=_copy(item.get("uncertainty")), status="quality_violation"))
            self.contexts.add("source_declared_QC")
            self.has_specialized = True
        other = [item for item in self.anomaly_records
                 if not (isinstance(item.get("value"), dict) and item["value"].get("status") == "quality_violation")]
        groups = {}
        for item in other:
            key = (str(item.get("kind", "source_issue")), str(item.get("status", "reported")))
            groups.setdefault(key, []).append(item)
        for (kind, status), items in sorted(groups.items()):
            ids = [item.get("id") for item in items if item.get("id")]
            sample = items[0]
            self.anomalies.append(self.finding(
                "Non-QC source issues remain recorded separately from declared threshold events.",
                ids[:1] or sample.get("evidence", []), kind="source_issue_group",
                source_issue_kind=kind, reported_status=status, source_anomaly_count=len(items),
                source_anomaly_ids=ids, sample_issue=_without_rows(sample),
                full_records="work/analysis.json#/source_anomalies"))

    def add_nested(self):
        nested_results = [result for result in self.rules if result.get("operation") == NESTED_OPERATION]
        if not nested_results:
            return
        for result in nested_results:
            cases = [self.compact_rule(rule) for rule in _maps(result.get("coverage", {}).get("rules"))]
            self.add_rule_recheck(result, cases)
        summaries = [item for item in self.facts
                     if str(item.get("metric", "")).startswith("nested_paired_contrast_summary:")]
        for item in summaries:
            paired_metric = str(item.get("metric")).replace("nested_paired_contrast_summary:",
                                                            "nested_paired_left_minus_right:", 1)
            paired = [fact for fact in self.facts if fact.get("metric") == paired_metric]
            inner = [fact for fact in self.facts
                     if str(fact.get("metric", "")).startswith("inner_experimental_unit_") and
                     str(fact.get("metric", "")).endswith(":" + str(item.get("metric")).split(":", 1)[1])]
            self.trends.append(self.finding(
                "Outer paired summary for %s; actual pair and inner-unit definitions are preserved." % str(item.get("metric")).split(":", 1)[1],
                [item.get("id")], finding_type="nested_outer_paired_summary",
                metric=item.get("metric"), summary_fact_id=item.get("id"), entity=_copy(item.get("entity")),
                outer_summary=_copy(item.get("value")), unit=_copy(item.get("unit")),
                method=_copy(item.get("method")),
                actual_matched_pair_fact_count=len(paired),
                actual_inner_unit_fact_count=len(inner),
                complete_paired_and_inner_records={
                    "paired_fact_ids_available": "work/analysis.json#/facts",
                    "paired_fact_count": len(paired), "inner_unit_fact_count": len(inner),
                    "declared_raw_input_mapping": [case.get("raw_input_mapping") for case in self._nested_cases(nested_results)],
                },
                inference_boundary="Outer mean/median/range uses only this metric's complete, structurally compatible pairs; inner rows and technical repeats are not silently promoted to independent outer units."))
        self.followups.append({
            "statement": "For any new causal claim, predeclare an intervention and independent allocation at the source-supported nested hierarchy; no repeat count, material or physical mechanism is inferred from this descriptive seed.",
            "execution": "proposed_not_executed", "evidence": self.refs([item.get("id") for item in summaries]),
            "evidence_gap": "Current outer contrasts do not alone assign a mechanism beyond compatible paired units.",
            "source_hierarchy": self.configs(NESTED_OPERATION),
        })

    @staticmethod
    def _nested_cases(results):
        return [rule for result in results for rule in _maps(result.get("coverage", {}).get("rules"))]

    def add_computational(self):
        computation = self.analysis.get("computational_results", {})
        runs = _maps(computation.get("runs")) if isinstance(computation, dict) else []
        if not runs:
            return
        self.has_specialized = True
        self.contexts.add("shared_split_computation")
        accuracies = [item for item in self.facts if item.get("metric") == "classification_accuracy"]
        signatures = []
        for run_index, run in enumerate(runs):
            folds = _maps(run.get("folds"))
            fold_results = []
            refs = []
            signature = []
            for fold_index, fold in enumerate(folds):
                signature.append((fold.get("train_indices_zero_based"), fold.get("test_indices_zero_based")))
                fact = self.match_accuracy(accuracies, run, fold)
                refs.append(fact.get("id") if fact else self.first_ref())
                pointer = "artifacts/computational_results.json#/runs/%d/folds/%d" % (run_index, fold_index)
                fold_results.append({
                    "fold": _copy(fold.get("fold")), "fact_id": fact.get("id") if fact else None,
                    "value": _copy(fact.get("value")) if fact else _pick(fold, (
                        "n_train", "n_test", "n_scored", "correct_count", "accuracy", "exit", "error")),
                    "n_train": _copy(fold.get("n_train")), "n_test": _copy(fold.get("n_test")),
                    "prediction_record_count": len(_maps(fold.get("samples"))),
                    "full_prediction_and_index_artifact": pointer,
                    "index_fields": ["train_indices_zero_based", "test_indices_zero_based", "samples"],
                    "coverage_status": "complete" if _maps(fold.get("samples")) or fold.get("n_test", 0) == 0 else "unresolved_missing_prediction_records",
                })
            signatures.append(signature)
            run_trend = self.finding(
                "All executed folds for %s are retained with a full prediction/index artifact pointer." % run.get("estimator", run.get("run_id", "unknown estimator")),
                refs, finding_type="shared_split_estimator_folds", estimator=_copy(run.get("estimator")),
                run_id=_copy(run.get("run_id")), fold_results=fold_results,
                prediction_artifact="artifacts/computational_results.json",
                sampling_uncertainty="Cross-validation folds reuse training samples; no pseudo-independent fold CI or t-test is supplied.")
            self.trends.append(run_trend)
            if any(item["fact_id"] is None for item in fold_results):
                self.anomalies.append(self.finding(
                    "An executed fold lacks a matching accuracy fact and remains unresolved.",
                    refs, kind="unresolved_computational_fold_fact", estimator=_copy(run.get("estimator"))))
            self.rechecks.append(self.finding(
                "Reexecute %s only with the retained estimator, splitter, preprocessing and physical split indices." % run.get("estimator", "configured estimator"),
                refs, kind="executed_computational_configuration",
                configuration=_without(run, {"folds", "dataset_provenance"}),
                executed_cases=[{"fold": fold.get("fold"), "n_train": fold.get("n_train"), "n_test": fold.get("n_test")}
                                for fold in folds],
                full_indices_and_predictions="artifacts/computational_results.json"))
        shared = bool(signatures) and all(signature == signatures[0] for signature in signatures[1:])
        for pair in _maps(computation.get("paired_comparison")):
            paired_fact = next((item for item in self.facts
                                if item.get("metric") == "paired_classification_accuracy_comparison" and
                                item.get("entity", {}).get("left_estimator") == pair.get("left_estimator") and
                                item.get("entity", {}).get("right_estimator") == pair.get("right_estimator")), None)
            self.trends.append(self.finding(
                "Same-fold paired deltas: %s minus %s; descriptive only." % (pair.get("left_estimator"), pair.get("right_estimator")),
                [paired_fact.get("id")] if paired_fact else self.first_ref(),
                finding_type="paired_computational_deltas",
                paired_fact_id=paired_fact.get("id") if paired_fact else None,
                folds=_copy(pair.get("folds", [])),
                summary={key: _copy(pair[key]) for key in (
                    "n_paired_folds", "wins", "losses", "ties", "difference_range", "mean_difference",
                    "median_difference", "mean_percentage_point_difference", "paired_samples_n",
                    "prediction_disagreements", "prediction_disagreement_fraction", "left_only_correct",
                    "right_only_correct") if key in pair},
                shared_split_indices=shared,
                method=_copy(pair.get("method")), limitations=_copy(pair.get("limitations", [])),
                full_fold_xy_predictions="artifacts/computational_results.json"))
        for limit in _maps(computation.get("limits")):
            ref = self.first_ref(accuracies)
            if limit.get("state") in ("design_recommendation_not_executed", "not_executed"):
                self.followups.append({"statement": _copy(limit.get("recommendation", limit.get("reason"))),
                                       "execution": "proposed_not_executed", "evidence": self.refs(ref),
                                       "source_limit": _copy(limit)})
            else:
                self.add_uncertainty(_copy(limit.get("reason", limit.get("state", "Computational limitation retained"))),
                                     ref, source_limit=_copy(limit),
                                     shared_split_indices=shared)
        if not shared:
            self.anomalies.append(self.finding(
                "Shared physical split identity is not established; paired deltas remain unresolved rather than independent evidence.",
                self.first_ref(accuracies), kind="unresolved_shared_split_mapping"))
        else:
            failures = [{"run_id": run.get("run_id"), "fold": fold.get("fold"), "error": _copy(fold.get("error"))}
                        for run in runs for fold in _maps(run.get("folds")) if fold.get("error")]
            warning_count = sum(len(fold.get("warnings", [])) for run in runs for fold in _maps(run.get("folds")))
            self.anomalies.append(self.finding(
                "No source-declared anomaly or executed fold failure is observed in this computational packet; cross-validation dependence remains an inference boundary, not an anomaly.",
                self.first_ref(accuracies), kind="computational_anomaly_coverage",
                source_anomaly_count=len(self.anomaly_records), fold_error_count=len(failures),
                fold_warning_count=warning_count, shared_split_indices=shared,
                inference_boundary="Paired folds reuse training data; descriptive deltas are not independent-fold confidence intervals."))

    @staticmethod
    def match_accuracy(facts, run, fold):
        for item in facts:
            entity = item.get("entity", {})
            if (entity.get("estimator") == run.get("estimator") and entity.get("fold") == fold.get("fold") and
                    (not run.get("run_id") or entity.get("run_state") in (None, run.get("execution"), run.get("state")))):
                return item
        return None

    def add_curves(self):
        curve = self.analysis.get("curve_analysis", {})
        coverage = curve.get("coverage", {}) if isinstance(curve, dict) else {}
        groups = _maps(curve.get("groups")) if isinstance(curve, dict) else []
        if not groups or not coverage.get("traces"):
            return
        self.has_specialized = True
        self.contexts.add("native_curve_coverage")
        join_issues = _maps(curve.get("source_join_issues")) or [
            item for item in _maps(self.analysis.get("issues")) if item.get("kind") == "source_metadata_unmatched"]
        for group in groups:
            traces = _maps(group.get("traces"))
            statuses = {}
            for trace in traces:
                status = str(trace.get("source_join_status", "unresolved"))
                statuses[status] = statuses.get(status, 0) + 1
            examples = []
            seen_status = set()
            for trace in traces:
                status = trace.get("source_join_status")
                if status in seen_status:
                    continue
                seen_status.add(status)
                examples.append({
                    "trace_id": trace.get("id"), "source_join_status": status,
                    "source_join_method": _copy(trace.get("source_join_method")),
                    "source_raw_path": _copy(trace.get("entity", {}).get("source_raw_path")),
                    "source_id_candidates": _copy(trace.get("source_id_candidates", [])),
                    "metadata": _copy(trace.get("metadata")),
                    "metadata_provenance": _copy(trace.get("metadata_provenance")),
                    "representation_id": _copy(trace.get("representation_id")),
                    "independent_unit_id": _copy(trace.get("independent_unit_id")),
                    "independent_unit_basis": _copy(trace.get("independent_unit_basis")),
                })
            responses = []
            keys = set()
            for trace in traces:
                item = _pick(trace, ("variable", "x_label", "y_label", "x_unit", "y_unit", "x_mode", "y_unit_provenance"))
                item["source_join_status"] = _copy(trace.get("source_join_status"))
                item["response_unit_status"] = ("source_bound" if trace.get("y_unit") is not None
                                                 else "unresolved_missing_source_response_unit")
                item["abscissa"] = _copy(trace.get("abscissa"))
                key = repr(sorted(item))
                if key not in keys:
                    keys.add(key)
                    responses.append(item)
            related_facts = [item for item in self.table_facts
                             if item.get("entity", {}).get("table_id") in set(group.get("source_tables", []))]
            join_ids = [item.get("id") for item in join_issues if item.get("id")]
            self.trends.append(self.finding(
                "Native curve group %s is summarized by coverage and operational trace identity, not by invented strength effects." % group.get("id"),
                [item.get("id") for item in related_facts[:1]] + join_ids,
                finding_type="native_curve_group_coverage", group_id=group.get("id"),
                metric_key=_copy(group.get("metric_key")), n_traces=group.get("n_traces", len(traces)),
                representation_count=group.get("representation_count"), independent_unit_count=group.get("independent_unit_count"),
                comparison_context=_copy(group.get("comparison_context")), technical_contexts=_copy(group.get("technical_contexts")),
                source_trace_table_count=len(group.get("source_tables", [])),
                source_file_count=len(group.get("source_files", [])),
                mapped_and_unmatched=statuses,
                native_curve_csv={"path": "analysis_curve_traces.csv", "expected_rows": coverage.get("csv_trace_rows"),
                                  "actual_trace_count": coverage.get("traces"),
                                  "complete_export": coverage.get("csv_trace_rows") == coverage.get("traces"),
                                  "row_policy": "Use csv.DictReader through EOF, not a preview."},
                identity_mapping_examples=examples,
                operational_responses=responses,
                candidate_status="Exploratory shape/depth candidates only; not all candidates are high-confidence findings.",
                strength_effect="not_estimated; operational responses and unconfirmed abscissae are not physical bending-strength or geometric evidence",
                full_trace_records="work/analysis.json#/curve_analysis/groups"))
            if any(status != "matched" for status in statuses):
                self.anomalies.append(self.finding(
                    "Curve group %s has unmatched/ambiguous source identities; these are unresolved, not high-confidence physical trends." % group.get("id"),
                    join_ids or [item.get("id") for item in related_facts[:1]], kind="unresolved_curve_source_mapping",
                    group_id=group.get("id"), mapped_and_unmatched=statuses,
                    source_join_issues=_copy(join_issues),
                    full_trace_csv="analysis_curve_traces.csv"))
        self.rechecks.append(self.finding(
            "Read every native curve CSV record through EOF, cross-check total/group coverage and reverse-check source identities before interpreting exploratory curve candidates.",
            [item.get("id") for item in join_issues if item.get("id")] or self.first_ref(self.table_facts),
            kind="native_curve_full_export_and_identity_recheck",
            coverage=_pick(coverage, ("groups", "traces", "mapped_traces", "unmatched_traces", "ambiguous_traces", "csv_trace_rows", "source_join_issue_count")),
            artifact="analysis_curve_traces.csv", expected_all_groups=_copy([group.get("id") for group in groups])))
        self.followups.append({
            "statement": "Resolve source-to-trace identity, operational response meaning and physical abscissa metadata before proposing a mechanism or a strength-effect experiment; do not convert arbitrary index/response units into geometric quantities.",
            "execution": "proposed_not_executed", "evidence": self.refs([item.get("id") for item in join_issues if item.get("id")] or self.first_ref(self.table_facts)),
            "evidence_gap": _copy(join_issues)
        })

    def add_fallback(self):
        if self.has_specialized or self.trends:
            return
        self.contexts.add("No_data_allowed")
        table_refs = [item.get("id") for item in self.table_facts]
        tables = [_identity(item) | {"shape": _copy(item.get("shape"))} for item in _maps(self.analysis.get("table_summaries"))]
        self.trends.append(self.finding(
            "No_data_allowed: no source-bound quantitative trend or geometric interpretation is assigned; only the parsed inventory is available pending semantic mapping.",
            table_refs, finding_type="semantic_inventory_not_physical_trend",
            tables=tables, status="unknown_pending_semantic_mapping"))
        self.rechecks.append(self.finding(
            "Map each listed sheet and raw column to source-declarative method metadata before choosing any formula; no OLS, calibration or LOD/LOQ calculation is assumed from sheet names.",
            table_refs, kind="semantic_mapping_boundary_recheck",
            cases=tables, executed_formula_configuration=[],
            boundary="No executable source formula/configuration is known in this seed; descriptive columns are not evidence of a fitted method."))
        self.rule_coverage.append({"operation": "No_data_allowed", "status": "unknown",
                                   "coverage": {"tables": tables}, "full_records": "work/analysis.json"})
        self.followups.append({
            "statement": "Obtain and approve a sheet/column-to-method and unit map, including declared exclusion and acceptance rules, before proposing an analytical experiment; OLS and LOD/LOQ inputs remain unprovided unless the sources explicitly define them.",
            "execution": "proposed_not_executed", "evidence": self.refs(table_refs),
            "evidence_gap": "Paper/table presence alone does not supply necessary method semantics or acceptance metadata.",
            "source_tables": tables,
        })

    def add_causes(self):
        quality = [item for item in self.anomaly_records
                   if isinstance(item.get("value"), dict) and item["value"].get("status") == "quality_violation"]
        if "No_data_allowed" in self.contexts:
            candidate, evidence = "unknown", [item.get("id") for item in self.table_facts]
            action = "Approve source-backed semantics for every listed sheet/column and unit; until then no physical cause, geometric interpretation, OLS fit or LOD/LOQ calculation is admissible."
            status = "No_data_allowed_candidate_unknown_pending_semantic_review"
        else:
            candidate = "cause_undetermined"
            evidence = ([item.get("id") for item in quality] or
                        [item.get("id") for item in self.result_facts(self.rules[0])[:1]] if self.rules else [self.first_ref()])
            checks = []
            for context, check in (
                ("source_declared_QC", "Compare each quality_violation entity/value/raw locator with the declared QC rule and its incomplete-policy; a QC threshold does not select an operator, replacement, contamination or other physical cause."),
                ("paired_replicate_range", "Re-read the actual paired entity and source replicate records; numerical refinement cannot by itself falsify a physical mechanism."),
                ("derived_ratio_audit", "Inspect the actual raw numerator/denominator cells for undefined denominators; do not repair them to zero."),
                ("shared_split_computation", "Compare same-fold and same-sample prediction records, estimator configurations and shared physical split indices; this distinguishes computational behavior from sampling inference and does not create an experimental mechanism."),
                ("native_curve_coverage", "Resolve source identity and operational response metadata; shape candidates alone do not distinguish material, geometric or human causes."),
                (NESTED_OPERATION, "Check declared inner raw measurements and compatible outer pair identities before comparing any mechanistic alternative."),
            ):
                if context in self.contexts:
                    checks.append(check)
            if any(context in FIT_OPERATIONS for context in self.contexts):
                checks.append("Compare each declared-equation group against its source units, raw evidence and independent error model; refined Kd/local SE alone does not distinguish root causes.")
            action = " ".join(checks) or "Bind the current observation to source-defined experimental units, controls and competing measurements before deciding which causal alternatives disappear; this recheck does not automatically assign an operational intervention or personnel cause."
            status = "local_observation_only_cause_open"
        return [{
            "id": "No_data_allowed" if "No_data_allowed" in self.contexts else "cause_undetermined",
            "candidate": candidate, "status": status,
            "supporting_fact_ids": self.refs(evidence),
            "discriminating_action": action,
            "current_scope": "No tested root-cause claim; numeric refinement and recheck configuration are not causal falsification.",
        }]

    def inputs(self):
        return [{"fact_id": item.get("id"), "entity": _identity(item.get("entity")),
                 "value": _without_rows(item.get("value")), "evidence": _copy(item.get("evidence", []))}
                for item in self.facts if item.get("metric") == "input_file"]

    def sources(self):
        result, seen = [], set()
        for table in _maps(self.analysis.get("table_summaries")):
            item = _identity(table)
            path = item.get("path")
            if not path:
                continue
            item["source_role"] = "existing_analysis_table_summary"
            key = (path, item.get("sheet"), item.get("table_id"))
            if key not in seen:
                seen.add(key)
                result.append(item)
        for item in self.anomaly_records:
            path = item.get("path") or (item.get("entity", {}) if isinstance(item.get("entity"), dict) else {}).get("path")
            if not path:
                continue
            key = (path, None, "source_anomaly")
            if key in seen:
                continue
            seen.add(key)
            result.append({"path": _copy(path), "source_role": "source_anomaly",
                           "source_anomaly_id": item.get("id"), "evidence": _copy(item.get("evidence", []))})
        for item in self.inputs():
            path = item.get("entity", {}).get("path")
            if not path or any(source.get("path") == path and source.get("source_role") != "source_anomaly" for source in result):
                continue
            result.append({"path": _copy(path), "source_role": "reader_input_record",
                           "fact_id": item.get("fact_id"), "parser": _copy(item.get("value", {}).get("parser"))})
        return result

    def build(self):
        self.add_fit()
        self.add_paired_and_ratios()
        self.add_declared_qc()
        self.add_nested()
        self.add_computational()
        self.add_curves()
        self.add_fallback()
        if not self.followups:
            self.followups.append({
                "statement": "No source-supported new experiment is executed or invented. Before a mechanism claim, define a discriminating design from the actual source controls and unresolved metadata rather than assuming materials, repeat counts, costs or replacement actions.",
                "execution": "proposed_not_executed", "evidence": self.refs(self.first_ref()),
                "evidence_gap": "A source-defined causal experiment and independent allocation design remain unavailable in this partial seed.",
            })
        for text in _maps_text(self.analysis.get("coverage", {}).get("limitations", [])):
            self.add_uncertainty(text, self.first_ref())
        if not self.uncertainties:
            self.add_uncertainty("All unbound identities and unprovided semantic mappings remain unresolved; computed values are not a final interpretation.", self.first_ref())
        configs = [_copy(item) for item in _maps(self.case.get("analysis_rules_v1"))]
        tables = _maps(self.analysis.get("table_summaries"))
        records = self.analysis.get("coverage", {}).get("tables", {})
        all_inputs = self.inputs()
        table_paths = {item.get("path") for item in tables}
        non_tabular_inputs = [item for item in all_inputs
                              if item.get("entity", {}).get("path") not in table_paths]
        design = {
            "objective": [{"statement": _copy(self.case.get("objective", "Source-bound scientific interpretation")),
                           "evidence": ["case.json:objective"], "status": "existing_case_metadata"}],
            "hypotheses": [],
            "variables": configs,
            "experimental_units": [{key: _copy(value) for key, value in config.items()
                                    if key in ("entity_scope", "row_unit_column", "experimental_unit_columns", "pair_unit_columns",
                                               "pairing", "aggregate_inner", "outer_summary", "condition_column", "left_condition",
                                               "right_condition", "lineage_dimensions", "missing_policy")}
                                   for config in configs],
            "controls": [{key: _copy(value) for key, value in config.items()
                          if key in ("condition_column", "left_condition", "right_condition", "control_header_family",
                                     "comparison", "basis", "rule", "basis_for_exclusion")}
                         for config in configs],
            "steps": self.rechecks,
            "parameters": {"declared_source_rules": configs,
                           "actual_computational_configurations": [
                               {"run_id": run.get("run_id"), "estimator": run.get("estimator"),
                                "requested_params": _copy(run.get("requested_params")),
                                "pipeline_params": _copy(run.get("pipeline_params")),
                                "splitter": _copy(run.get("splitter")), "preprocessing": _copy(run.get("preprocessing"))}
                               for run in _maps(self.analysis.get("computational_results", {}).get("runs"))]},
            "resources": {"input_file_fact_count": len(all_inputs), "non_tabular_input_records": non_tabular_inputs,
                          "all_inline_input_facts": "work/analysis.json#/facts",
                          "generated_artifact_pointers": _copy(self.analysis.get("artifacts", [])),
                          "computational_predictions": "artifacts/computational_results.json" if _maps(self.analysis.get("computational_results", {}).get("runs")) else None},
            "time_cost_constraints": [],
            "decision_rules": [item for item in self.rechecks if item.get("operation")],
            "completion_criteria": [{"status": "pending_semantic_review",
                                     "rule": "The seed remains partial until source semantics, every unresolved mapping and the final interpretation are reviewed; it is never an automatic complete response."}],
        }
        model_semantic_checks = []
        for trend in self.trends:
            if trend.get("finding_type") != "declared_group_fit":
                continue
            domain = trend.get("response_domain")
            if not isinstance(domain, dict):
                continue
            model_semantic_checks.append({
                "fact_id": trend.get("fit_fact_id"),
                "group": _copy(trend.get("group")),
                "response_unit": _copy(domain.get("R_unit")),
                "count_below_required_lower_bound": _copy(domain.get("count_R_below_required_lower_bound")),
                "count_below_zero_same_rows_not_additional": _copy(domain.get("count_R_below_zero")),
                "count_above_one": _copy(domain.get("count_R_above_one")),
                "deficit_unit": "1",
                "local_covariance_residual_dof": _copy(trend.get("fit_result", {}).get("local_covariance_residual_dof")),
                "free_parameter_count": _copy(trend.get("fit_result", {}).get("free_parameter_count")),
                "review": "Count and compare groups separately; below-zero rows are a subset of below-bound rows, not additional violations.",
            })
        if model_semantic_checks:
            design["controls"] = design["controls"] + [{
                "control": "per_group_declared_model_response_domain",
                "groups": model_semantic_checks,
                "instruction": "Retain the exact per-group count and unit records when reviewing narrative; do not combine different groups into one count.",
            }]
        basis = {
            "basis": [{"status": "partial_pending_semantic_review",
                       "principle": "Numbers and identifiers are copied only from existing facts, declared coverage and source anomalies; unresolved bindings are not converted into trends."}],
            "risks": self.uncertainties,
            "exceptions": _copy(self.analysis.get("issues", [])),
            "coverage": {
                "facts_in_analysis": len(self.facts),
                "table_summaries": {"total": len(tables), "summarized": records.get("summarized"),
                                    "failed": _copy(records.get("failed", [])),
                                    "all_sheets_and_paths_listed_in_sources": True},
                "source_anomaly_count": len(self.anomaly_records),
                "declared_rules": self.rule_coverage,
                "curve_coverage": _copy(self.analysis.get("curve_analysis", {}).get("coverage", {})),
                "complete_original_record_pointers": ["work/analysis.json", "packets/facts.json", "packets/source-anomalies.json"],
            },
        }
        summary = ("Partial deterministic seed for %s: %d existing facts, %d table summaries, %d source anomalies; "
                   "semantic review remains required. Numeric values are linked to existing IDs and unresolved bindings are not assigned physical causes.") % (
                       str(self.case.get("case_id")), len(self.facts), len(tables), len(self.anomaly_records))
        confirmations = [{"id": "semantic-review", "kind": "semantic_mapping_and_final_interpretation",
                          "state": "pending", "action": "Review source semantics, declared units/control contexts and unresolved identities before editing this seed into any final response.",
                          "owner_role": "scientific_reviewer", "evidence": self.refs(self.first_ref())}]
        return {
            "case_id": _copy(self.case.get("case_id")), "status": "partial", "fields": {},
            "trends": self.trends, "anomalies": self.anomalies, "possible_causes": self.add_causes(),
            "rechecks": self.rechecks, "followup_experiments": self.followups,
            "experiment_design": design, "basis_and_risk": basis, "uncertainty": self.uncertainties,
            "confirmation_items": confirmations, "sources": self.sources(),
            "change_records": [{"stage": "reasoning_seed", "status": "partial", "deterministic": True,
                                "change": "Created a compact evidence-linked seed only; no pipeline, source data or output contract was changed.",
                                "original_records": "work/analysis.json"}],
            "result_text": summary,
        }


def _maps_text(items):
    return [str(item) for item in items or [] if isinstance(item, str) and item]


def result_facts_for_fit(facts, result):
    return [item for item in facts
            if item.get("declared_rule") in FIT_OPERATIONS and
            (item.get("metric") == "binding_source_random_error_fractions" or
             str(item.get("metric", "")).endswith("_source_random_error_fractions")) and
            (not result.get("fact_ids") or item.get("id") in set(result.get("fact_ids", [])))]


def make_seed(case, analysis):
    """Return a deterministic patch that is partial and safe to merge into finalize."""
    return _Seed(case, analysis).build()
