export type RunStatus =
  | "created"
  | "planning"
  | "running"
  | "executing"
  | "verifying"
  | "paused"
  | "needs_approval"
  | "blocked"
  | "completed"
  | "failed"
  | "rejected"
  | "aborted";

export type HarnessEvent = {
  sequence: number;
  at: string;
  kind: string;
  data: Record<string, unknown>;
};

export type RunSummary = {
  run_id: string;
  task: string;
  status: RunStatus;
  origin: "managed" | "direct_mcp";
  model_provider?: string | null;
  caller?: Record<string, unknown>;
  session_id?: string | null;
  created_at: string;
  updated_at: string;
  event_count: number;
  event_cursor: number;
  error?: string | null;
};

export type RunSnapshot = RunSummary & {
  plan?: {
    summary: string;
    steps: string[];
    success_criteria: string[];
    constraints: string[];
  } | null;
  operator_guidance: string[];
  observation?: {
    session_id: string;
    status: string;
    machine: Record<string, unknown>;
    frame_id?: number | null;
    world_version?: number | null;
    control_epoch?: number | null;
    width?: number | null;
    height?: number | null;
  } | null;
  pending_action?: {
    index: number;
    intent: string;
    actions: Array<Record<string, unknown>>;
    expected_evidence: string[];
    based_on_world_version: number;
    based_on_control_epoch: number;
    idempotency_key: string;
    attempts: number;
  } | null;
  pending_approval?: Record<string, unknown> | null;
  last_verification?: Record<string, unknown> | null;
  active_activity?: {
    kind: "model" | "tool";
    started_at: string;
    role?: string | null;
    provider?: string | null;
    model?: string | null;
    attempt?: number | null;
    tool?: string | null;
    call_id?: string | null;
    arguments?: Record<string, unknown>;
  } | null;
  events: HarnessEvent[];
  events_truncated: boolean;
};

export type ProviderHealth = {
  kind?: string;
  configured_model?: string;
  ready?: boolean;
  support_tier?: string;
  auth_mode?: string;
  credential_owner?: string;
  calls?: number;
  successes?: number;
  failures?: number;
  last_latency_ms?: number | null;
  last_model?: string | null;
};

export type ProviderMap = Record<string, ProviderHealth>;
