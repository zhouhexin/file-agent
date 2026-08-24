// 前端共享类型与后端 API 响应保持同名字段，降低接口映射成本。

export type User = {
  id: string;
  username: string;
  email: string | null;
  display_name: string;
  role: string;
  default_workspace_id: string | null;
};

export type TokenResponse = {
  access_token: string;
  token_type: 'bearer';
  user: User;
};

// 能力建议只在管理员页面展示脱敏产品缺口，不包含文件正文、Prompt 或 Tool 输入。
export type CapabilitySuggestion = {
  id: string;
  suggestion_kind: 'CAPABILITY' | 'TOOL' | 'SKILL' | string;
  title: string;
  missing_capability: string;
  reason: string;
  expected_inputs_json: string[];
  expected_outputs_json: string[];
  related_skill_ids_json: string[];
  confidence: number;
  occurrence_count: number;
  catalog_fingerprint: string;
  status: 'NEW' | 'UNDER_REVIEW' | 'ACCEPTED' | 'REJECTED' | 'MERGED' | 'IMPLEMENTED' | string;
  review_note: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
};

// 管理员任务诊断只展示业务阶段、中文结论和处置建议，不包含文件正文或模型 Prompt。
export type AdminAgentRun = {
  id: string;
  conversation_id: string;
  user_id: string;
  intent: string | null;
  status: string;
  planner_mode: string;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentDiagnosticEvent = {
  occurred_at: string;
  stage: string;
  event_title: string;
  status: string | null;
  operator_message: string;
  cause_code: string | null;
  recommended_action: string | null;
  duration_ms: number | null;
  tool_name: string | null;
  document_id: string | null;
  document_version_id: string | null;
  filesystem_job_id: string | null;
};

export type AgentRunDiagnostics = {
  run: AdminAgentRun;
  summary: string;
  recommended_actions: string[];
  events: AgentDiagnosticEvent[];
};

// 普通聊天页面只消费稳定任务投影，不依赖 Skill、ToolInvocation 或 Graph 内部结构。
export type TaskResult = {
  task_id: string;
  task_status: 'processing' | 'waiting_confirmation' | 'completed' | 'needs_attention' | 'failed';
  response_type:
    | 'text'
    | 'file_results'
    | 'managed_file_list'
    | 'rename_plan'
    | 'operation_plan'
    | 'async_job'
    | 'file_search_results'
    | 'trash_restore_selection'
    | 'file_search_clarification'
    | 'evidence_answer'
    | 'file_selection'
    | 'classification_clarification'
    | 'classification_decision'
    | 'filename_conflict'
    | 'structured_extraction';
  display_mode: 'default' | 'classification_cards';
  final_response: string | null;
  processed_count: number;
  document_results: DocumentResult[];
  managed_file_result: {
    root_key: string | null;
    root_display_name: string;
    files: ManagedFileResult[];
  } | null;
  rename_plan_result: import('./features/chat/RenameSuggestionReceipt').RenamePlanResult | null;
  file_search_result: FileSearchResult | null;
  search_context: SearchContext | null;
  trash_restore_result: TrashRestoreResult | null;
  file_search_clarification_result: FileSearchClarificationResult | null;
  evidence_answer_result: EvidenceAnswerResult | null;
  file_selection_result: FileSelectionResult | null;
  classification_clarification_result: ClassificationClarificationResult | null;
  classification_decision_result: ClassificationDecisionResult | null;
  filename_conflict_result: FilenameConflictResult | null;
  structured_extraction_result: StructuredExtractionResult | null;
  pending_job_ids: string[];
  operation_plan_id: string | null;
  pending_decisions: Array<Record<string, unknown>>;
  references: Array<Record<string, unknown>>;
  suggested_next_actions: string[];
  presentation: FileTaskPresentation | null;
};

// 所有文件任务共享的展示外壳只包含后端验证后的业务事实；专用明细继续使用各自 payload。
export type FileTaskPresentation = {
  schema_version: 'file-task-receipt.v1';
  task_kind:
    | 'INGEST'
    | 'READ'
    | 'SUMMARIZE'
    | 'ANSWER'
    | 'CLASSIFY'
    | 'SEARCH'
    | 'LIST'
    | 'SPREADSHEET'
    | 'RENAME_SUGGESTION'
    | 'OPERATION_PLAN'
    | 'FILE_OPERATION'
    | 'CLARIFICATION'
    | 'FAILURE';
  title: string;
  phase: {
    code:
      | 'RECEIVED'
      | 'UNDERSTANDING'
      | 'PROCESSING'
      | 'ORGANIZING'
      | 'WAITING_CONFIRMATION'
      | 'COMPLETED'
      | 'NEEDS_ATTENTION'
      | 'FAILED';
    label: string;
  };
  request: {
    target_label: string;
    scope_label: string;
    action_label: string;
    conditions: Array<{
      label: string;
      value: string;
      condition_type: string;
      status: string;
    }>;
  };
  outcome: {
    headline: string;
    total_count: number;
    completed_count: number;
    failed_count: number;
    needs_review_count: number;
    skipped_count: number;
    completeness: 'COMPLETE' | 'PROCESSING' | 'PARTIAL' | 'UNVERIFIABLE';
  };
  change_impact: {
    originals_changed: boolean | null;
    working_copies_changed: boolean | null;
    derivatives_created: number;
    operation_executed: boolean;
    message: string;
  };
  notices: Array<{
    level: 'INFO' | 'WARNING' | 'ERROR';
    message: string;
  }>;
  next_actions: FileTaskNextAction[];
};

export type FileTaskNextAction = {
  id: string;
  label: string;
  action_kind:
    | 'FILL_PROMPT'
    | 'OPEN_FILE'
    | 'RESOLVE_CLARIFICATION'
    | 'CONFIRM_OPERATION'
    | 'LOAD_MORE';
  prompt: string | null;
  target_ref: string | null;
  requires_confirmation: boolean;
};

export type SearchContext = {
  effective_conditions: Array<{
    label: string;
    value: string;
    condition_type: string;
    status: 'APPLIED' | 'SEMANTIC_ONLY' | 'RELAXED' | 'UNSUPPORTED' | 'REJECTED' | string;
    source: string;
  }>;
  attempts: Array<{
    query: string;
    result_count: number;
    result_status: string;
    index_status: string;
  }>;
};

// 两阶段文件搜索结果的普通用户投影。
// 不包含 Skill、Tool、内部路径、SQL 分数或 search_text。
export type FileSearchMatchLocation = {
  page_number?: number | null;
  sheet_name?: string | null;
  cell_range?: string | null;
};

export type FileSearchResultFile = {
  working_copy_id: string | null;
  managed_file_id?: string | null;
  document_id: string;
  document_version_id: string;
  filename: string;
  // 仅允许展示受管根键与逻辑相对路径，禁止接收服务器绝对路径。
  root_key?: string | null;
  relative_path?: string | null;
  category_path: string[];
  year?: number | null;
  overview?: string;
  match_reasons: string[];
  match_location: FileSearchMatchLocation | null;
  evidence_preview: string;
  // 后端基于受控索引和正文证据给出的检索分级，不能由前端自行推断。
  relevance_tier?: 'SUPPORTED' | 'POSSIBLE';
};

export type FileSearchResult = {
  query: string;
  total_returned: number;
  supported_count?: number;
  possible_count?: number;
  partial: boolean;
  user_message: string;
  // 后端基于真实范围和索引状态生成的覆盖结论，前端只负责展示，不能自行判定“找全”。
  search_completeness?: SearchCompleteness;
  show_all_results?: boolean;
  files: FileSearchResultFile[];
};

export type SearchCompleteness = {
  status: 'COMPLETE' | 'PROCESSING' | 'PARTIAL' | 'UNVERIFIABLE';
  can_claim_complete: boolean;
  scope_label: string;
  eligible_file_count: number;
  ready_file_count: number;
  pending_file_count: number;
  failed_file_count: number;
  candidate_limit_reached: boolean;
  message: string;
};

export type TrashRestoreCandidate = {
  trash_entry_id: string;
  display_index: number;
  filename: string;
  size_bytes: number;
  version_number: number;
  deleted_at: string;
  created_at: string;
};

export type TrashRestoreResult = {
  conversation_id: string;
  query_type: 'EXACT_FILENAME';
  requires_selection: true;
  message: string;
  candidates: TrashRestoreCandidate[];
};

export type FileSearchClarificationOption = {
  id: string;
  label: string;
  description: string;
  examples: string[];
  estimated_count: number | null;
};

export type FileSearchClarificationResult = {
  id: string;
  status: 'WAITING_SELECTION' | 'RESOLVED' | 'SUPERSEDED' | 'EXPIRED' | string;
  prompt: string;
  core_phrase: string;
  options: FileSearchClarificationOption[];
  allow_custom_phrase: boolean;
  selection_type: 'DOCUMENT_SELECTION' | 'SEARCH_PHRASE' | string;
  allow_multiple: boolean;
  expires_at: string | null;
};

export type ClassificationClarificationResult = {
  id: string;
  status: 'WAITING_SELECTION' | 'RESOLVED' | 'SUPERSEDED' | 'EXPIRED' | string;
  prompt: string;
  action: 'ACCEPT' | 'REJECT' | 'CORRECT' | string;
  options: Array<{
    id: string;
    filename: string;
    category_label: string;
  }>;
  expires_at: string | null;
};

export type ClassificationDecisionResult = {
  action: string;
  message: string;
  file_position_changed: false;
};

export type FilenameConflictResult = {
  filename: string;
  message: string;
  allowed_decisions: string[];
};

export type EvidenceAnswerFile = {
  document_id: string;
  document_version_id: string;
  working_copy_id: string;
  filename: string;
  category_labels: string[];
  availability: 'AVAILABLE' | string;
  availability_message?: string;
  can_open?: boolean;
  can_restore?: boolean;
  reference_indexes: number[];
  // 已由后端限制长度并完成权限校验的原文片段；不含 Evidence/Chunk 等内部标识。
  // 历史消息尚未保存该字段，前端读取时必须按空数组兼容。
  evidence_items?: EvidenceAnswerEvidenceItem[];
};

export type EvidenceAnswerEvidenceItem = {
  quote: string;
  page_number: number | null;
  sheet_name: string | null;
  cell_range: string | null;
};

export type EvidenceAnswerResult = {
  answer_id: string | null;
  status: string;
  answer: string;
  limitations: string[];
  files: EvidenceAnswerFile[];
  cached: boolean;
};

export type StructuredExtractionField = {
  key: string;
  label: string;
  field_type: string;
  required: boolean;
};

export type StructuredExtractionCell = {
  raw_text: unknown;
  normalized_value: unknown;
  confidence: number;
  status: string;
  evidence: {
    page_number: number | null;
    bbox: Partial<Record<'left' | 'top' | 'right' | 'bottom', number>>;
  };
  warnings: string[];
};

export type StructuredExtractionResult = {
  document_id: string;
  presentation: 'AUTO' | 'TABLE' | 'JSON' | 'CSV' | 'XLSX' | 'TEXT' | string;
  schema_mode: string;
  record_mode: string;
  field_schema: StructuredExtractionField[];
  records: Array<{
    record_index: number;
    fields: Record<string, StructuredExtractionCell>;
  }>;
  review_items: Array<{
    record_index: number;
    field_key: string;
    field_label: string;
    raw_text: unknown;
    status: string;
    reason_codes: string[];
    page_number: number | null;
  }>;
  record_count: number;
  review_count: number;
  quality_band: 'HIGH' | 'MEDIUM' | 'LOW' | string;
  original_unchanged: true;
  export_artifact: {
    artifact_id: string;
    format: 'CSV' | 'XLSX';
    filename: string;
    content_type: string;
    size_bytes: number;
  } | null;
};

export type FileSelectionChoice = {
  option_id: string;
  document_id: string;
  document_version_id: string;
  working_copy_id: string;
  filename: string;
  size_bytes: number;
  created_at: string;
  // 历史消息中的同名选择卡尚未写入这两个字段，前端必须兼容展示。
  suggested_category_labels?: string[];
  directory_path?: string;
};

export type FileSelectionResult = {
  clarification_id: string;
  message: string;
  choices: FileSelectionChoice[];
};

export type OperationPlanItem = {
  document_id: string;
  working_copy_id?: string | null;
  operation?: string | null;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  rename_metadata: Record<string, unknown>;
  execution_status: string;
};

export type OperationPlanResponse = {
  id: string;
  conversation_id: string;
  user_id: string;
  operation_type: string;
  status: string;
  requires_confirmation: boolean;
  risk_level: string;
  reason: string;
  items: OperationPlanItem[];
  total_item_count: number;
  items_truncated: boolean;
  skipped_items: Array<Record<string, unknown>>;
  scope?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  confirmed_at: string | null;
  executed_at: string | null;
};

export type OperationConfirmResponse = {
  id: string;
  status: string;
  changeset_id: string | null;
  result: Record<string, unknown>;
};

export type RenameBatchItem = {
  id: string;
  managed_file_id: string;
  root_key: string;
  original_relative_path: string;
  original_filename: string;
  proposed_filename: string | null;
  status: string;
  position: number;
  warnings: string[];
};

export type RenameBatchItemsResponse = {
  items: RenameBatchItem[];
  next_cursor: number | null;
};

// 受管文件结果只包含逻辑 root 与相对路径，前端不能接触服务器绝对路径。
export type ManagedFileResult = {
  root_key: string;
  display_name: string;
  relative_path: string;
  category_path: string | null;
  filename: string;
  extension: string;
  size_bytes: number;
  modified_at: string | null;
  status: string;
};

export type UploadedFile = {
  document_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  status: string;
  ingest_status: string;
  deduplicated: boolean;
  upload_document_version_id?: string;
  duplicate_review_id?: string;
  filesystem_job_id?: string;
  archive_status?: string;
  duplicate_review_status?: string;
  working_copy_id?: string | null;
  working_copy_status?: string | null;
  file_availability?: 'AVAILABLE' | 'TRASHED' | 'PROCESSING' | 'MISSING' | 'UNAVAILABLE' | string;
  availability_message?: string | null;
  can_open?: boolean;
  can_restore?: boolean;
};

export type FilePreviewSection = {
  page_number: number | null;
  sheet_name: string | null;
  text: string;
};

export type FilePreviewResponse = {
  document_id: string;
  filename: string;
  content_type: string;
  sections: FilePreviewSection[];
  truncated: boolean;
};

export type DuplicateCandidate = {
  id: string;
  match_type: 'EXACT_SHA256' | 'NEAR_DUPLICATE' | string;
  match_scope: 'SAME_WORKSPACE' | 'SAME_USER' | 'CROSS_USER' | string;
  similarity_score: number;
  summary: Record<string, unknown>;
  existing_working_copy_id: string | null;
  existing_document_id: string | null;
};

export type DuplicateReview = {
  id: string;
  upload_document_version_id: string;
  document_id: string;
  filename: string;
  status: string;
  decision: string | null;
  expires_at: string;
  candidates: DuplicateCandidate[];
  allowed_decisions: string[];
  duplicate_check_job_id: string | null;
};

export type DuplicateDecisionResponse = {
  review: DuplicateReview;
  archive_status: string;
  filesystem_job_id: string | null;
  selected_existing_document_id: string | null;
};

export type UploadArchiveStatus = {
  upload_document_version_id: string;
  status: string;
  managed_file_id: string | null;
  working_copy_id: string | null;
  filesystem_job_id: string | null;
  error_code: string | null;
  error_message: string | null;
};

export type ConversationHistoryMessage = {
  id: string;
  conversation_id: string;
  user_id: string;
  role: string;
  content: string;
  attachments: UploadedFile[];
  metadata: Record<string, unknown>[];
  task_result: TaskResult | null;
};

export type ConversationDetailResponse = {
  id: string;
  user_id: string;
  title: string;
  status: string;
  messages: ConversationHistoryMessage[];
  pagination: {
    has_more: boolean;
    oldest_message_id: string | null;
    limit: number;
  };
};

export type SendMessageResponse = {
  message: {
    id: string;
    conversation_id: string;
    user_id: string;
    role: string;
    content: string;
    attachments: { document_id: string }[];
  };
  task_result: TaskResult;
};

export type FilesystemJobResponse = {
  id: string;
  job_type: string;
  queue_name: string;
  root_id: string | null;
  status: string;
  progress_current: number;
  progress_total: number;
  result: Record<string, unknown>;
  error_message: string | null;
  attempt_count: number;
  max_attempts: number;
  available_at: string;
  started_at: string | null;
  finished_at: string | null;
};

export type FailedFileJob = {
  job_id: string;
  job_type: string;
  queue_name: string;
  filename: string;
  root_key: string | null;
  relative_path: string | null;
  attempt_count: number;
  max_attempts: number;
  error_message: string | null;
  error_reference: string | null;
  created_at: string;
  finished_at: string | null;
};

export type DocumentCategory = {
  suggestion_id?: string;
  category_id?: string;
  name: string;
  category_path?: string[];
  confidence: number;
  evidence: string[];
  status?: 'SUGGESTED' | 'CONFIRMED' | string;
  source?: string;
  taxonomy_version?: string;
  candidate_scores?: Record<string, number>;
  semantic_evidence?: {
    support_count?: number;
    similarity_bucket?: string;
    source?: string;
  };
};

export type ClassificationFeedbackResponse = {
  id: string;
  suggestion_id: string;
  document_id: string;
  document_version_id?: string | null;
  working_copy_id?: string | null;
  action: 'ACCEPTED' | 'REJECTED' | 'CORRECTED' | string;
  corrected_category_id?: string | null;
  corrected_category_path: string[];
  positive_category_ids: string[];
  negative_category_ids: string[];
  changeset_id?: string | null;
  file_position_changed?: boolean;
  user_message?: string;
  created_at: string;
};

export type ClassificationTaxonomyOption = {
  category_id: string;
  label: string;
  path: string[];
};

export type ClassificationTaxonomyOptionsResponse = {
  taxonomy_key: string;
  taxonomy_version: string;
  options: ClassificationTaxonomyOption[];
};

export type DocumentResult = {
  document_id: string;
  document_version_id?: string;
  working_copy_id?: string;
  filename: string;
  organization_status?: 'READY' | 'NEEDS_REVIEW' | string;
  /** 用户可理解的原文检索准备状态，不暴露内部索引、Skill 或 Tool。 */
  search_status?: 'READY' | 'NEEDS_REVIEW' | string;
  /** 当前文件可定位证据数量，只用于说明检索准备度。 */
  evidence_count?: number;
  extraction_status: 'COMPLETED' | 'FAILED' | string;
  extractor?: string;
  page_count: number;
  char_count: number;
  text_reused: boolean;
  classification_reused: boolean;
  year?: string | null;
  /** 仅供展示的命名建议；文件尚未改名，用户仍需明确发起重命名。 */
  rename_suggestion?: { proposed_filename?: string } | null;
  document_type?: string | null;
  keywords?: string[];
  entities?: string[];
  managed_original_unchanged?: boolean;
  risk_warnings?: Array<{ code?: string; message?: string }>;
  pending_decision?: Record<string, unknown> | null;
  categories: DocumentCategory[];
  warnings: Array<Record<string, unknown> | string>;
  errors: Array<{
    code?: string;
    message?: string;
  }>;
};

export type AgentCapability = {
  id: string;
  name: string;
  description: string;
  examples: string[];
};

export type AgentCapabilityCatalog = {
  ok: boolean;
  version: string;
  capabilities: AgentCapability[];
};
