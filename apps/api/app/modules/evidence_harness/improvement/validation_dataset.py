"""C阶段真实任务题库：锁定已核验候选、合并原始上下文、人工分组与冻结，不调用模型。"""

from pathlib import Path
from typing import Literal

from pydantic import Field

from app.modules.evidence_evaluation.schemas import StrictModel
from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.routing.dataset import current_catalog
from app.modules.evidence_harness.routing.schemas import RoutingInputs, Route
from app.modules.evidence_harness.schemas import Hash
from app.modules.evidence_harness.improvement import adaptive_policy as policy


class ImportedInput(StrictModel):
    """输入导出文件必须保留真实来源，不允许手改题干或补造附件。"""

    path: str
    sha256: Hash


class CaseAnnotation(StrictModel):
    """人工首轮目标与文件族，保留集资格需额外显式确认。"""

    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    split: Literal["DEVELOPMENT", "HOLDOUT"] | None = None
    family_ids: list[str] = Field(default_factory=list, max_length=10)
    expected_route: Route | None = None
    expected_decision_type: Literal["TOOL_PLAN", "CLARIFY", "DIRECT_RESPONSE", "FINISH"] | None = None
    reason: str = Field(default="", max_length=2000)
    confirmed: bool = False
    holdout_independence_confirmed: bool = False


class ValidationDraft(StrictModel):
    """只有annotations允许人工填写，来源和实际输入必须从原文件重建一致。"""

    schema_version: Literal["adaptive-validation-draft-v1"] = "adaptive-validation-draft-v1"
    candidate_source: str
    candidate_source_ref: Hash
    policy_ref: Hash
    imports: list[ImportedInput]
    inputs: RoutingInputs
    annotations: list[CaseAnnotation]


class ValidationDataset(ValidationDraft):
    """冻结原始任务及独立人工标签，不能把预期答案交给Planner或建议器。"""

    schema_version: Literal["adaptive-validation-dataset-v1"] = "adaptive-validation-dataset-v1"
    inputs_sha256: Hash
    annotations_sha256: Hash


def lock_candidate(directory):
    """独立重算B实验并从原提案重建候选，不信缓存IMPROVED或孤立patch文件。"""
    directory = _directory(directory)
    report = policy.compare(directory)
    if report.get("decision") != "IMPROVED" or not report.get("strategy_evaluable"):
        raise ValueError("VALIDATION_CANDIDATE_NOT_VERIFIED_IMPROVED")
    meta = store._read(directory / "experiment.json")[0]
    version = "adaptive-policy-feedback-v1" if meta["schema_version"] == "adaptive-policy-experiment-v1" else "adaptive-policy-feedback-v2"
    origin = policy.load_baseline(store.TEST_ROOT / meta["baseline_source"], feedback_version=version)
    candidate = policy.materialize((directory / "baseline" / policy.TARGET).read_text(), store._read(directory / "proposal.json")[0],
                                  origin["feedback"], origin["cases"])
    if candidate["prompt_ref"] != report["policy_ref"] or candidate["patch_ref"] != report["patch_ref"]:
        raise ValueError("VALIDATION_CANDIDATE_IDENTITY_INVALID")
    return {"directory": directory, "metadata": meta, "candidate": candidate, "origin": origin,
            "source_ref": digest(store._read(directory / "integrity.json")[0])}


def _merge(locked, imports):
    """只合并原始inputs，题号、源运行、Catalog和真实/Fake身份都不能混用。"""
    original = locked["origin"]["dataset"].inputs
    cases, refs = list(original.cases), list(original.source_catalog_refs)
    ids, runs = {c.case_id for c in cases}, {c.source_run_ref for c in cases}
    for item in imports:
        path = store._dataset_file(store.TEST_ROOT / item.path)
        raw, sha = store._read(path)
        inputs = RoutingInputs.model_validate(raw)
        if sha != item.sha256:
            raise ValueError("VALIDATION_IMPORTED_INPUT_CHANGED")
        if inputs.source != original.source or inputs.catalog_snapshot != original.catalog_snapshot:
            raise ValueError("VALIDATION_IMPORT_PROFILE_MISMATCH")
        for case in inputs.cases:
            if case.case_id in ids or case.source_run_ref in runs:
                raise ValueError("VALIDATION_DUPLICATE_CASE_OR_SOURCE")
            cases.append(case)
            ids.add(case.case_id)
            runs.add(case.source_run_ref)
        refs.extend(inputs.source_catalog_refs)
    return RoutingInputs(source=original.source, catalog_snapshot=original.catalog_snapshot, cases=cases, source_catalog_refs=refs)


