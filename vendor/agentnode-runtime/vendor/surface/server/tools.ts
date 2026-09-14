import path from 'node:path';
import type { ComponentType, LogLevel, Region } from '../shared/protocol.ts';
import type { AssetHub } from './assets.ts';
import type { SurfaceStore } from './state.ts';

/**
 * The surface tool registry — single source of truth for the agent's UI
 * vocabulary. The hub executes these; a thin stdio MCP proxy (mcp-stdio.mjs,
 * spawned by the `claude -p` turn) forwards tools/call here over localhost.
 */

const COMPONENT_TYPES = [
  'markdown', 'code', 'table', 'form', 'tasks', 'stat', 'chart',
  'image', 'video', 'audio', 'file', 'diagram', 'html', 'app',
] as const;

const PROPS_CHEATSHEET = `Component types and their props:
- markdown: { text }                                  — rich text card (GFM)
- code:     { code, lang? }                           — highlighted code block
- table:    { columns: string[], rows: any[][], selectable?: boolean } — row clicks arrive as ui-events when selectable
- form:     { fields: [{ key, label, kind: text|textarea|number|select|checkbox|slider, options?: string[], min?, max?, step?, value?, placeholder? }], submitLabel? } — submit + live values arrive as ui-events
- tasks:    { items: [{ label, status: todo|doing|done }] } — show your plan/progress
- stat:     { items: [{ label, value, unit?, delta? }] } — KPI tiles
- chart:    { kind: line|area|bar|scatter|pie, labels?: string[], series: [{ name, data: number[], color? }], stacked?, unit?, height? } — themed chart (line/area/bar share labels as x axis)
- image:    { path }  — local file path; the hub serves it
- video:    { path }  — local file path; streamed with scrubbing
- audio:    { path }  — local audio file; player with scrubbing
- file:     { path }  — offers the file as a download card
- diagram:  { mermaid } — mermaid source, rendered dark
- html:     { html, height? } — sandboxed iframe for small one-offs; window.surface.emit(action, payload) sends events back to you
- app:      { path, height? }  — directory containing index.html; prefer ui_app instead

height for html/app panels: a number in px, or "fill" to take the whole visible workspace area (use "fill" for presentations, dashboards, games and any single-panel layout — the human's area size is reported by ui_list and in [surface-context]).`;

const APP_GUIDE = `Building apps that work well:
- LAYOUT: the panel is a viewport of unknown size (the human may be on a laptop, a 4K monitor or a TV). Use height "fill" for single-panel experiences, make the page fill its frame (html,body {height:100%; margin:0}) and CENTER content with flex/grid — never hard-code a canvas size or leave content pinned top-left. ui_list tells you the current viewer area in px.
- PRESENTATIONS: register every deck. use a Surface HTML/app deck with height "fill". Register window.surface.presentation.register({id:"stable-deck-id",count:steps.length,render:index=>showStep(index)}). Include fragment reveals in steps. The runtime supplies phone-sized Previous/Next controls, swipe, keyboard navigation and shared presenter position. render only paints; route custom navigation through the returned next(), previous(), go(index). Never promise keyboard-only navigation. Verify phone and presenter advance together. Arbitrary app-local input and scrolling do not synchronize automatically.
- KEYBOARD: the frame is focused automatically when the pointer is over it, so listen for keys on window (keydown) and call window.focus() on load. Also provide on-screen buttons for every key action (touch/TV remote users).
- THEME: dark shell; use the CSS vars --bg, --panel, --panel2, --border, --text, --muted, --accent so it blends in.
- EVENTS: window.surface.emit(action, payload) reaches you as a [ui-event]; prefix high-frequency actions with ~ (e.g. ~slide) so they become passive context instead of waking you.
- CHECK: after mounting, call ui_screenshot and Read the PNG to verify layout before telling the human it is done.`;

type JsonSchema = Record<string, unknown>;

export interface ToolDef {
  name: string;
  description: string;
  inputSchema: JsonSchema;
}

const str = (description?: string): JsonSchema => ({ type: 'string', ...(description ? { description } : {}) });
const region: JsonSchema = { type: 'string', enum: ['main', 'side'], description: 'Workspace region (default main)' };

