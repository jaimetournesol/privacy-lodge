/**
 * Surface wire protocol — shared between hub (server/) and shell (web/).
 *
 * The hub owns one canonical SurfaceState document. Clients receive a full
 * snapshot on (re)connect and RFC 6902 JSON-Patch deltas afterwards, tagged
 * with the document version they produce. Human input travels up as typed
 * messages — never free text mixed into the state doc.
 */

export type Region = 'main' | 'side';

export type ComponentType =
  | 'markdown'
  | 'code'
  | 'table'
  | 'form'
  | 'tasks'
  | 'stat'
  | 'chart'
  | 'image'
  | 'video'
  | 'audio'
  | 'file'
  | 'diagram'
  | 'html'
  | 'app';

export interface SurfaceComponent {
  id: string;
  type: ComponentType;
  title?: string;
  region: Region;
  order: number;
  /** Type-specific payload; see registry component for each type's shape. */
  props: Record<string, unknown>;
  /** Soft-closed by the human — kept in state, not rendered. */
  hidden?: boolean;
  /** Bumped on every mutation — used for cheap memoization client-side. */
  v: number;
  createdAt: number;
  updatedAt: number;
}

export type AgentPhase =
  | 'starting'
  | 'idle'
  | 'thinking'
  | 'responding'
  | 'tool'
  | 'stopped'
  | 'error';

export interface AgentStatus {
  phase: AgentPhase;
  /** Tool currently executing, when phase === 'tool'. */
  activeTool?: string;
  model: string;
  sessionId?: string;
  costUsd: number;
  /** Cumulative tokens (prompt + completion) across turns — the meaningful meter for a self-hosted model. */
  tokens?: number;
  turns: number;
  lastError?: string;
  /** A persistent streaming session is attached (instant dispatch). */
  warm?: boolean;
}

export interface ChatAttachment {
  name: string;
  size: number;
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  /** True while the hub is still appending streamed text. */
  streaming?: boolean;
  ts: number;
  attachments?: ChatAttachment[];
}

export type LogLevel = 'debug' | 'info' | 'success' | 'warn' | 'error' | 'tool';

export interface LogEntry {
  id: string;
  ts: number;
  level: LogLevel;
  source: 'agent' | 'hub' | 'human';
  message: string;
  data?: unknown;
}

export interface SurfaceState {
  view?: { expandedId: string | null; presentations?: Record<string,string|null> };
  v: number;
  components: SurfaceComponent[];
  chat: ChatMessage[];
  log: LogEntry[];
  agent: AgentStatus;
}

export interface WorkspaceInfo {
  id: string;
  name: string;
  dir: string;
  /** Attached pre-existing folder (delete only detaches) vs created/managed. */
  attached: boolean;
  /** Runtime summary for the home screen / switcher badges. */
  phase?: AgentPhase;
  costUsd?: number;
  panels?: number;
  lastActive?: number;
}

export interface FileEntry {
  name: string;
  kind: 'dir' | 'file';
  size: number;
  mtime: number;
}

/** Minimal RFC 6902 subset the hub emits. */
export interface PatchOp {
  op: 'add' | 'remove' | 'replace';
  path: string;
  value?: unknown;
}

export type ServerMsg =
  | { type: 'snapshot'; state: SurfaceState; workspace: WorkspaceInfo }
  | { type: 'patch'; v: number; ops: PatchOp[] }
  | { type: 'app-reload'; componentId: string }
  | { type: 'workspaces'; items: WorkspaceInfo[] }
  /** An agent in some (possibly background) workspace went idle/error after working. */
  | { type: 'ws-activity'; id: string; name: string; phase: AgentPhase }
  /** Hub asks this client to rasterize the workspace area (agent self-sight). */
  | { type: 'capture-request'; reqId: string };

/** A frozen-panel annotation attached to a chat message. */
export interface ChatAnnotation {
  componentId: string;
  componentType: string;
  title?: string;
  /** Normalized (0..1) bounding boxes of the drawn marks. */
  marks: { x: number; y: number; w: number; h: number }[];
  /** PNG of the frozen snapshot with the marks drawn on it, as a data URL. */
  imageDataUrl?: string;
}

export type ClientMsg =
  | { type: 'chat'; text: string; annotations?: ChatAnnotation[] }
  | { type: 'ui-event'; componentId: string; action: string; payload?: unknown }
  | { type: 'interrupt' }
  | { type: 'clear-workspace' }
  | { type: 'reset-agent' }
  | { type: 'switch-workspace'; id: string }
  /** Human rearranged panels: full ordered id lists per region. */
  | { type: 'ui-arrange'; main: string[]; side: string[] }
  /** Size of this viewer's workspace area (agent awareness of the canvas it composes on). */
  | { type: 'viewport'; width: number; height: number; dpr: number }
  | { type: 'resync' };

/** postMessage envelope used by iframe panels (apps + html) → shell. */
export interface BridgeEvent {
  __surface: true;
  componentId: string;
  action: string;
  payload?: unknown;
}

export const HUB_PORT = 4400;
