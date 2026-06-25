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
