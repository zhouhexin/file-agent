"""实验历史回写：独立重算后生成新版安全反馈，不修改旧反馈、Gold或实验文件。"""

from contextvars import ContextVar
from pathlib import Path

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.schemas import FeedbackPacketV2, ExperimentHistory, HistoryTrial, TrialSignal
from app.scripts.evaluate_evidence import load_cases

_depth = ContextVar("harness_history_depth", default=0)


def _entry(directory: Path, expected):
    """以真实逐轮结果重新评分，不读取缓存comparison作为模型可见的事实依据。"""
    from app.modules.evidence_harness.optimizer import _directory, load_feedback
    from app.modules.evidence_harness.comparator import compare_experiment
    directory = _directory(directory)
    comparison = compare_experiment(directory)
    if comparison["decision"] == "INVALID_EXPERIMENT":
        raise ValueError("HISTORY_EXPERIMENT_INVALID")
    experiment, _ = store._read(directory / "experiment.json")
    if (experiment["dataset_sha256"] != expected.dataset_sha256
        or experiment["annotation_sha256"] != expected.annotation_sha256):
        raise ValueError("HISTORY_DATASET_MISMATCH")
    old = load_feedback(store.TEST_ROOT / experiment["feedback_source"])
    baseline = next(r for r in old.reports if r.report_ref == old.baseline_ref)
    index, _ = store._read(store.TEST_ROOT / experiment["feedback_source"] / "source-index.json")
    cases = {c.case_id: c for c in load_cases(store._dataset_file(store.TEST_ROOT / index["dataset_source"]))}
    ref = digest([experiment, comparison])
    trials = []
    for arm in ("control", "candidate"):
        for path in sorted((directory / arm / "rounds").glob("*.json")):
            record, _ = store._read(path)
            trial = store._round(record["result"], report_ref=ref, case=cases[record["case_id"]],
                                 repetition=record["repetition"])
            fields = {k: getattr(trial, k) for k in TrialSignal.model_fields}
            fields["feedback_id"] = digest([ref, arm, record["case_id"], record["repetition"]])
            trials.append(HistoryTrial(**fields, arm=arm))
    return ExperimentHistory(experiment_ref=ref, condition_ref=baseline.condition_ref, source=experiment["source"],
        decision=comparison["decision"], execution_stage=comparison["execution_stage"],
        control_budget=experiment["profiles"]["control"], candidate_budget=experiment["profiles"]["candidate"],
        execution_code_ref=experiment["executor_code_sha256"], trials=trials)


def extend_feedback(base, index: dict, experiment_dirs: list[Path], *, source: str | None = None):
    """显式选择历史、限制递归与数量；不同来源不能把Fake当成live改进经验。"""
    depth = _depth.get()
    if depth >= 8 or len(experiment_dirs) > 10:
        raise ValueError("HISTORY_DEPTH_OR_COUNT_EXCEEDED")
    token = _depth.set(depth + 1)
    try:
        entries, sources, refs = [], [], set()
        from app.modules.evidence_harness.optimizer import _directory
        for path in experiment_dirs:
            path = _directory(path)
            entry = _entry(path, base)
            if source is not None and entry.source != source:
                raise ValueError("HISTORY_LIVE_FAKE_MISMATCH")
            if entry.experiment_ref in refs:
                raise ValueError("DUPLICATE_EXPERIMENT_HISTORY")
            refs.add(entry.experiment_ref);entries.append(entry)
            sources.append({"experiment_ref": entry.experiment_ref, "source": str(path.relative_to(store.TEST_ROOT))})
        fields = base.model_dump(mode="json")
        fields.pop("schema_version")
        fields.pop("experiment_history", None)
        packet = FeedbackPacketV2(**fields, experiment_history=entries)
        return packet, {**index, "experiment_sources": sources}
    finally:
        _depth.reset(token)


def rebuild_extended_feedback(raw: dict, public: dict, index: dict):
    """加载v2时从原报告及历史重建；即使重写自带散列也不能混入任意摘要。"""
    base_index = {k: v for k, v in index.items() if k != "experiment_sources"}
    baseline = next(r for r in index["reports"] if r["report_ref"] == raw["baseline_ref"])
    base, fresh_index = store.build_feedback(dataset_path=store.TEST_ROOT / index["dataset_source"],
        baseline_path=store.TEST_ROOT / baseline["source"],
        history_paths=[store.TEST_ROOT / r["source"] for r in index["reports"] if r is not baseline])
    if base_index != fresh_index:
        raise ValueError("FEEDBACK_SOURCE_CHANGED")
    packet, reconstructed = extend_feedback(base, fresh_index,
        [store.TEST_ROOT / s["source"] for s in index["experiment_sources"]])
    if (packet.model_dump(mode="json") != raw or packet.optimizer_input().model_dump(mode="json") != public
        or reconstructed != index):
        raise ValueError("HISTORY_FEEDBACK_CHANGED")
    return packet


def save_feedback(packet, index: dict, output: Path):
    """只新增一版反馈，完成标记最后发布，不回写旧实验的完整性清单。"""
    from app.modules.evidence_harness.optimizer import _directory
    output = _directory(output)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700);output.mkdir(mode=0o700, exist_ok=False)
    raw, public = packet.model_dump(mode="json"), packet.optimizer_input().model_dump(mode="json")
    for name, value in (("feedback", raw), ("optimizer-input", public), ("source-index", index)):
        write_new_json(output / f"{name}.json", value)
    manifest = {"stage": "FEEDBACK_READY", "feedback_sha256": digest(raw),
        "optimizer_input_sha256": digest(public), "source_index_sha256": digest(index),
        "report_count": len(packet.reports), "experiment_history_count": len(packet.experiment_history),
        "model_calls": 0}
    write_new_json(output / "manifest.json", manifest)
    return manifest
