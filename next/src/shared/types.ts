export interface LogEntry {
  ts: string;
  text: string;
}

export interface UiRow {
  index: number;
  text: string;
  desc: string;
  resourceId: string;
  className: string;
  package: string;
  clickable: boolean;
  enabled: boolean;
  visible: boolean;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  cx: number;
  cy: number;
  w: number;
  h: number;
}

export interface DeviceState {
  connected: boolean;
  mode: "u2" | "adb" | "";
  label: string;
  status: string;
  rows: UiRow[];
  screen: [number, number];
  taskRunning: boolean;
  taskProgress: LogEntry[];
  autoConnect: boolean;
}

export interface StreamStatus {
  status: string;
  h264: boolean;
  h264Fps: number;
  wsUrl: string;
  mjpegUrl: string;
  clear: boolean;
}

export type ActionKind =
  | "click_ratio" | "click_match" | "back" | "scroll" | "wait"
  | "finish" | "mute_once" | "close_ad" | "capture" | "loop";

export interface RecordedStep {
  id: string;
  kind: ActionKind;
  detail: string;
  target: string;
  ratio?: [number, number];
  x?: number;
  y?: number;
  texts?: string[];
  label?: string;
  match?: string;
  repeat?: number;
  body?: RecordedStep[];
}

export interface TaskState {
  id: string;
  match?: string[] | string;
  match_any?: unknown;
  match_all?: unknown;
  exclude?: string[] | string;
  action: string;
  fallback?: boolean;
  [key: string]: unknown;
}

export interface TaskData {
  key: string;
  name: string;
  description: string;
  app: string;
  rule: string;
  flow: TaskState[];
  engine: "configured" | "legacy";
  enabled: boolean;
  max_loops: number;
  capabilities: Record<string, boolean>;
  states: Record<string, unknown>;
  stuck_states: string[];
  stuck_limit: number;
  finish_info_keywords: string[];
  poll: number;
  tap_gap: number;
  updated: string;
}

export interface FlowSpec {
  $schema: string;
  version: string;
  meta: Record<string, unknown>;
  settings: Record<string, unknown>;
  locators: Record<string, unknown>;
  flow: Record<string, unknown>;
  observability: Record<string, unknown>;
}

export interface AnalysisResult {
  total: number;
  executed: number;
  failed: number;
  successRate: number;
  actionStats: Record<string, number>;
  resultStats: Record<string, number>;
  sourceStats: Record<string, number>;
  events: Array<Record<string, unknown>>;
}
