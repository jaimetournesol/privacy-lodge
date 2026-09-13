#!/usr/bin/env node
/**
 * surface stdio MCP server — a thin proxy.
 *
 * Spawned by OpenCode for each workspace session (it is registered in the
 * hub-written OpenCode config). Declares the ui_* tools (definitions come
 * from the hub-written tools.json) and forwards every tools/call to the hub
 * over localhost HTTP, so the hub stays the single owner of UI state.
 *
 * Which workspace? OpenCode starts MCP servers with the session's directory
 * as cwd, so the proxy sends its cwd and the hub maps it to the workspace.
 * SURFACE_WS_ID is an explicit override. Zero dependencies; newline-delimited
 * JSON-RPC.
 */
import fs from 'node:fs';
import readline from 'node:readline';

const HUB = process.env.SURFACE_HUB_URL ?? 'http://127.0.0.1:4400';
const DIR = process.cwd();
// Workspace binding: SURFACE_WS_FILE (re-read on every call, so a long-lived
// agent can be re-pointed at another workspace without a restart) wins over
// the static SURFACE_WS_ID; the hub falls back to matching cwd.
function wsId() {
  const file = process.env.SURFACE_WS_FILE;
  if (file) {
    try {
      const v = fs.readFileSync(file, 'utf8').trim();
      if (v) return v;
    } catch {
      /* fall through */
    }
  }
  return process.env.SURFACE_WS_ID;
}
const TOOLS = JSON.parse(fs.readFileSync(process.env.SURFACE_TOOLS_JSON, 'utf8'));

const write = (msg) => process.stdout.write(JSON.stringify(msg) + '\n');
const reply = (id, result) => write({ jsonrpc: '2.0', id, result });
const fail = (id, code, message) => write({ jsonrpc: '2.0', id, error: { code, message } });

async function callHub(name, args) {
  const res = await fetch(`${HUB}/internal/tool`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Agentnode-Token': process.env.AGENTNODE_TOKEN ?? '' },
    body: JSON.stringify({ ws: wsId(), dir: DIR, name, args }),
  });
  const body = await res.json();
  if (!res.ok) return { content: [{ type: 'text', text: `Error: ${body.error}` }], isError: true };
  return { content: [{ type: 'text', text: body.text }] };
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  line = line.trim();
  if (!line) return;
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    return;
  }
  const { id, method, params } = msg;
  if (id === undefined) return; // notification — nothing to do

  try {
    switch (method) {
      case 'initialize':
        reply(id, {
          protocolVersion: params?.protocolVersion ?? '2025-06-18',
          capabilities: { tools: {} },
          serverInfo: { name: 'surface', version: '0.2.0' },
        });
        break;
      case 'tools/list':
        reply(id, { tools: TOOLS });
        break;
      case 'tools/call':
        reply(id, await callHub(params.name, params.arguments ?? {}));
        break;
      case 'ping':
        reply(id, {});
        break;
      default:
        fail(id, -32601, `method not found: ${method}`);
    }
  } catch (err) {
    fail(id, -32603, String(err));
  }
});

// When stdin closes, let the event loop drain naturally so in-flight
// tools/call fetches still get their replies out before the process ends.
