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
    | 'file_search_results';
  final_response: string | null;
  processed_count: number;
  document_results: DocumentResult[];
  managed_file_result: { root_key: string; files: ManagedFileResult[] } | null;
  rename_plan_result: import('./features/chat/RenameSuggestionReceipt').RenamePlanResult | null;
  file_search_result: FileSearchResult | null;
  pending_job_ids: string[];
  operation_plan_id: string | null;
  pending_decisions: Array<Record<string, unknown>>;
  references: Array<Record<string, unknown>>;
  suggested_next_actions: string[];
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
  document_id: string;
  document_version_id: string;
  filename: string;
  category_path: string[];
  year?: number | null;
  overview?: string;
  match_reasons: string[];
  match_location: FileSearchMatchLocation | null;
  evidence_preview: string;
};

export type FileSearchResult = {
  query: string;
  total_returned: number;
  partial: boolean;
  user_message: string;
  files: FileSearchResultFile[];
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
  action: 'ACCEPTED' | 'REJECTED' | 'CORRECTED' | string;
  corrected_category_id?: string | null;
  corrected_category_path: string[];
  positive_category_ids: string[];
  negative_category_ids: string[];
  created_at: string;
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
