export type StageStatus =
  | 'complete'
  | 'in_progress'
  | 'blocked'
  | 'awaiting_approval'
  | 'waiting'
  | 'uncertain'
  | 'failed'
  | 'not_started'
  | 'pending'
  | 'not_requested'
  | 'running';

export type WorkflowStatus = 'complete' | 'in_progress' | 'blocked' | 'at_risk' | 'failed';

export interface PendingApproval {
  id: string;
  agent: string;
  reference: string;
  role: string;
  description: string;
  stage: string;
  gate: string;
}

export interface StageError {
  code: string;
  detail: string;
  retryable: boolean;
}

export interface OnboardingCase {
  case_id: string;
  client_id: string;
  employee_id: string;
  employee_name: string;
  job_title: string;
  department: string;
  location: string;
  joining_date: string;
  current_stage: string;
  overall_status: WorkflowStatus;
  next_action: string;
  last_updated: string;
  created_at: string;
  offer_reference: string;
  stage_statuses: Record<string, StageStatus>;
  pending_approvals: PendingApproval[];
  stage_errors: Record<string, StageError>;
  orientation?: {
    uc05_case: boolean;
    uc05_employee_id: string;
    session_id?: string;
    session_status?: string;
    scheduled_start?: string;
    approval_id?: string;
    approval_status?: string;
  };
  buddy_assignment?: {
    proposed_buddy_id?: string;
    buddy_id?: string;
    approval_id?: string;
    status: string;
  };
  welcome_message?: {
    draft_id?: string;
    draft_status?: string;
    delivery?: string;
  };
  documents?: {
    submitted: string[];
    pending: string[];
    invalid: string[];
  };
  tasks?: {
    total: number;
    completed: number;
    items: Array<{ id: string; title: string; owner: string; due_date: string; status: string }>;
  };
  equipment?: {
    items: Array<{ name: string; status: string }>;
    access: Array<{ system: string; status: string }>;
  };
}

export interface AgentStep {
  id: string;
  name: string;
  purpose: string;
  status: StageStatus;
  input: Record<string, unknown>;
  processing: string[];
  output: Record<string, unknown>;
  error?: string;
  warning?: string;
  duration?: string;
}

export interface AgentExecution {
  execution_id: string;
  case_id: string;
  agent_name: string;
  uc_number: string;
  description: string;
  status: StageStatus;
  start_time: string;
  end_time?: string;
  duration?: string;
  steps: AgentStep[];
  input_summary: Record<string, unknown>;
  output_summary: Record<string, unknown>;
  history: Array<{ timestamp: string; status: StageStatus; note: string }>;
}
