<script lang="ts">
  import {
    copyText,
    getConnectQr,
    liveness,
    refreshStatus,
    startBox,
    stopBox,
    type ConnectQr,
    type Status,
  } from "$lib/api";
  import { onDestroy } from "svelte";
  import { mapError } from "$lib/errors";
  import AgentnodePanel from "./AgentnodePanel.svelte";
  import SettingsPanel from "./SettingsPanel.svelte";
  import BoxesPanel from "./BoxesPanel.svelte";
  import Sunflower from "./Sunflower.svelte";

  let { st }: { st: Status } = $props();

  // Poll liveness — when this goes stale the metric numbers below are frozen,
  // not live, so we say so instead of leaving a green "all good" lie on screen.
  const live = $derived($liveness);
  const stale = $derived(live.stale);
  const coreHealthy = $derived(["homeserver", "tor"].every(name => st.services.some(service => service.name === name && service.state === "healthy")));

  // A 1 s clock so "last seen Xs ago" counts up live while contact is lost.
  let now = $state(Date.now());
  const nowTimer = setInterval(() => (now = Date.now()), 1000);
  onDestroy(() => clearInterval(nowTimer));
  const lastSeenSecs = $derived(
    live.lastOk ? Math.max(0, Math.round((now - live.lastOk) / 1000)) : null,
  );

  type View = "home" | "people" | "agent" | "settings";
  let view = $state<View>("home");

  let qr = $state<ConnectQr | null>(null);
  let showQr = $state(false);
  let qrBusy = $state(false);
  let copied = $state(false);
  let powerBusy = $state(false);
  // A persistent, friendly reason the last start/pause/restart failed. Lives
  // inside the (aria-live) status card; cleared on the next successful power()
  // or once the box is observed running again.
  let powerError = $state("");
  let reloadBusy = $state(false);
  let toast = $state("");
  let toastTimer: ReturnType<typeof setTimeout> | null = null;

  // The box came back to life on its own → drop any stale power error.
  $effect(() => {
    if (st.phase === "running" && !stale) powerError = "";
  });

  // People = the pairing panel (each person runs their own box, so a paired box
  // *is* a person). Agent (MCP) lands in a later build.
  const nav: { key: View; label: string; soon?: boolean }[] = [
    { key: "home", label: "Home" },
    { key: "people", label: "People" },
    { key: "agent", label: "Agents" },
    { key: "settings", label: "Settings" },
  ];

  const actionCards: { title: string; blurb: string; go?: View }[] = [
    { title: "Add a person", blurb: "Pair with a friend's box.", go: "people" },
    { title: "Protect your identity", blurb: "Export your address, login and contacts.", go: "settings" },
  ];

  // Map a service state to its status-dot class (healthy=green, starting=amber,
  // stopped=dim, error=red). Shared by the always-on System row and the error list.
  function dotClass(state: string) {
    return state === "healthy"
      ? "dot-ok"
      : state === "starting"
        ? "dot-warn"
        : state === "error"
          ? "dot-err"
          : "dim";
  }

  function showToast(msg: string) {
    toast = msg;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = ""), 2600);
  }

  function comingSoon() {
    showToast("Coming in the next build");
  }

  function onAction(go?: View) {
    if (go) view = go;
    else comingSoon();
  }

  async function copyAddress() {
    if (st.onion && (await copyText(st.onion))) {
      copied = true;
      setTimeout(() => (copied = false), 1800);
    }
  }

  async function toggleQr() {
    if (showQr) {
      showQr = false;
      return;
    }
    if (!qr) {
      qrBusy = true;
      try {
        qr = await getConnectQr();
      } catch {
        showToast("Couldn't build the QR code just yet — try again in a moment.");
        qrBusy = false;
        return;
      }
      qrBusy = false;
    }
    showQr = true;
  }

  async function power(action: "start" | "stop") {
    powerBusy = true;
    try {
      if (action === "start") await startBox();
      else await stopBox();
      // Success clears any lingering error row. Keep toasts for ephemeral
      // success cues only (e.g. "coming soon"), never for failures.
      powerError = "";
    } catch (e) {
      console.error(`power(${action}) failed:`, e);
      powerError = mapError(e);
    } finally {
      powerBusy = false;
    }
  }

  function retryPower() {
    // Retry whichever direction makes sense for the current phase.
    power(st.phase === "running" ? "stop" : "start");
  }

  async function reload() {
    reloadBusy = true;
    try {
      await refreshStatus();
    } finally {
      reloadBusy = false;
    }
  }
</script>

