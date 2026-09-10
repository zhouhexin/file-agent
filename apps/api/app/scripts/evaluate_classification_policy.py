"""Run a read-only, reproducible classification-policy evaluation.

This module deliberately has no database, storage, placement, or file-operation
dependencies.  It reads a frozen evaluation manifest plus taxonomy/rule snapshots
and writes only de-identified evaluation artifacts under the caller-provided
output directory.  Gold labels are loaded only after the prediction for a sample
has been generated, so they cannot affect candidate recall or primary selection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app.modules.classification.loader import load_taxonomy
from app.modules.classification.matcher import DocumentFeatures, match_document_features
from app.modules.classification.primary_selection import select_primary_category
from app.modules.classification.rule_policy import load_rule_policy


QUALITY_MODES = ("shadow", "conservative_rules", "calibrated")
FALLBACK_CATEGORY_ID = "system.other"
SCORED_LABEL_STATUS = "ADJUDICATED"


def evaluate_classification_policy(
    *,
    manifest_path: Path,
    taxonomy_snapshot_path: Path,
    rule_snapshot_path: Path,
    quality_mode: str,
    output_dir: Path,
) -> dict[str, Any]:
    """Evaluate frozen inputs without changing production data or source files."""

    if quality_mode not in QUALITY_MODES:
        raise ValueError(f"Unsupported quality mode: {quality_mode}")
    _prepare_output_dir(output_dir)

    taxonomy = load_taxonomy(taxonomy_snapshot_path)
    policy = load_rule_policy(rule_snapshot_path)
    _validate_snapshots(taxonomy=taxonomy, policy=policy, quality_mode=quality_mode)
    manifest = _load_manifest(manifest_path)
    samples = _validate_samples(manifest.get("samples"))
    gold_labels_path = _resolve_gold_labels_path(manifest=manifest, manifest_path=manifest_path)
    gold_by_sample_id = _load_gold_labels(gold_labels_path)

    _validate_sample_label_join(
        samples=samples,
        gold_by_sample_id=gold_by_sample_id,
        taxonomy=taxonomy,
    )
    results = [
        _evaluate_sample(
            sample=sample,
            gold=gold_by_sample_id.get(str(sample["sample_id"])),
            taxonomy=taxonomy,
            policy=policy,
            quality_mode=quality_mode,
            manifest_dir=manifest_path.parent,
        )
        for sample in samples
    ]
    summary = _build_summary(results=results, manifest=manifest, quality_mode=quality_mode)
    confusion_matrix = _build_confusion_matrix(results)
    version_manifest = _build_version_manifest(
        manifest_path=manifest_path,
        gold_labels_path=gold_labels_path,
        taxonomy_snapshot_path=taxonomy_snapshot_path,
        rule_snapshot_path=rule_snapshot_path,
        taxonomy=taxonomy,
        policy=policy,
        quality_mode=quality_mode,
    )

    _write_json(output_dir / "summary.json", summary)
    _write_jsonl(output_dir / "sample-results.jsonl", results)
    _write_json(output_dir / "confusion-matrix.json", confusion_matrix)
    _write_json(output_dir / "version-manifest.json", version_manifest)
    return summary


def _prepare_output_dir(output_dir: Path) -> None:
    """Avoid silently mixing a new run with an existing evaluation result."""

    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("output-dir must be empty so an evaluation run is immutable")
    output_dir.mkdir(parents=True, exist_ok=True)


def _validate_snapshots(*, taxonomy, policy, quality_mode: str) -> None:
    category_ids = {
        item.category_id
        for item in _flatten_taxonomy_category_ids(taxonomy)
        if item.category_id
    }
    if FALLBACK_CATEGORY_ID not in category_ids:
        raise ValueError("taxonomy snapshot must contain system.other")
    if taxonomy.fallback_policy is None or (
        taxonomy.fallback_policy.target_category_id != FALLBACK_CATEGORY_ID
    ):
        raise ValueError("taxonomy snapshot must use system.other as its fallback")
    unknown_rule_categories = sorted(
        {
            category_id
            for rule in policy.rules
            for category_id in rule.candidate_category_ids
            if category_id not in category_ids
        }
    )
    if unknown_rule_categories:
        raise ValueError("rule snapshot refers to category IDs absent from taxonomy")
    if quality_mode == "calibrated" and policy.policy_mode != "calibrated":
        raise ValueError("calibrated mode requires a calibrated rule snapshot")
    if quality_mode == "conservative_rules" and policy.policy_mode != "conservative_rules":
        raise ValueError("conservative_rules mode requires a conservative rule snapshot")


def _flatten_taxonomy_category_ids(taxonomy):
    from app.modules.classification.matcher import flatten_category_paths

    return flatten_category_paths(taxonomy)


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    if not isinstance(payload.get("gold_labels"), str) or not payload["gold_labels"].strip():
        raise ValueError("manifest must name a separate gold_labels file")
    return payload


def _validate_samples(raw_samples: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ValueError("manifest samples must be a non-empty list")
    samples: list[dict[str, Any]] = []
    sample_ids: set[str] = set()
    for raw in raw_samples:
        if not isinstance(raw, dict):
            raise ValueError("each manifest sample must be an object")
        sample_id = str(raw.get("sample_id") or "").strip()
        if not sample_id or sample_id in sample_ids:
            raise ValueError("manifest sample_id values must be unique and non-empty")
        has_inline_text = isinstance(raw.get("text"), str)
        has_text_path = isinstance(raw.get("text_path"), str) and bool(raw["text_path"].strip())
        if has_inline_text == has_text_path:
            raise ValueError("each sample must provide exactly one of text or text_path")
        sample_ids.add(sample_id)
        samples.append(raw)
    return samples


def _resolve_gold_labels_path(*, manifest: dict[str, Any], manifest_path: Path) -> Path:
    raw_path = str(manifest["gold_labels"]).strip()
    candidate = Path(raw_path)
    resolved = _resolve_manifest_relative_path(
        manifest_dir=manifest_path.parent,
        candidate=candidate,
    )
    if not resolved.is_file():
        raise ValueError("manifest gold_labels file does not exist")
    return resolved


def _load_gold_labels(path: Path) -> dict[str, dict[str, Any]]:
    payload = _load_json_or_jsonl(path)
    if not isinstance(payload, list):
        raise ValueError("gold_labels must be a JSON array or JSONL records")
    labels: dict[str, dict[str, Any]] = {}
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("each gold label must be an object")
        sample_id = str(raw.get("sample_id") or "").strip()
        if not sample_id or sample_id in labels:
            raise ValueError("gold label sample_id values must be unique and non-empty")
        should_abstain = raw.get("should_abstain")
        if not isinstance(should_abstain, bool):
            raise ValueError("gold labels must declare boolean should_abstain")
        if not should_abstain and not str(raw.get("gold_primary_category_id") or "").strip():
            raise ValueError("classifiable gold labels require gold_primary_category_id")
        labels[sample_id] = raw
    return labels


def _validate_sample_label_join(
    *,
    samples: list[dict[str, Any]],
    gold_by_sample_id: dict[str, dict[str, Any]],
    taxonomy,
) -> None:
    sample_ids = {str(sample["sample_id"]) for sample in samples}
    extra_labels = set(gold_by_sample_id) - sample_ids
    if extra_labels:
        raise ValueError("gold labels contain sample IDs absent from the manifest")
    missing_labels = [
        str(sample["sample_id"])
        for sample in samples
        if str(sample["sample_id"]) not in gold_by_sample_id
    ]
    if missing_labels:
        raise ValueError("every manifest sample must have a separate gold label")
    taxonomy_category_ids = {
        item.category_id
        for item in _flatten_taxonomy_category_ids(taxonomy)
        if item.category_id
    }
    unknown_gold_categories = sorted(
        {
            category_id
            for label in gold_by_sample_id.values()
            for category_id in [
                str(label.get("gold_primary_category_id") or ""),
                *_string_list(label.get("acceptable_secondary_category_ids")),
            ]
            if category_id and category_id not in taxonomy_category_ids
        }
    )
    if unknown_gold_categories:
        raise ValueError("gold labels refer to category IDs absent from taxonomy")


def _evaluate_sample(
    *,
    sample: dict[str, Any],
    gold: dict[str, Any] | None,
    taxonomy,
    policy,
    quality_mode: str,
    manifest_dir: Path,
) -> dict[str, Any]:
    text = _read_sample_text(sample=sample, manifest_dir=manifest_dir)
    filename = str(sample.get("filename") or "")
    title = str(sample.get("title") or "")
    features = DocumentFeatures(
        filename=filename,
        title=title,
        full_text=text,
        headings=_string_list(sample.get("headings")),
        sheet_names=_string_list(sample.get("sheet_names")),
        source_context=str(sample.get("source_context") or ""),
        verified_purpose_category_id=(
            str(sample["verified_purpose_category_id"])
            if sample.get("verified_purpose_category_id")
            else None
        ),
    )
    candidates = match_document_features(
        features,
        taxonomy,
        limit=8,
        rule_policy=policy,
    )
    fingerprint = _sha256_text(
        "\n".join((filename, title, str(sample.get("format") or ""), text))
    )
    selection = select_primary_category(
        taxonomy=taxonomy,
        candidates=candidates,
        input_fingerprint=fingerprint,
        policy_version=f"{policy.policy_id}:{policy.version}",
        extraction_status="COMPLETED",
        risk_passed=True,
        purpose_candidate=None,
    )
    candidate_ids = [
        str(item.get("category_id"))
        for item in candidates[:8]
        if str(item.get("category_id") or "") and str(item.get("category_id")) != FALLBACK_CATEGORY_ID
    ]
    predicted_primary_id = str(selection.primary_candidate.get("category_id") or FALLBACK_CATEGORY_ID)
    expected_primary_id = _expected_primary_id(gold)
    scored = _is_scored(gold=gold, sample=sample)
    expected_secondary = _string_list((gold or {}).get("acceptable_secondary_category_ids"))
    predicted_secondary = [
        str(item.get("category_id"))
        for item in selection.secondary_candidates
        if str(item.get("category_id") or "")
        and str(item.get("category_id")) != FALLBACK_CATEGORY_ID
    ]
    return {
        "sample_id": str(sample["sample_id"]),
        "family_id": str(sample.get("family_id") or ""),
        "format": str(sample.get("format") or _suffix_format(filename)),
        "input_sha256": fingerprint,
        "scored": scored,
        "scoring_exclusion": _scoring_exclusion(gold=gold, sample=sample),
        "candidate_category_ids_top8": candidate_ids,
        "predicted_primary_category_id": predicted_primary_id,
        "predicted_secondary_category_ids": predicted_secondary,
        "classification_outcome": str(selection.classification_outcome),
        "classification_quality": str(selection.classification_quality),
        "selection_basis": selection.selection_basis,
        "reason_codes": list(selection.reason_codes),
        "simulated_policy_action": _simulated_policy_action(quality_mode),
        "gold_primary_category_id": expected_primary_id if scored else None,
        "gold_secondary_category_ids": expected_secondary if scored else [],
        "primary_exact_match": (
            predicted_primary_id == expected_primary_id if scored else None
        ),
        "gold_business_in_top8": (
            expected_primary_id in candidate_ids
            if scored and expected_primary_id != FALLBACK_CATEGORY_ID
            else None
        ),
    }


def _read_sample_text(*, sample: dict[str, Any], manifest_dir: Path) -> str:
    if isinstance(sample.get("text"), str):
        return str(sample["text"])
    raw_path = str(sample.get("text_path") or "").strip()
    resolved = _resolve_manifest_relative_path(
        manifest_dir=manifest_dir,
        candidate=Path(raw_path),
    )
    if not resolved.is_file():
        raise ValueError("sample text_path does not exist")
    return resolved.read_text(encoding="utf-8")


def _resolve_manifest_relative_path(*, manifest_dir: Path, candidate: Path) -> Path:
    """Keep evaluation inputs inside the explicitly selected controlled manifest root."""

    if candidate.is_absolute():
        raise ValueError("manifest references must be relative to the manifest directory")
    root = manifest_dir.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("manifest references must stay within the manifest directory") from exc
    return resolved


def _expected_primary_id(gold: dict[str, Any] | None) -> str:
    if gold is None or bool(gold.get("should_abstain")):
        return FALLBACK_CATEGORY_ID
    return str(gold.get("gold_primary_category_id") or FALLBACK_CATEGORY_ID)


def _is_scored(*, gold: dict[str, Any] | None, sample: dict[str, Any]) -> bool:
    if gold is None:
        return False
    if str(gold.get("adjudication_status") or "").upper() != SCORED_LABEL_STATUS:
        return False
    return not bool(sample.get("duplicate_of")) and not bool(sample.get("out_of_scope"))


def _scoring_exclusion(*, gold: dict[str, Any] | None, sample: dict[str, Any]) -> str | None:
    if bool(sample.get("duplicate_of")):
        return "DUPLICATE"
    if bool(sample.get("out_of_scope")):
        return "OUT_OF_SCOPE"
    if gold is None or str(gold.get("adjudication_status") or "").upper() != SCORED_LABEL_STATUS:
        return "UNADJUDICATED"
    return None


def _simulated_policy_action(quality_mode: str) -> str:
    return {
        "shadow": "SHADOW_ONLY",
        "conservative_rules": "SIMULATED_CONSERVATIVE_RULES",
        "calibrated": "SIMULATED_CALIBRATED_POLICY",
    }[quality_mode]


def _build_summary(
    *, results: list[dict[str, Any]], manifest: dict[str, Any], quality_mode: str
) -> dict[str, Any]:
    scored = [item for item in results if item["scored"]]
    business_expected = [
        item for item in scored if item["gold_primary_category_id"] != FALLBACK_CATEGORY_ID
    ]
    predicted_business = [
        item
        for item in scored
        if item["predicted_primary_category_id"] != FALLBACK_CATEGORY_ID
    ]
    predicted_other = [
        item
        for item in scored
        if item["predicted_primary_category_id"] == FALLBACK_CATEGORY_ID
    ]
    secondary_counts = _secondary_counts(scored)
    summary = {
        "schema_version": 1,
        "dataset_id": str(manifest.get("dataset_id") or ""),
        "quality_mode": quality_mode,
        "total_samples": len(results),
        "scored_samples": len(scored),
        "family_count": len({item["family_id"] for item in scored if item["family_id"]}),
        "excluded_samples": dict(Counter(
            item["scoring_exclusion"] for item in results if item["scoring_exclusion"]
        )),
        "metrics": {
            "business_candidate_recall_at_8": _rate(
                sum(bool(item["gold_business_in_top8"]) for item in business_expected),
                len(business_expected),
            ),
            "exact_primary_category_id_accuracy": _rate(
                sum(bool(item["primary_exact_match"]) for item in scored), len(scored)
            ),
            "non_other_automatic_business_precision": _rate(
                sum(
                    item["predicted_primary_category_id"]
                    == item["gold_primary_category_id"]
                    for item in predicted_business
                ),
                len(predicted_business),
            ),
            "non_other_automatic_business_coverage": _rate(
                len(predicted_business), len(scored)
            ),
            "business_coverage_on_gold_business": _rate(
                sum(
                    item["predicted_primary_category_id"] != FALLBACK_CATEGORY_ID
                    for item in business_expected
                ),
                len(business_expected),
            ),
            "other_precision": _rate(
                sum(
                    item["gold_primary_category_id"] == FALLBACK_CATEGORY_ID
                    for item in predicted_other
                ),
                len(predicted_other),
            ),
            "other_rate": _rate(len(predicted_other), len(scored)),
            "other_recall": _rate(
                sum(
                    item["predicted_primary_category_id"] == FALLBACK_CATEGORY_ID
                    for item in scored
                    if item["gold_primary_category_id"] == FALLBACK_CATEGORY_ID
                ),
                sum(
                    item["gold_primary_category_id"] == FALLBACK_CATEGORY_ID
                    for item in scored
                ),
            ),
            "secondary_label_precision": _rate(
                secondary_counts["true_positive"], secondary_counts["predicted"]
            ),
            "secondary_label_recall": _rate(
                secondary_counts["true_positive"], secondary_counts["gold"]
            ),
        },
        "strata": {
            "by_format": _stratified_metrics(scored, key="format"),
            "by_gold_primary_category_id": _stratified_metrics(
                scored, key="gold_primary_category_id"
            ),
        },
    }
    return summary


def _secondary_counts(results: Iterable[dict[str, Any]]) -> dict[str, int]:
    true_positive = predicted = gold = 0
    for item in results:
        predicted_ids = set(item["predicted_secondary_category_ids"])
        gold_ids = set(item["gold_secondary_category_ids"])
        true_positive += len(predicted_ids & gold_ids)
        predicted += len(predicted_ids)
        gold += len(gold_ids)
    return {"true_positive": true_positive, "predicted": predicted, "gold": gold}


def _stratified_metrics(results: list[dict[str, Any]], *, key: str) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        groups[str(item.get(key) or "UNSPECIFIED")].append(item)
    return {
        group: {
            "sample_count": len(items),
            "exact_primary_category_id_accuracy": _rate(
                sum(bool(item["primary_exact_match"]) for item in items), len(items)
            ),
            "other_rate": _rate(
                sum(item["predicted_primary_category_id"] == FALLBACK_CATEGORY_ID for item in items),
                len(items),
            ),
        }
        for group, items in sorted(groups.items())
    }


def _rate(numerator: int, denominator: int) -> dict[str, Any]:
    value = round(numerator / denominator, 6) if denominator else None
    interval = _wilson_interval(numerator, denominator)
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "confidence_interval_95": interval,
    }


def _wilson_interval(numerator: int, denominator: int) -> dict[str, float] | None:
    if denominator == 0:
        return None
    z = 1.959963984540054
    proportion = numerator / denominator
    denominator_adjusted = 1 + z * z / denominator
    center = (proportion + z * z / (2 * denominator)) / denominator_adjusted
    margin = (
        z
        * ((proportion * (1 - proportion) / denominator + z * z / (4 * denominator * denominator)) ** 0.5)
        / denominator_adjusted
    )
    return {"lower": round(max(0.0, center - margin), 6), "upper": round(min(1.0, center + margin), 6)}


def _build_confusion_matrix(results: list[dict[str, Any]]) -> dict[str, Any]:
    rows: dict[str, Counter[str]] = defaultdict(Counter)
    for item in results:
        if item["scored"]:
            rows[str(item["gold_primary_category_id"])][
                str(item["predicted_primary_category_id"])
            ] += 1
    return {
        "schema_version": 1,
        "rows": [
            {
                "gold_primary_category_id": gold_id,
                "sample_count": sum(predictions.values()),
                "predicted_primary_counts": dict(sorted(predictions.items())),
            }
            for gold_id, predictions in sorted(rows.items())
        ],
    }


def _build_version_manifest(
    *,
    manifest_path: Path,
    gold_labels_path: Path,
    taxonomy_snapshot_path: Path,
    rule_snapshot_path: Path,
    taxonomy,
    policy,
    quality_mode: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "runner": "app.scripts.evaluate_classification_policy",
        "quality_mode": quality_mode,
        "fallback_category_id": FALLBACK_CATEGORY_ID,
        "inputs": {
            "manifest_sha256": _sha256_file(manifest_path),
            "gold_labels_sha256": _sha256_file(gold_labels_path),
            "taxonomy_snapshot_sha256": _sha256_file(taxonomy_snapshot_path),
            "rule_snapshot_sha256": _sha256_file(rule_snapshot_path),
        },
        "taxonomy": {"key": taxonomy.key, "version": taxonomy.version},
        "rule_policy": {
            "policy_id": policy.policy_id,
            "version": policy.version,
            "policy_mode": policy.policy_mode,
        },
    }


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("unable to read JSON input") from exc


def _load_json_or_jsonl(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise ValueError("unable to read JSON or JSONL input") from exc


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _suffix_format(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".") or "UNKNOWN"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run a read-only classification policy evaluation")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--taxonomy-snapshot", required=True, type=Path)
    parser.add_argument("--rule-snapshot", required=True, type=Path)
    parser.add_argument("--quality-mode", required=True, choices=QUALITY_MODES)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    summary = evaluate_classification_policy(
        manifest_path=args.manifest,
        taxonomy_snapshot_path=args.taxonomy_snapshot,
        rule_snapshot_path=args.rule_snapshot,
        quality_mode=args.quality_mode,
        output_dir=args.output_dir,
    )
    print(
        json.dumps(
            {
                "scored_samples": summary["scored_samples"],
                "total_samples": summary["total_samples"],
                "quality_mode": summary["quality_mode"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