def prepare(*, candidate_dir, inputs_paths, output):
    """新增人工核对草稿，不替用户确认标签、文件族或保留集资格。"""
    locked = lock_candidate(candidate_dir)
    output = store._dataset_file(output)
    if output.exists():
        raise FileExistsError("VALIDATION_OUTPUT_ALREADY_EXISTS")
    imports = []
    for path in inputs_paths:
        path = store._dataset_file(path)
        imports.append(ImportedInput(path=path.relative_to(store.TEST_ROOT).as_posix(), sha256=store._read(path)[1]))
    inputs = _merge(locked, imports)
    old_gold = {g.case_id: g for g in locked["origin"]["dataset"].gold}
    draft = ValidationDraft(candidate_source=locked["directory"].relative_to(store.TEST_ROOT).as_posix(),
        candidate_source_ref=locked["source_ref"], policy_ref=locked["candidate"]["prompt_ref"], imports=imports, inputs=inputs,
        annotations=[CaseAnnotation(case_id=c.case_id, split="DEVELOPMENT" if c.case_id in old_gold else None,
            expected_route=old_gold[c.case_id].expected_route if c.case_id in old_gold else None,
            reason=old_gold[c.case_id].reason if c.case_id in old_gold else "") for c in inputs.cases])
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_new_json(output / "dataset-draft.json", draft.model_dump(mode="json"))
    write_new_json(output / "annotations-draft.json", {"cases": [a.model_dump(mode="json") for a in draft.annotations]})
    lines = ["# C题库人工核对", "", "优先只编辑annotations-draft.json，冻结时通过--annotations明确传入。原任务与附件不得编辑。",
        "原B两题固定为DEVELOPMENT；新增题填写分组、文件族、预期路线、理由和confirmed=true。",
        "HOLDOUT还须明确holdout_independence_confirmed=true；OTHER须指定预期决策类型。",
        "两个分组不可共享文件族、实际文件ID或原始问题；保留标签不能来自旧证据状态表。", ""]
    for case in inputs.cases:
        lines += [f"## {case.case_id}", "", case.question, "",
                  f"实际附件{len(case.attachments)}份、会话文件{len(case.context_documents)}份。", ""]
    policy._write_text(output / "人工核对说明.md", "\n".join(lines))
    return {"stage": "AWAITING_MANUAL_VALIDATION_GOLD", "case_count": len(inputs.cases),
            "locked_policy_ref": draft.policy_ref, "model_calls": 0}


