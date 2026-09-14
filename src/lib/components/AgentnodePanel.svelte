<script lang="ts">
  import { onMount } from 'svelte';
  import { agentnodeStatus, installAgentnode, openConductor, type AgentnodeStatus } from '$lib/api';
  let status = $state<AgentnodeStatus | null>(null);
  let busy = $state(false);
  let error = $state('');
  const preparing = $derived(busy || Boolean(status?.stage && /^(Preparing|Downloading|Starting)/.test(status.stage)));
  async function refresh() {
    try { status = await agentnodeStatus(); } catch (e) { error = String(e); }
  }
  async function setup() {
    busy = true; error = '';
    try { status = await installAgentnode(); } catch (e) { error = String(e); }
    finally { busy = false; }
  }
  async function open() {
    error = '';
    try { await openConductor(); } catch (e) { error = String(e); }
  }
  onMount(() => { refresh(); const timer = setInterval(refresh, 10000); return () => clearInterval(timer); });
</script>
<header><p class="eyebrow">YOUR WORKSPACE</p><h1>Your agents</h1>
<p>Conductor keeps your plans and results visible while you chat. Add agents, choose their machines and follow each conversation from one workspace.</p></header>
<div class="actions">
  {#if status?.installed && status.conductor && status.worker}
    <button class="primary" onclick={open}>Open Conductor</button>
  {:else}
    <button class="primary" disabled={preparing} onclick={setup}>{preparing ? 'Preparing Conductor & worker…' : status?.installed ? 'Start Agents' : 'Set up Agents'}</button>
  {/if}
  <button onclick={refresh} disabled={busy}>Refresh status</button>
</div>
<div class="machines">
  <section><h2>Conductor</h2><p>Coordinates work and presents results.</p><span class:ready={status?.conductor}>{status === null ? 'Checking…' : status.conductor ? 'Running' : 'Not running'}</span></section>
  <section><h2>Docker worker</h2><p>Runs tasks in its own container workspace.</p><span class:ready={status?.worker}>{status === null ? 'Checking…' : status.worker ? 'Running' : 'Not running'}</span></section>
</div>

{#if status?.stage}<p role="status" aria-live="polite">{status.stage}</p>{/if}
{#if error || status?.error}<p class="error" role="alert">{error || status?.error}</p>{/if}
<section class="privacy"><h2>Your connection to Codex</h2><p>Conductor opens with its stage and text conversation together. From there, connect Codex on each machine, add agents, and browse their chats.</p><p>Prompts, supplied files and tool results are sent to OpenAI. Sign-in credentials stay in each runtime’s private storage. Agents do not receive the host’s Docker socket or your personal Codex login.</p></section>
<style>
  header{max-width:720px;margin-bottom:24px}h1{font-size:34px;margin:4px 0 12px;letter-spacing:-.7px}h2{font-size:19px;margin:0 0 8px}p{color:var(--text-dim);line-height:1.6}.eyebrow{font-size:12px;letter-spacing:.12em;color:var(--accent)}

  .machines{display:grid;grid-template-columns:1fr 1fr;gap:16px}.machines section,.privacy{padding:22px;border-radius:20px;background:var(--surface);border:1px solid var(--hairline)}.machines span{font-size:14px;color:var(--text-dim)}.machines span.ready{color:var(--accent)}.actions{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}.actions button{appearance:none;font:inherit;min-height:48px;padding:12px 20px;border-radius:14px;background:var(--surface);color:var(--text);border:1px solid var(--hairline-strong);cursor:pointer}.actions button:disabled{opacity:.6;cursor:default}.actions .primary{background:var(--accent);color:var(--accent-ink);border-color:transparent}.error{color:var(--err);padding:16px;background:var(--surface);border-radius:12px}.privacy{margin-top:20px}.privacy p:last-child{margin-bottom:0}
  @media(max-width:640px){.machines{grid-template-columns:1fr}.actions button{width:100%}}
</style>
