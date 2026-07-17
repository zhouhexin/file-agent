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

export type AgentRun = {
  agent_run_id: string;
  conversation_id: string;
  user_id: string;
  message_id: string;
  intent: string | null;
  status: string;
  selected_skills: string[];
  final_response: string | null;
  tool_invocations: ToolInvocation[];
  document_results: DocumentResult[];
  async_job_ids: string[];
  changeset_id: string | null;
  operation_plan_id: string | null;
};

export type ChangeItem = {
  id: string;
  target_type: string;
  target_id: string | null;
  target_document_id: string | null;
  change_type: string;
  before_value_json: Record<string, unknown>;
  after_value_json: Record<string, unknown>;
  source: string;
  confidence: number;
  evidence_json: Record<string, unknown>;
  execution_status: string;
  created_at: string;
};

export type ChangeSetResponse = {
  id: string;
  conversation_id: string;
  agent_run_id: string;
  user_id: string;
  status: string;
  summary: string;
  created_at: string;
  updated_at: string;
  items: ChangeItem[];
};

export type OperationPlanItem = {
  document_id: string;
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

export type ToolInvocation = {
  id: string;
  tool_name: string;
  status: string;
  input_json: Record<string, unknown>;
  output_json: Record<string, unknown>;
  changeset_id: string | null;
  operation_plan_id: string | null;
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
};

export type ConversationHistoryMessage = {
  id: string;
  conversation_id: string;
  user_id: string;
  role: string;
  content: string;
  attachments: UploadedFile[];
  agent_run: AgentRun | null;
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
  agent_run: AgentRun;
};

export type FilesystemJobResponse = {
  id: string;
  job_type: string;
  root_id: string | null;
  status: string;
  progress_current: number;
  progress_total: number;
  result: Record<string, unknown>;
  error_message: string | null;
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
  filename: string;
  extraction_status: 'COMPLETED' | 'FAILED' | string;
  extractor?: string;
  page_count: number;
  char_count: number;
  text_reused: boolean;
  classification_reused: boolean;
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
