"""Tests for the isolated D6 classification-policy evaluator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.modules.classification.loader import DEFAULT_TAXONOMY_PATH
from app.modules.classification.rule_policy import DEFAULT_RULE_POLICY_PATH
from app.scripts.evaluate_classification_policy import evaluate_classification_policy


def _write_evaluation_inputs(tmp_path: Path) -> Path:
    """Create de-identified inline texts; the evaluator must never write them back."""

    (tmp_path / "gold-labels.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "sample_id": "EVAL-001",
                        "should_abstain": True,
                        "adjudication_status": "ADJUDICATED",
                        "acceptable_secondary_category_ids": [],
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "sample_id": "EVAL-002",
                        "should_abstain": True,
                        "adjudication_status": "UNRESOLVED",
                        "acceptable_secondary_category_ids": [],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "dataset_id": "deidentified-evaluator-test-v1",
        "gold_labels": "gold-labels.jsonl",
        "samples": [
            {
                "sample_id": "EVAL-001",
                "family_id": "family-one",
                "format": "txt",
                "filename": "neutral.txt",
                "text": "SENSITIVE-EVALUATION-TEXT-ONE",
            },
            {
                "sample_id": "EVAL-002",
                "family_id": "family-two",
                "format": "txt",
                "filename": "neutral-two.txt",
                "text": "SENSITIVE-EVALUATION-TEXT-TWO",
            },
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def test_evaluator_writes_only_deidentified_read_only_artifacts(tmp_path):
    """D6: no DB/placement is needed and result artifacts omit source text."""

    manifest_path = _write_evaluation_inputs(tmp_path)
    output_dir = tmp_path / "output"

    summary = evaluate_classification_policy(
        manifest_path=manifest_path,
        taxonomy_snapshot_path=DEFAULT_TAXONOMY_PATH,
        rule_snapshot_path=DEFAULT_RULE_POLICY_PATH,
        quality_mode="conservative_rules",
        output_dir=output_dir,
    )

    assert summary["total_samples"] == 2
    assert summary["scored_samples"] == 1
    assert summary["metrics"]["exact_primary_category_id_accuracy"]["denominator"] == 1
    assert (output_dir / "summary.json").is_file()
    assert (output_dir / "sample-results.jsonl").is_file()
    assert (output_dir / "confusion-matrix.json").is_file()
    assert (output_dir / "version-manifest.json").is_file()
    exported_text = "\n".join(
        path.read_text(encoding="utf-8") for path in output_dir.iterdir()
    )
    assert "SENSITIVE-EVALUATION-TEXT" not in exported_text
    assert "neutral-two.txt" not in exported_text
    rows = [
        json.loads(line)
        for line in (output_dir / "sample-results.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert rows[0]["simulated_policy_action"] == "SIMULATED_CONSERVATIVE_RULES"
    assert rows[1]["scored"] is False
    assert rows[1]["scoring_exclusion"] == "UNADJUDICATED"


def test_evaluator_rejects_calibrated_mode_without_calibrated_snapshot(tmp_path):
    """A conservative policy cannot be mislabeled as a calibrated policy."""

    manifest_path = _write_evaluation_inputs(tmp_path)
    with pytest.raises(ValueError, match="calibrated rule snapshot"):
        evaluate_classification_policy(
            manifest_path=manifest_path,
            taxonomy_snapshot_path=DEFAULT_TAXONOMY_PATH,
            rule_snapshot_path=DEFAULT_RULE_POLICY_PATH,
            quality_mode="calibrated",
            output_dir=tmp_path / "output",
        )


def test_evaluator_refuses_to_mix_results_into_nonempty_output_directory(tmp_path):
    """A rerun requires a fresh output directory rather than overwriting evidence."""

    manifest_path = _write_evaluation_inputs(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "existing.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="output-dir must be empty"):
        evaluate_classification_policy(
            manifest_path=manifest_path,
            taxonomy_snapshot_path=DEFAULT_TAXONOMY_PATH,
            rule_snapshot_path=DEFAULT_RULE_POLICY_PATH,
            quality_mode="conservative_rules",
            output_dir=output_dir,
        )
