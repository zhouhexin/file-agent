"""准备受控采集计划、确认实际包金标并构建冻结评测集；只有prepare只读连接测试库。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from app.core.config import get_settings
from app.modules.evidence_evaluation.frozen_inputs import (
    CASE_ID, CapturePlan, digest, load_snapshot, private_dataset_path,
    require_test_database, write_new_json,
)
from app.scripts.evaluate_evidence import EvaluationCase


def read_workbook_plan(workbook: Path, sources_file: Path, case_ids: list[str], resolve_documents):
    """按当前工作簿取题干/标签；按源哈希唯一映射已入库活动副本，不猜文件名。"""
    from openpyxl import load_workbook

    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise ValueError("EMPTY_OR_DUPLICATE_CASE_IDS")
    if any(not re.fullmatch(CASE_ID, x) for x in case_ids):
        raise ValueError("INVALID_CASE_ID")
    wb = load_workbook(workbook, read_only=True, data_only=True)
    try:
        questions = {row[0]: row for row in wb["问题"].iter_rows(min_row=2, values_only=True) if row[0]}
        gold = {row[0]: row for row in wb["人工判定"].iter_rows(min_row=2, values_only=True) if row[0]}
        sources = {item["file_id"]: item for item in json.loads(sources_file.read_text(encoding="utf-8"))}
        selections, annotations = [], []
        resolved = {}
        for cid in case_ids:
            q, g = questions[cid], gold[cid]
            if g[7] != "已冻结" or any(type(v) not in (int, float) or v not in (0, 1) for v in g[1:4]):
                raise ValueError("WORKBOOK_GOLD_NOT_FROZEN")
            docs = []
            for fid in filter(None, re.split(r"[、,，\s]+", q[1])):
                sha = sources[fid]["sha256"]
                if sha not in resolved:
                    matches = resolve_documents(sha)
                    if len(matches) != 1:
                        raise ValueError("SOURCE_REQUIRES_ONE_ACTIVE_INDEXED_COPY:" + fid)
                    resolved[sha] = matches[0]
                docs.append(resolved[sha])
            selections.append({"case_id": cid, "question": q[2].strip(), "document_ids": sorted(set(docs))})
            issues = [{"type": kind, "evidence_ids": []} for kind, value in
                      zip(("MISSING", "CONFLICT", "OUTDATED"), g[1:4]) if value == 1]
            annotations.append({"case_id": cid, "group": issues[0]["type"] if issues else "NORMAL",
                                "expected_issues": issues, "expected_explanation": g[4] or "待核对实际证据包并补充理由",
                                "annotation_source": "用户确认参考答案后维护的工作簿标签；需再次核对实际包",
                                "annotation_status": "DRAFT", "input_sha256": None})
        return selections, annotations
    finally:
        wb.close()


def prepare(root: Path, workbook: Path, sources: Path, case_ids: list[str], settings, material_source="real") -> dict:
    require_test_database(settings)
    root = private_dataset_path(root)
    if (root / "capture-plan.json").exists() or (root / "annotations/gold-draft.json").exists():
        raise ValueError("CAPTURE_PLAN_ALREADY_EXISTS")
    from sqlalchemy import create_engine, text
    from app.modules.chunks.service import INDEX_VERSION

    engine = create_engine(settings.database_url, connect_args={
        "connect_timeout": 8, "options": "-c default_transaction_read_only=on -c statement_timeout=15000",
    })
    try:
        with engine.connect() as connection:
            def resolve(sha):
                return list(connection.execute(text("""
                    SELECT w.document_id FROM working_copies w
                    JOIN document_versions v ON v.id=w.current_version_id AND v.document_id=w.document_id
                    WHERE w.status='ACTIVE' AND v.sha256=:sha
                      AND EXISTS (SELECT 1 FROM document_index_runs i WHERE i.document_version_id=v.id
                                  AND i.status='COMPLETED' AND i.index_version=:index_version)
                """), {"sha": sha, "index_version": INDEX_VERSION}).scalars())
            cases, annotations = read_workbook_plan(workbook, sources, case_ids, resolve)
    finally:
        engine.dispose()
    plan = CapturePlan(dataset_id=root.name, cases=[{**case, "source": material_source} for case in cases])
    # 正文未进入普通日志；本地问题/标注文件按0600落盘。
    write_new_json(root / "capture-plan.json", plan.model_dump(mode="json"))
    write_new_json(root / "annotations/gold-draft.json", {"dataset_id": root.name, "cases": annotations})
    return {"prepared_cases": [c.case_id for c in plan.cases], "state": "AWAITING_REAL_CAPTURE"}


def confirm(root: Path, gold_file: Path, case_id: str, input_sha256: str, output: Path) -> dict:
    root, output = private_dataset_path(root), private_dataset_path(output)
    if not output.is_relative_to(root) or not re.fullmatch(CASE_ID, case_id) or not re.fullmatch(r"[0-9a-f]{64}", input_sha256):
        raise ValueError("INVALID_CONFIRM_TARGET")
    snapshot = load_snapshot(root / "captures" / case_id / input_sha256)
    if snapshot["manifest"]["case_id"] != case_id:
        raise ValueError("CAPTURE_CASE_MISMATCH")
    gold = json.loads(private_dataset_path(gold_file).read_text(encoding="utf-8"))
    matches = [item for item in gold["cases"] if item["case_id"] == case_id]
    if len(matches) != 1:
        raise ValueError("GOLD_CASE_NOT_UNIQUE")
    annotation = {**matches[0], "input_sha256": input_sha256, "annotation_status": "CONFIRMED"}
    # 使用正式评测协议先检查引用，冲突和旧版本正例不能留空引用。
    _case_from_snapshot(snapshot, annotation)
    write_new_json(output, {"dataset_id": root.name, "cases": [annotation]})
    return {"case_id": case_id, "input_sha256": input_sha256, "state": "CONFIRMED"}


def _case_from_snapshot(snapshot: dict, annotation: dict, observation: dict | None = None) -> EvaluationCase:
    manifest = snapshot["manifest"]
    if annotation.get("annotation_status") != "CONFIRMED" or annotation.get("input_sha256") != manifest["input_sha256"]:
        raise ValueError("GOLD_NOT_BOUND_TO_CAPTURE")
    if not annotation.get("expected_explanation", "").strip() or annotation["expected_explanation"].startswith("待核对"):
        raise ValueError("GOLD_EXPLANATION_REQUIRED")
    return EvaluationCase(
        case_id=annotation["case_id"], group=annotation["group"], package=snapshot["package"],
        versions=snapshot["versions"], expected_issues=annotation["expected_issues"],
        expected_explanation=annotation["expected_explanation"],
        recorded_semantic_calls=observation["semantic_calls"] if observation else None,
        temporal_focus=snapshot["context"]["temporal_focus"],
        original_question=snapshot["context"]["original_question"], input_sha256=manifest["input_sha256"],
        capture_config=(observation["runtime_config"] if observation else manifest["capture_config"]),
        source=manifest.get("material_source", "real"),
    )


def build(root: Path, gold_files: list[Path], output: Path, observations: list[Path]) -> dict:
    root, output = private_dataset_path(root), private_dataset_path(output)
    if not output.is_relative_to(root):
        raise ValueError("DATASET_OUTPUT_OUTSIDE_ROOT")
    annotations = [entry for f in gold_files for entry in json.loads(private_dataset_path(f).read_text())["cases"]]
    if not annotations or len({a["case_id"] for a in annotations}) != len(annotations):
        raise ValueError("EMPTY_OR_DUPLICATE_GOLD")
    cases, manifests = [], []
    observation_map = {}
    for file in observations:
        file = private_dataset_path(file)
        if not file.is_relative_to(root / "captures") or file.parent.name != "observations":
            raise ValueError("OBSERVATION_OUTSIDE_CAPTURE")
        item = json.loads(file.read_text())
        if file.parent.parent.name != item["input_sha256"] or item["input_sha256"] in observation_map:
            raise ValueError("OBSERVATION_INPUT_MISMATCH")
        observation_map[item["input_sha256"]] = item
    for annotation in annotations:
        cid, fingerprint = annotation["case_id"], annotation.get("input_sha256", "")
        if not re.fullmatch(CASE_ID, cid) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("INVALID_GOLD_IDENTITY")
        snapshot = load_snapshot(root / "captures" / cid / fingerprint)
        if snapshot["manifest"]["case_id"] != cid:
            raise ValueError("CAPTURE_CASE_MISMATCH")
        case = _case_from_snapshot(snapshot, annotation, observation_map.get(fingerprint))
        cases.append(case.model_dump(mode="json"))
        manifests.append(snapshot["manifest"])
    payload = {"cases": cases, "annotations": annotations, "manifest": {
        "schema_version": "evidence-frozen-dataset-v1", "dataset_id": root.name,
        "cases_sha256": digest(cases), "annotation_sha256": digest(annotations),
        "input_hashes": {c["case_id"]: c["input_sha256"] for c in cases},
        "capture_manifests": manifests,
    }}
    write_new_json(output, payload)
    return {"built_cases": len(cases), "dataset_sha256": payload["manifest"]["cases_sha256"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--workbook", type=Path, required=True)
    p.add_argument("--sources", type=Path, required=True)
    p.add_argument("--case-ids", nargs="+", required=True)
    p.add_argument("--material-source", choices=("real", "derived"), default="real")
    p = sub.add_parser("confirm")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--gold", type=Path, required=True)
    p.add_argument("--case-id", required=True)
    p.add_argument("--input-sha256", required=True)
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("build")
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--gold", type=Path, nargs="+", required=True)
    p.add_argument("--observations", type=Path, nargs="*", default=[])
    p.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args.root, args.workbook, args.sources, args.case_ids, get_settings(), args.material_source)
        elif args.action == "confirm":
            result = confirm(args.root, args.gold, args.case_id, args.input_sha256, args.output)
        else:
            result = build(args.root, args.gold, args.output, args.observations)
    except Exception as exc:
        # CLI错误也不回显数据库DSN、正文或业务绝对路径。
        safe = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z_]+(?::[A-Za-z0-9_-]+)?", str(exc)) else type(exc).__name__
        parser.exit(2, f"冻结数据操作失败：{safe}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