export const TOOL_DEFS: ToolDef[] = [
  {
    name: 'ui_show',
    description: `Display a component on the Surface workspace (or fully replace one by passing its id — this always brings a hidden panel back on stage). Returns the component id. ${PROPS_CHEATSHEET}`,
    inputSchema: {
      type: 'object',
      properties: {
        type: { type: 'string', enum: [...COMPONENT_TYPES], description: 'Component type' },
        props: { type: 'object', description: 'Type-specific props, see tool description', additionalProperties: true },
        id: str('Reuse an existing id to replace that component'),
        title: str('Panel title shown in the header bar'),
        region,
      },
      required: ['type', 'props'],
    },
  },
  {
    name: 'ui_update',
    description: 'Update an existing component: shallow-merge `patch` into its props (set a key to null to delete it). Optionally change title/region, or set hidden to take it off/on stage without losing it. Cheaper than ui_show for small changes.',
    inputSchema: {
      type: 'object',
      properties: {
        id: str(),
        patch: { type: 'object', description: 'Props to merge', additionalProperties: true },
        title: str(),
        region,
        hidden: { type: 'boolean', description: 'true = hide (kept, reopenable), false = show again' },
      },
      required: ['id'],
    },
  },
  {
    name: 'ui_remove',
    description: 'Remove a component from the workspace.',
    inputSchema: { type: 'object', properties: { id: str() }, required: ['id'] },
  },
  {
    name: 'ui_clear',
    description: 'Remove all components (optionally only one region). Chat and event log are unaffected.',
    inputSchema: { type: 'object', properties: { region } },
  },
  {
    name: 'ui_list',
    description: 'Inventory of every component: id, type, title, region, summarized props — and hidden:true for panels the human soft-closed (they exist but are off stage; ui_update {hidden:false} or ui_show brings them back). Also reports who is looking and how big their workspace area is (px), so you can size and center panels. Check this before adding to a busy workspace.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'ui_get',
    description: 'Full props and live state of one component — e.g. current form field values as the human typed them.',
    inputSchema: { type: 'object', properties: { id: str() }, required: ['id'] },
  },
  {
    name: 'log',
    description: 'Append an entry to the Surface event log (always visible to the human). Use for meaningful progress, decisions, warnings and results — not chatter.',
    inputSchema: {
      type: 'object',
      properties: {
        level: { type: 'string', enum: ['debug', 'info', 'success', 'warn', 'error'] },
        message: str(),
        data: { description: 'Optional structured detail' },
      },
      required: ['level', 'message'],
    },
  },
  {
    name: 'ui_html',
    description: `Show a small self-contained HTML page in a sandboxed panel. For anything substantial (apps, interactive designs), write real files under <workspace>/apps/<name>/ and use ui_app instead. ${APP_GUIDE}`,
    inputSchema: {
      type: 'object',
      properties: {
        html: str(),
        id: str(),
        title: str(),
        height: { type: ['number', 'string'], description: 'Panel height in px, or "fill" to take the whole visible workspace area (default 360)' },
        region,
      },
      required: ['html'],
    },
  },
  {
    name: 'ui_app',
    description: `Mount a directory you built (must contain index.html) as a live app panel. The hub serves the whole directory, injects window.surface, and HOT-RELOADS the panel whenever you edit the files — so build with Write/Edit in <workspace>/apps/<name>/ and iterate while the human watches. Use this for full apps, presentations, dashboards, interactive visualizations and design canvases. ${APP_GUIDE}`,
    inputSchema: {
      type: 'object',
      properties: {
        path: str('App directory (absolute, or relative to the workspace)'),
        id: str(),
        title: str(),
        height: { type: ['number', 'string'], description: 'Panel height in px, or "fill" to take the whole visible workspace area (default 480; the human can also expand any panel to fullscreen)' },
        region,
      },
      required: ['path'],
    },
  },
  {
    name: 'ui_screenshot',
    description: 'See the workspace as the human currently sees it: a connected browser rasterizes the workspace area and the PNG is saved into your folder — Read the returned path to look at it. Iframe interiors (app/html panels) may appear blank; everything else renders faithfully. Fails if no browser is viewing this workspace.',
    inputSchema: { type: 'object', properties: {} },
  },
];

// ---- execution (hub side) ------------------------------------------------

export interface ToolContext {
  store: SurfaceStore;
  assets: AssetHub;
  workspaceDir: string;
  wsId: string;
  /** Ask a connected browser to rasterize the workspace; resolves to a PNG path. */
  capture: () => Promise<string>;
  /** Workspace-area sizes of the browsers currently viewing this workspace. */
  viewers?: () => { width: number; height: number; dpr: number }[];
}

function summarize(value: unknown, max = 160): unknown {
  if (typeof value === 'string') return value.length > max ? value.slice(0, max) + `… (${value.length} chars)` : value;
  if (Array.isArray(value)) return value.length > 8 ? [...value.slice(0, 8).map((v) => summarize(v, 60)), `… ${value.length - 8} more`] : value.map((v) => summarize(v, 60));
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, summarize(v, 60)]));
  }
  return value;
}