<div class="shell">
  <nav class="rail" aria-label="Main">
    <div class="rail-brand"><Sunflower size={24} /></div>
    {#each nav as item}
      {#if item.soon}
        <button class="rail-item" disabled title="Soon">
          {item.label}
          <span class="soon">soon</span>
        </button>
      {:else}
        <button
          class="rail-item"
          class:active={view === item.key}
          aria-current={view === item.key ? "page" : undefined}
          onclick={() => (view = item.key)}
        >
          {item.label}
        </button>
      {/if}
    {/each}
  </nav>

  {#if view === "people"}
    <main class="content"><BoxesPanel {st} /></main>
  {:else if view === "agent"}
    <main class="content"><AgentnodePanel /></main>
  {:else if view === "settings"}
    <main class="content"><SettingsPanel {st} /></main>
  {:else}
  <main class="content">
    <header class="topline">
      <h1>{st.box_name || "Lodge"}</h1>
      <p class="dim counts" class:frozen={stale}>
        {st.people_count}
        {st.people_count === 1 ? "person" : "people"} &middot; {st.paired_count}
        paired {st.paired_count === 1 ? "box" : "boxes"}
      </p>
    </header>

    {#if st.demo_mode}
      <div class="banner-demo" role="status">
        <span class="dot-warn" aria-hidden="true">&#9679;</span>
        <span>
          Demo mode — running without the real engine. Run
          <span class="mono">scripts/fetch-sidecars.sh</span>.
        </span>
      </div>
    {/if}

    <section class="card status-card" aria-live="polite">
      {#if stale}
        <p class="sentence">
          <span class="dot-warn" aria-hidden="true">&#9679;</span> Lost contact
          {#if lastSeenSecs !== null}
            — last seen {lastSeenSecs}s ago
          {:else}
            — checking again…
          {/if}
        </p>
        <button
          class="btn btn-subtle"
          onclick={reload}
          disabled={reloadBusy}
        >
          {reloadBusy ? "Checking…" : "Reload"}
        </button>
      {:else if st.phase === "running"}
        <p class="sentence">
          <span class={coreHealthy ? "dot-ok" : "dot-warn"} aria-hidden="true">&#9679;</span>
          {coreHealthy ? "Messaging and the private network are running." : "Lodge is running, but a service needs attention."}
        </p>
        <button
          class="btn btn-subtle"
          onclick={() => power("stop")}
          disabled={powerBusy}
        >
          {powerBusy ? "Pausing…" : "Pause"}
        </button>
      {:else if st.phase === "stopped"}
        <p class="sentence">
          <span class="dim" aria-hidden="true">&#9702;</span> Lodge is paused —
          people can't reach you.
        </p>
        <button
          class="btn btn-primary"
          onclick={() => power("start")}
          disabled={powerBusy}
        >
          {powerBusy ? "Starting…" : "Start"}
        </button>
      {:else if st.phase === "error"}
        <p class="sentence">
          <span class="dot-err" aria-hidden="true">&#9679;</span> Something's not
          right — {st.error || "Lodge hit an error."}
        </p>
        <button
          class="btn btn-subtle"
          onclick={() => power("start")}
          disabled={powerBusy}
        >
          {powerBusy ? "Trying…" : "Try again"}
        </button>
      {:else}
        <p class="sentence">
          <span class="dot-warn" aria-hidden="true">&#9679;</span> Finishing setup —
          almost there…
        </p>
      {/if}

      {#if powerError}
        <p class="power-error" role="alert">
          <span class="dot-err" aria-hidden="true">&#9679;</span>
          <span class="power-error-msg">{powerError} — Try again</span>
          <button
            class="btn-mini retry"
            onclick={retryPower}
            disabled={powerBusy}
          >
            {powerBusy ? "…" : "Retry"}
          </button>
        </p>
      {/if}
    </section>

    {#if st.phase === "error" && st.services.length}
      <ul class="services">
        {#each st.services as svc}
          <li>
            <span aria-hidden="true" class={dotClass(svc.state)}>&#9679;</span>
            {svc.name} — {svc.state}
          </li>
        {/each}
      </ul>
    {/if}

    {#if st.services.length}
      <div class="system-row" class:frozen={stale} aria-label="System services">
        <span class="system-label dim">System</span>
        {#each st.services as svc}
          <span class="system-svc" title="{svc.name} — {svc.state}">
            <span aria-hidden="true" class={dotClass(svc.state)}>&#9679;</span>
            {svc.name}
          </span>
        {/each}
      </div>
    {/if}

    <section class="card address-card">
      <div class="address-head">
        <h2>Your private address</h2>
        <div class="address-actions">
          <button
            class="btn-mini"
            onclick={copyAddress}
            disabled={!st.onion}
            aria-label="Copy your private address"
            title="Copy your private address"
          >
            {copied ? "✓ copied" : "copy"}
          </button>
          <button
            class="btn-mini"
            onclick={toggleQr}
            disabled={qrBusy}
            aria-expanded={showQr}
            aria-label={showQr ? "Hide QR code" : "Show QR code"}
          >
            {qrBusy ? "…" : showQr ? "hide QR" : "show QR"}
          </button>
        </div>
      </div>
      {#if st.onion}
        <p class="mono address">{st.onion}</p>
      {:else}
        <div class="address-minting" aria-live="polite">
          <span class="spinner" aria-hidden="true"></span>
          <span class="dim"
            >Building your private address — usually under a minute.</span
          >
        </div>
      {/if}
      {#if showQr && qr}
        <div
          class="qr-box"
          role="img"
          aria-label="QR code for your private address"
        >
          {@html qr.svg}
        </div>
      {/if}
    </section>

    <section class="actions-grid" aria-label="Actions">
      {#each actionCards as a}
        <button class="card action-card" onclick={() => onAction(a.go)}>
          <span class="action-title">{a.title}</span>
          <span class="dim action-blurb">{a.blurb}</span>
        </button>
      {/each}
    </section>

    <footer class="foot dim">
      Lodge runs while this computer is on — keep it awake and plugged in.
    </footer>
  </main>
  {/if}

  {#if toast}
    <div class="toast" role="status">{toast}</div>
  {/if}
</div>

<style>
  .shell {
    display: grid;
    grid-template-columns: 13rem 1fr;
    min-height: 100vh;
  }

  /* ── left rail ── */

  .rail {
    position: sticky;
    top: 0;
    height: 100vh;
    align-self: start;
    overflow-y: auto;
    border-right: 1px solid var(--hairline);
    padding: var(--sp-5) var(--sp-3);
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    background: var(--surface);
  }

  .rail-brand {
    color: var(--accent);
    font-size: var(--fs-xl);
    padding: 0 var(--sp-3) var(--sp-4);
  }

  .rail-item {
    appearance: none;
    font: inherit;
    font-size: var(--fs-sm);
    font-weight: 600;
    text-align: left;
    color: var(--text-dim);
    background: none;
    border: none;
    border-radius: var(--radius-sm);
    padding: 0.55rem var(--sp-3);
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-2);
  }

  .rail-item.active {
    color: var(--accent-ink);
    background: var(--accent);
  }

  .rail-item:disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  .soon {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    border: 1px solid var(--hairline-strong);
    border-radius: 999px;
    padding: 0.05rem 0.4rem;
  }

  /* ── content ── */

  .content {
    padding: var(--sp-6);
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
    max-width: 52rem;
  }

  .topline h1 {
    font-size: var(--fs-xl);
  }

  .counts {
    font-size: var(--fs-sm);
    margin-top: var(--sp-1);
  }

  .banner-demo {
    display: flex;
    align-items: baseline;
    gap: var(--sp-2);
    border: 1px solid var(--warn);
    background: color-mix(in srgb, var(--warn) 12%, transparent);
    color: var(--text);
    border-radius: var(--radius);
    padding: var(--sp-3) var(--sp-4);
    font-size: var(--fs-sm);
  }

  .status-card {
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: var(--sp-3) var(--sp-4);
  }

  /* The persistent power-error row spans both columns, under the sentence. */
  .power-error {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    color: var(--err);
    font-size: var(--fs-sm);
    border-top: 1px solid var(--hairline);
    padding-top: var(--sp-3);
  }

  .power-error-msg {
    flex: 1;
  }

  .power-error .retry {
    flex: none;
  }

  /* Frozen / stale metrics — dimmed so they don't read as live. */
  .counts.frozen,
  .system-row.frozen {
    opacity: 0.45;
  }

  .sentence {
    font-size: var(--fs-lg);
    font-weight: 600;
  }

  .services {
    list-style: none;
    margin: 0;
    padding: 0 var(--sp-2);
    display: flex;
    gap: var(--sp-5);
    font-size: var(--fs-sm);
    color: var(--text-dim);
  }

  .system-row {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--sp-2) var(--sp-4);
    padding: 0 var(--sp-2);
    font-size: var(--fs-xs);
    color: var(--text-dim);
  }

  .system-label {
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .system-svc {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-1);
  }

  .address-card h2 {
    font-size: var(--fs-md);
  }

  .address-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--sp-3);
    margin-bottom: var(--sp-3);
  }

  .address-actions {
    display: flex;
    gap: var(--sp-2);
  }

  .address {
    color: var(--text);
    font-size: var(--fs-sm);
  }

  .address-minting {
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--fs-sm);
  }

  .spinner {
    width: 0.9rem;
    height: 0.9rem;
    flex: none;
    border: 2px solid var(--hairline-strong);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .qr-box {
    margin-top: var(--sp-4);
    background: #fff;
    border-radius: var(--radius);
    padding: var(--sp-3);
    width: 12rem;
    height: 12rem;
  }

  .qr-box :global(svg) {
    display: block;
    width: 100%;
    height: 100%;
  }

  .actions-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: var(--sp-4);
  }

  .action-card {
    appearance: none;
    font: inherit;
    color: var(--text);
    text-align: left;
    cursor: pointer;
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    transition: border-color 120ms ease;
  }

  .action-card:hover {
    border-color: var(--accent);
  }

  .action-title {
    font-weight: 650;
    font-size: var(--fs-md);
  }

  .action-blurb {
    font-size: var(--fs-sm);
  }

  .foot {
    font-size: var(--fs-xs);
    margin-top: var(--sp-4);
  }

  .toast {
    position: fixed;
    bottom: var(--sp-5);
    left: 50%;
    transform: translateX(-50%);
    background: var(--surface-raised);
    border: 1px solid var(--hairline-strong);
    color: var(--text);
    border-radius: 999px;
    padding: var(--sp-2) var(--sp-5);
    font-size: var(--fs-sm);
    box-shadow: 0 6px 24px rgba(0, 0, 0, 0.4);
  }
</style>
