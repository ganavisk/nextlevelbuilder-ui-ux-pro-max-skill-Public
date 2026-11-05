// Mirrors the FastAPI response schemas in apps/api/src/app/modules/*/schemas.py.

export interface Page<T> {
  items: T[];
  total: number;
}

export type Role = "viewer" | "admin" | "owner";

export interface User {
  id: string;
  email: string;
  display_name: string;
  role: Role;
}

export interface CallbackPromisesPreview {
  /** What {next_open} says for a caller who rang after today's closing; null without hours. */
  next_open_example: string | null;
}

/** Whether the phone agent callers reach has the saved settings. */
export interface VoiceSync {
  state: "live" | "updating" | "failed" | "not_connected";
  synced_at: string | null;
  error: string;
}

export interface Tenant {
  id: string;
  slug: string;
  name: string;
  vertical: string;
  timezone: string;
  business_hours: Record<string, unknown>;
  settings: Record<string, unknown>;
  status: string;
  created_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  tenant: Tenant;
  user: User;
}

export interface AgentConfig {
  id: string;
  version: number;
  is_active: boolean;
  system_prompt: string;
  first_message: string;
  voice_id: string;
  voice_provider: string;
  voice_model: string;
  language: string;
  qualification: unknown[];
  escalation_policy: Record<string, unknown>;
}

export interface AgentConfigUpdate {
  system_prompt?: string;
  first_message?: string;
  voice_id?: string;
}

export interface Integration {
  provider: string;
  status: string;
  connected: boolean;
}

export interface CallSummary {
  id: string;
  caller_e164: string;
  direction: string;
  started_at: string;
  ended_at: string | null;
  outcome: string | null;
  sentiment: string | null;
  summary: string | null;
}

export interface TranscriptTurn {
  role: string;
  text: string;
}

export interface CallDetail extends CallSummary {
  recording_url: string | null;
  vendor_cost_cents: number;
  llm_cost_cents: number;
  transcript: TranscriptTurn[];
}

export interface Appointment {
  id: string;
  service: string;
  starts_at: string;
  ends_at: string;
  status: string;
  customer_name: string;
  customer_phone: string;
  customer_email: string | null;
  timezone: string;
  bay: number;
  details: Record<string, string>;
  /** Booked from a Test Lab call, not by a customer. */
  simulated: boolean;
}

export interface CallbackRequest {
  id: string;
  caller_e164: string;
  /** Who to ask for; "" when the caller didn't say. */
  caller_name: string;
  /** Dial this instead of caller_e164 when set: the caller asked to be called back here. */
  callback_e164: string | null;
  reason: string;
  preferred_window: string | null;
  status: string;
}

export interface CrmSync {
  id: string;
  call_id: string | null;
  provider: string;
  status: string;
  attempts: number;
  last_error: string | null;
  created_at: string;
}

export interface AnalyticsSummary {
  window_days: number;
  total_calls: number;
  classified_calls: number;
  calls_by_outcome: Record<string, number>;
  calls_by_sentiment: Record<string, number>;
  booking_rate: number;
  escalation_rate: number;
  daily_call_volume: { date: string; count: number }[];
}

// ── Test Lab (apps/api/src/app/modules/testlab/schemas.py) ─────────────────

export interface LabStatus {
  llm_available: boolean;
  tools: string[];
  bays: number;
  timezone: string;
  default_caller: string;
  services: string[];
  booking_fields: { key: string; description: string }[];
}

export interface LabSession {
  session_id: string;
  caller_e164: string;
  first_message: string;
  started_at: string;
}

export interface ToolEvent {
  tool: string;
  arguments: Record<string, unknown>;
  result: string;
  latency_ms: number;
}

export interface LabTurn {
  reply: string;
  tool_events: ToolEvent[];
  model: string;
  input_tokens: number;
  output_tokens: number;
}

export interface BoardAppointment {
  id: string;
  bay: number;
  starts_at: string;
  ends_at: string;
  service: string;
  customer_name: string;
  details: Record<string, string>;
  simulated: boolean;
}

export interface Board {
  day: string;
  timezone: string;
  bays: number;
  opening_hours: string[][];
  appointments: BoardAppointment[];
}

export interface ActivityItem {
  id: string;
  occurred_at: string;
  event_type: string;
  call_id: string;
  caller_e164: string;
  simulated: boolean;
  tool: string | null;
  summary: string;
  latency_ms: number | null;
}