def validate(dataset):
    """检查所有人工标签、真实来源和开发/保留泄漏；不自动纠正或猜测任何分组。"""
    locked = lock_candidate(store.TEST_ROOT / dataset.candidate_source)
    if dataset.candidate_source_ref != locked["source_ref"] or dataset.policy_ref != locked["candidate"]["prompt_ref"]:
        raise ValueError("VALIDATION_LOCKED_CANDIDATE_CHANGED")
    if dataset.inputs != _merge(locked, dataset.imports) or dataset.inputs.catalog_snapshot != current_catalog():
        raise ValueError("VALIDATION_ORIGINAL_INPUT_CHANGED")
    ids = {c.case_id for c in dataset.inputs.cases}
    annotations = {a.case_id: a for a in dataset.annotations}
    if len(annotations) != len(dataset.annotations) or set(annotations) != ids:
        raise ValueError("VALIDATION_ANNOTATION_CASE_MISMATCH")
    if len(ids) < 4 or len(ids) <= len(locked["origin"]["cases"]):
        raise ValueError("VALIDATION_EXPANSION_REQUIRED")
    origin_ids = {c.case_id for c in locked["origin"]["cases"]}
    origin_gold = {g.case_id: g.expected_route for g in locked["origin"]["dataset"].gold}
    families, documents, questions = {}, {}, {}
    for split in ("DEVELOPMENT", "HOLDOUT"):
        selected = [a for a in dataset.annotations if a.split == split]
        if len(selected) < 2 or not {"CONTENT_ANSWER", "FILE_LIST"} <= {a.expected_route for a in selected}:
            raise ValueError("VALIDATION_SPLIT_COVERAGE_REQUIRED")
        families[split], documents[split], questions[split] = set(), set(), set()
    all_tasks = set()
    for case in dataset.inputs.cases:
        a = annotations[case.case_id]
        family_ids = [f.strip().casefold() for f in a.family_ids]
        if (not a.confirmed or a.expected_route is None or not a.reason.strip() or a.split is None
            or not family_ids or any(not f or len(f) > 120 for f in family_ids) or len(set(family_ids)) != len(family_ids)):
            raise ValueError("VALIDATION_MANUAL_ANNOTATION_REQUIRED")
        if a.expected_route == "OTHER" and a.expected_decision_type is None:
            raise ValueError("VALIDATION_OTHER_DECISION_REQUIRED")
        if case.case_id in origin_ids and a.split != "DEVELOPMENT":
            raise ValueError("VALIDATION_EXPOSED_CASE_IN_HOLDOUT")
        if case.case_id in origin_gold and a.expected_route != origin_gold[case.case_id]:
            raise ValueError("VALIDATION_ORIGINAL_GOLD_CHANGED")
        if a.split == "HOLDOUT" and not a.holdout_independence_confirmed:
            raise ValueError("VALIDATION_HOLDOUT_CONFIRMATION_REQUIRED")
        question = "".join(case.question.casefold().split())
        task_ref = digest([question, [d.model_dump() for d in case.attachments], [d.model_dump() for d in case.context_documents]])
        if task_ref in all_tasks:
            raise ValueError("VALIDATION_DUPLICATE_TASK")
        all_tasks.add(task_ref)
        families[a.split].update(family_ids)
        questions[a.split].add(question)
        documents[a.split].update(d.document_id for d in [*case.attachments, *case.context_documents])
    if (families["DEVELOPMENT"] & families["HOLDOUT"] or documents["DEVELOPMENT"] & documents["HOLDOUT"]
        or questions["DEVELOPMENT"] & questions["HOLDOUT"]):
        raise ValueError("VALIDATION_CROSS_SPLIT_LEAKAGE")
    return locked


def freeze(*, draft_path, output, annotations_path=None):
    """确认后另存冻结数据；保留草稿与全部原输入，不覆盖旧题库。"""
    draft = ValidationDraft.model_validate(store._read(store._dataset_file(draft_path))[0])
    if annotations_path is not None:
        raw = store._read(store._dataset_file(annotations_path))[0]
        if set(raw) != {"cases"}:
            raise ValueError("VALIDATION_ANNOTATION_SCHEMA_INVALID")
        draft = draft.model_copy(update={"annotations": [CaseAnnotation.model_validate(a) for a in raw["cases"]]})
    locked = validate(draft)
    values = draft.model_dump(mode="json")
    values.update(schema_version="adaptive-validation-dataset-v1", inputs_sha256=digest(values["inputs"]),
                  annotations_sha256=digest(values["annotations"]))
    dataset = ValidationDataset.model_validate(values)
    output = store._dataset_file(output)
    write_new_json(output, dataset.model_dump(mode="json"))
    return {"stage": "VALIDATION_DATASET_FROZEN", "case_count": len(dataset.inputs.cases),
            "locked_policy_ref": locked["candidate"]["prompt_ref"], "model_calls": 0}


def load(path):
    """每次执行和比较均重新验证散列与真实来源，未确认草稿不能作为可运行题库。"""
    path = store._dataset_file(path)
    dataset = ValidationDataset.model_validate(store._read(path)[0])
    if dataset.inputs_sha256 != digest(dataset.inputs.model_dump(mode="json")) or dataset.annotations_sha256 != digest([a.model_dump(mode="json") for a in dataset.annotations]):
        raise ValueError("VALIDATION_FROZEN_DATA_CHANGED")
    locked = validate(dataset)
    return dataset, locked