export function createToolRunner(ctx: ToolContext) {
  const { store, assets } = ctx;

  const resolvePath = (p: string) => (path.isAbsolute(p) ? p : path.resolve(ctx.workspaceDir, p));

  const materializeProps = (
    type: ComponentType,
    props: Record<string, unknown>,
    componentId: string,
  ): Record<string, unknown> => {
    if (type === 'image' || type === 'video' || type === 'audio' || type === 'file') {
      const p = props.path;
      if (typeof p !== 'string') throw new Error(`${type} needs props.path (local file path)`);
      const abs = resolvePath(p);
      const asset = assets.registerAsset(abs);
      // Store workspace-relative paths (rename-proof); absolute only outside it.
      const stored = abs.startsWith(ctx.workspaceDir + path.sep) ? path.relative(ctx.workspaceDir, abs) : abs;
      return { ...props, path: stored, src: asset.url, name: asset.name, size: asset.size, mime: asset.mime };
    }
    if (type === 'app') {
      const p = props.path;
      if (typeof p !== 'string') throw new Error('app needs props.path (directory with index.html)');
      const abs = resolvePath(p);
      const mount = assets.mountApp(abs, componentId, ctx.wsId);
      const stored = abs.startsWith(ctx.workspaceDir + path.sep) ? path.relative(ctx.workspaceDir, abs) : abs;
      return { ...props, path: stored, appId: mount.appId, url: mount.url };
    }
    return props;
  };

  return async function runTool(name: string, args: Record<string, any>): Promise<string> {
    switch (name) {
      case 'ui_show': {
        const type = args.type as ComponentType;
        if (!COMPONENT_TYPES.includes(type)) throw new Error(`unknown component type: ${args.type}`);
        const id = (args.id as string | undefined) ?? `${type}-${Math.random().toString(36).slice(2, 8)}`;
        const props = materializeProps(type, args.props ?? {}, id);
        const comp = store.upsertComponent({ id, type, title: args.title, region: args.region as Region | undefined, props });
        store.addLog('info', 'agent', `showed ${comp.type} “${comp.title ?? comp.id}”`);
        return `Displayed ${comp.type} as id=${comp.id} in region=${comp.region}. ${store.state.components.length} component(s) active.`;
      }

      case 'ui_update': {
        const comp = store.getComponent(args.id);
        if (!comp) return `No component with id=${args.id}. Use ui_list to see what is displayed.`;
        if (args.patch) {
          const props = materializeProps(comp.type, { ...comp.props, ...args.patch }, comp.id);
          store.patchComponentProps(args.id, props);
        }
        if (args.title !== undefined || args.region !== undefined) {
          store.setComponentMeta(args.id, { title: args.title, region: args.region as Region | undefined });
        }
        if (typeof args.hidden === 'boolean') {
          store.setHidden(args.id, args.hidden);
          store.addLog('info', 'agent', `${args.hidden ? 'hid' : 'reopened'} ${args.id}`);
        }
        return `Updated ${args.id}.`;
      }

      case 'ui_remove': {
        const ok = store.removeComponent(args.id);
        if (ok) store.addLog('info', 'agent', `removed ${args.id}`);
        return ok ? `Removed ${args.id}.` : `No component with id=${args.id}.`;
      }

      case 'ui_clear': {
        const n = store.clearComponents(args.region as Region | undefined);
        store.addLog('info', 'agent', `cleared ${n} component(s)${args.region ? ` from ${args.region}` : ''}`);
        return `Removed ${n} component(s).`;
      }

      case 'ui_list': {
        const items = store.state.components
          .slice()
          .sort((a, b) => a.order - b.order)
          .map((c) => ({ id: c.id, type: c.type, title: c.title, region: c.region, hidden: c.hidden || undefined, props: summarize(c.props) }));
        const viewers = ctx.viewers?.() ?? [];
        const head = viewers.length
          ? `viewers: ${viewers.length} — workspace area ${viewers.map((v) => `${v.width}×${v.height}px${v.dpr > 1 ? ` @${v.dpr}x` : ''}`).join(', ')}\n`
          : 'viewers: none right now (nobody has the workspace open)\n';
        return head + `shared expanded panel: ${store.state.view?.expandedId ?? 'none (panel layout)'}\n` + (items.length ? JSON.stringify(items, null, 2) : 'The workspace is empty.');
      }

      case 'ui_get': {
        const comp = store.getComponent(args.id);
        if (!comp) return `No component with id=${args.id}.`;
        return JSON.stringify({ id: comp.id, type: comp.type, title: comp.title, region: comp.region, expanded: store.state.view?.expandedId === comp.id, props: comp.props }, null, 2);
      }

      case 'log': {
        store.addLog(args.level as LogLevel, 'agent', String(args.message), args.data);
        return 'Logged.';
      }

      case 'ui_html': {
        const comp = store.upsertComponent({
          id: args.id,
          type: 'html',
          title: args.title,
          region: args.region as Region | undefined,
          props: { html: String(args.html), height: args.height },
        });
        store.addLog('info', 'agent', `rendered html panel “${comp.title ?? comp.id}”`);
        return `Displayed html panel as id=${comp.id}.`;
      }

      case 'ui_app': {
        const id = (args.id as string | undefined) ?? `app-${Math.random().toString(36).slice(2, 8)}`;
        const dir = resolvePath(String(args.path));
        const mount = assets.mountApp(dir, id, ctx.wsId);
        const comp = store.upsertComponent({
          id,
          type: 'app',
          title: args.title ?? path.basename(dir),
          region: args.region as Region | undefined,
          props: { path: dir, appId: mount.appId, url: mount.url, height: args.height },
        });
        store.addLog('success', 'agent', `mounted app “${comp.title}” (${dir})`);
        return `App mounted as id=${comp.id}, served at ${mount.url}. Edits to files in ${dir} hot-reload the panel.`;
      }

      case 'ui_screenshot': {
        const file = await ctx.capture();
        return `Workspace screenshot saved to ${file} — Read it to see the current rendering.`;
      }

      default:
        throw new Error(`unknown tool: ${name}`);
    }
  };
}
