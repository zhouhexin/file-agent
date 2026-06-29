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

export type ToolInvocation = {
  id: string;
  tool_name: string;
  status: string;
  input_json: Record<string, unknown>;
  output_json: Record<string, unknown>;
  changeset_id: string | null;
  operation_plan_id: string | null;
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

export type DocumentCategory = {
  name: string;
  category_path?: string[];
  confidence: number;
  evidence: string[];
  status?: 'SUGGESTED' | 'CONFIRMED' | string;
  source?: string;
  taxonomy_version?: string;
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
