"""只读导出真实首轮任务上下文，确认后冻结，模型输入不携带人工标签。"""

from pathlib import Path

from sqlalchemy import create_engine, text

from app.modules.agent.catalog import AgentCatalogService
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.evidence_evaluation.frozen_inputs import digest, require_test_database, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.routing.schemas import (
    RoutingCase, RoutingDataset, RoutingDocument, RoutingGold, RoutingInputs, SourceSignal,
)


def current_catalog() -> dict:
    """从真实Registry和Skill manifest构建当前实验Catalog，所有handler保持未调用。"""
    return AgentCatalogService(registry=ToolRegistry()).build_snapshot()


def _documents(values: list[dict]) -> list[RoutingDocument]:
    """逐字段投影原上下文，禁止把摘要、路径或任意扩展字段透传给规划模型。"""
    result = []
    for value in values:
        document_id = value.get("document_id")
        if not document_id:
            raise ValueError("SOURCE_DOCUMENT_ID_MISSING")
        fields = {name: value.get(name) or "" for name in RoutingDocument.model_fields}
        fields["filename"] = Path(str(fields["filename"]).replace("\\", "/")).name
        result.append(RoutingDocument.model_validate(fields))
    return result


def prepare(*, user_id: str, run_ids: list[str], case_ids: list[str], output: Path, settings) -> dict:
    """只读查询本人已结束运行，新增私有任务快照及未确认的Gold草稿。"""
    require_test_database(settings)
    output = store._dataset_file(output)
    if output.exists():
        raise FileExistsError("OUTPUT_ALREADY_EXISTS")
    if not run_ids or len(set(run_ids)) != len(run_ids) or len(run_ids) > 50:
        raise ValueError("INVALID_ROUTING_RUN_IDS")
    if case_ids and len(case_ids) != len(run_ids):
        raise ValueError("CASE_ID_COUNT_MISMATCH")
    engine = create_engine(settings.database_url, connect_args={
        "connect_timeout": 8,
        "options": "-c default_transaction_read_only=on -c statement_timeout=15000",
    })
    cases, catalogs = [], []
    try:
        with engine.connect() as connection:
            for index, run_id in enumerate(run_ids):
                row = connection.execute(text("""
                    SELECT a.graph_state_json, a.status, a.intent, m.content, m.attachments_json
                    FROM agent_runs a JOIN messages m ON m.id=a.message_id
                    WHERE a.id=:run_id AND a.user_id=:user_id AND m.user_id=:user_id
                """), {"run_id": run_id, "user_id": user_id}).mappings().one_or_none()
                if row is None or row["status"] not in {"COMPLETED", "NEEDS_REVIEW", "FAILED"}:
                    raise ValueError("ROUTING_SOURCE_NOT_READY_OR_NOT_OWNED")
                state = row["graph_state_json"] or {}
                if not state.get("tool_plan") and not state.get("planner_decision") and not state.get("slots"):
                    raise ValueError("ROUTING_SOURCE_PLANNING_MISSING")
                tools = list(connection.execute(text("""
                    SELECT tool_name FROM tool_invocations WHERE agent_run_id=:run_id
                    ORDER BY created_at, id
                """), {"run_id": run_id}).scalars())
                from app.modules.evidence_harness.routing.runtime import classify_route
                entry = tools[0] if tools else "OTHER"
                observed = classify_route(str(row["intent"] or ""), entry)
                catalogs.append(str((state.get("catalog_snapshot") or {}).get("catalog_fingerprint") or "UNKNOWN"))
                cases.append(RoutingCase(
                    case_id=case_ids[index] if case_ids else f"run_{digest(run_id)[:16]}",
                    source_run_ref=digest(run_id), question=row["content"],
                    attachments=_documents(row["attachments_json"] or []),
                    context_documents=_documents(state.get("context_documents") or []),
                    adaptive_mode=state.get("adaptive_planner_mode") or "legacy",
                    observation=SourceSignal(observed_route=observed,
                        entry_tool=entry if entry in {"evidence-answer", "hybrid-search", "managed-file-list"} else "OTHER",
                        evidence_answer_called="evidence-answer" in tools,
                        run_failed=row["status"] == "FAILED"),
                ))
    finally:
        engine.dispose()
    inputs = RoutingInputs(source="real", catalog_snapshot=current_catalog(), cases=cases,
                           source_catalog_refs=catalogs)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_new_json(output / "inputs.json", inputs.model_dump(mode="json"))
    write_new_json(output / "gold-draft.json", {"cases": [RoutingGold(case_id=c.case_id).model_dump() for c in cases]})
    write_new_json(output / "manifest.json", {"stage": "AWAITING_ROUTING_GOLD", "case_count": len(cases),
        "inputs_sha256": digest(inputs.model_dump(mode="json")), "model_calls": 0})
    return {"stage": "AWAITING_ROUTING_GOLD", "case_count": len(cases), "model_calls": 0}


def freeze(*, inputs_path: Path, gold_path: Path, output: Path) -> dict:
    """核验逐题人工确认，另存绑定输入和标签的新数据集。"""
    inputs_path, gold_path, output = map(store._dataset_file, (inputs_path, gold_path, output))
    raw, _ = store._read(inputs_path)
    inputs = RoutingInputs.model_validate(raw)
    annotations, _ = store._read(gold_path)
    if set(annotations) != {"cases"}:
        raise ValueError("ROUTING_GOLD_SCHEMA_INVALID")
    gold = [RoutingGold.model_validate(item) for item in annotations["cases"]]
    dataset = RoutingDataset(inputs=inputs, gold=gold,
        inputs_sha256=digest(inputs.model_dump(mode="json")), gold_sha256=digest([g.model_dump() for g in gold]))
    validate_dataset(dataset)
    write_new_json(output, dataset.model_dump(mode="json"))
    return {"stage": "ROUTING_DATASET_FROZEN", "case_count": len(inputs.cases), "model_calls": 0}


def validate_dataset(dataset: RoutingDataset) -> None:
    """人工标签完整、唯一且绑定输入，缺失或篡改均在模型调用前失败。"""
    if dataset.inputs_sha256 != digest(dataset.inputs.model_dump(mode="json")):
        raise ValueError("ROUTING_INPUT_CHANGED")
    if dataset.gold_sha256 != digest([g.model_dump() for g in dataset.gold]):
        raise ValueError("ROUTING_GOLD_CHANGED")
    ids = [g.case_id for g in dataset.gold]
    if len(set(ids)) != len(ids) or set(ids) != {c.case_id for c in dataset.inputs.cases}:
        raise ValueError("ROUTING_GOLD_CASE_MISMATCH")
    if any(not g.confirmed or g.expected_route is None or not g.reason.strip() for g in dataset.gold):
        raise ValueError("ROUTING_GOLD_NOT_CONFIRMED")


def load_dataset(path: Path) -> RoutingDataset:
    """只读加载私有冻结数据集并验证散列，保留真实与Fake来源区别。"""
    raw, _ = store._read(store._dataset_file(path))
    dataset = RoutingDataset.model_validate(raw)
    validate_dataset(dataset)
    return dataset
