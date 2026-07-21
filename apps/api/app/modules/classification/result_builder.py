"""把文档解析结果统一转换为逐文件分类回执结构。"""

from __future__ import annotations

from typing import Any


def build_document_results_from_extraction_results(
    *,
    extraction_results: list[dict[str, Any]],
    context_documents: list[dict[str, Any]],
    classification_service,
    include_categories: bool,
) -> list[dict[str, Any]]:
    """构造同步 Graph 和异步 Worker 共用的 document_results。"""

    document_lookup = {
        str(document.get("document_id")): document
        for document in context_documents
        if document.get("document_id")
    }
    document_results: list[dict[str, Any]] = []
    for result in extraction_results:
        document_id = str(result.get("document_id") or "")
        document_context = document_lookup.get(document_id, {})
        managed_file = result.get("managed_file") if isinstance(result.get("managed_file"), dict) else {}
        pages = [page for page in result.get("pages", []) if isinstance(page, dict)]
        char_count = sum(int(page.get("char_count", 0) or 0) for page in pages)
        text_preview = "\n".join(str(page.get("text_preview") or "") for page in pages)
        error = result.get("error") if isinstance(result.get("error"), dict) else None
        classification_result = (
            classification_service.classify(
                document_id=document_id,
                extraction_run_id=str(result.get("extraction_run_id") or ""),
                filename=str(
                    document_context.get("filename")
                    or managed_file.get("filename")
                    or ""
                ),
                fallback_text=text_preview,
                force_reprocess=bool(result.get("classification_force_reprocess", False)),
            )
            if include_categories and result.get("status") == "COMPLETED"
            else {}
        )
        categories = classification_result.get("categories", [])
        document_results.append(
            {
                "document_id": document_id,
                "filename": (
                    document_context.get("filename")
                    or managed_file.get("filename")
                    or document_id
                ),
                "extraction_status": result.get("status"),
                "extractor": result.get("extractor"),
                "read_quality": result.get("read_quality"),
                "read_profile": result.get("read_profile") or {},
                "page_count": len(pages),
                "char_count": char_count,
                "text_reused": bool(result.get("reused")),
                "classification_reused": bool(
                    classification_result.get("classification_reused", False)
                ),
                "document_version_id": (
                    classification_result.get("document_version_id") or document_id
                ),
                "document_summary_id": classification_result.get("document_summary_id"),
                "classification_summary_id": classification_result.get("classification_summary_id"),
                "summary_status": classification_result.get("summary_status"),
                "source_kind": result.get("source_kind"),
                "managed_file_id": result.get("managed_file_id"),
                "root_key": result.get("root_key"),
                "relative_path": result.get("relative_path"),
                "snapshot_id": result.get("snapshot_id"),
                "snapshot_status": result.get("snapshot_status"),
                "source_sha256": result.get("source_sha256"),
                "source": result.get("source"),
                "conversion_artifact_id": result.get("conversion_artifact_id"),
                "conversion_reused": result.get("conversion_reused"),
                "conversion_source_format": result.get("conversion_source_format"),
                "conversion_parsed_format": result.get("conversion_parsed_format"),
                "conversion_converter": result.get("conversion_converter"),
                "conversion_converter_version": result.get("conversion_converter_version"),
                "categories": categories,
                "warnings": list(result.get("warnings") or []),
                "errors": [error] if error else [],
            }
        )
    return document_results


def format_document_results_response(document_results: list[dict[str, Any]]) -> str:
    """生成可用于异步任务完成回写的简洁逐文件回执。"""

    blocks = [f"已处理 {len(document_results)} 个文件："]
    for index, result in enumerate(document_results, start=1):
        filename = result.get("filename") or result.get("document_id") or "未知文件"
        if result.get("extraction_status") == "FAILED":
            error = (result.get("errors") or [{}])[0]
            message = error.get("message") if isinstance(error, dict) else "未知错误"
            blocks.append(f"{index}. {filename}\n处理失败：{message}")
            continue
        category_names = [
            str(category.get("name") or "其他")
            for category in result.get("categories", [])
            if isinstance(category, dict)
        ]
        category_text = "、".join(category_names) if category_names else "待复核"
        blocks.append(f"{index}. {filename}\n分类建议：{category_text}")
    return "\n\n".join(blocks)
