<script lang="ts">
  import { onMount } from "svelte";
  import {
    appInfo,
    copyText,
    resetBox,
    saveRecoveryKitHtml,
    type AppInfo,
    type Status,
  } from "$lib/api";
  import { mapError } from "$lib/errors";

  let { st }: { st: Status } = $props();

  let info = $state<AppInfo | null>(null);
  let copied = $state(false);
  let kitPath = $state("");
  let kitErr = $state("");
  let confirmReset = $state(false);
  let resetting = $state(false);
  let advanced = $state(false);

  onMount(async () => {
    try {
      info = await appInfo();
    } catch {
      /* pre-setup or unavailable */
    }
  });

  async function copyDir() {
    if (info && (await copyText(info.data_dir))) {
      copied = true;
      setTimeout(() => (copied = false), 1600);
    }
  }

  async function saveKit() {
    kitErr = "";
    try {
      kitPath = await saveRecoveryKitHtml();
    } catch (e) {
      console.error("saveRecoveryKitHtml failed:", e);
      kitErr = mapError(e);
    }
  }

  async function doReset() {
    resetting = true;
    try {
      await resetBox();
      // status polling will flip the app back to the fresh/onboarding view
    } catch (e) {
      console.error("resetBox failed:", e);
      kitErr = mapError(e);
      resetting = false;
      confirmReset = false;
    }
  }
</script>

<div class="panel">
  <header><h1>Settings</h1></header>

  <section class="card">
    <h2>This computer is your box</h2>
    <p class="dim row">
      Your box only runs while this computer is awake. Keep it plugged in and
      stop it from sleeping so the people on it can always reach you.
    </p>
  </section>

  <section class="card">
    <h2>Recovery kit</h2>
    <p class="dim row">
      Re-save your printable recovery kit any time — keep it somewhere safe.
    </p>
    <div class="row actions">
      <button class="btn btn-subtle" onclick={saveKit}>Save recovery kit</button>
      {#if kitPath}<span class="dim ok">Saved to {kitPath}</span>{/if}
      {#if kitErr}<span class="err">{kitErr}</span>{/if}
    </div>
  </section>

  <section class="card">
    <button class="disclosure" onclick={() => (advanced = !advanced)}>
      {advanced ? "▾" : "▸"} Advanced
    </button>
    {#if advanced}
      <dl class="kv">
        <dt>Version</dt>
        <dd class="mono">{info?.version ?? "—"}</dd>
        <dt>Data folder</dt>
        <dd>
          <span class="mono">{info?.data_dir ?? "—"}</span>
          {#if info}<button class="btn-mini" onclick={copyDir}
              >{copied ? "✓" : "copy"}</button
            >{/if}
        </dd>
        <dt>Engine</dt>
        <dd>{info?.demo_mode ? "demo (no sidecars installed)" : "tuwunel"}</dd>
      </dl>

      <div class="danger">
        <h3>Reset this box</h3>
        <p class="dim">
          Permanently deletes everything — your messages, your account, and your
          box’s address. There’s no undo, and the address can’t be recovered.
        </p>
        {#if !confirmReset}
          <button class="btn btn-danger" onclick={() => (confirmReset = true)}
            >Reset box…</button
          >
        {:else}
          <div class="row actions">
            <span>Are you sure? This cannot be undone.</span>
            <button
              class="btn btn-danger"
              onclick={doReset}
              disabled={resetting}>{resetting ? "Resetting…" : "Yes, wipe it"}</button
            >
            <button class="btn btn-subtle" onclick={() => (confirmReset = false)}
              >Cancel</button
            >
          </div>
        {/if}
      </div>
    {/if}
  </section>

  <footer class="dim foot">Privacy Lodge · {st.box_name || "your box"}</footer>
</div>

<style>
  .panel {
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
  }
  header h1 {
    font-size: var(--fs-xl);
  }
  h2 {
    font-size: var(--fs-md);
  }
  .row {
    font-size: var(--fs-sm);
    margin-top: var(--sp-2);
  }
  .actions {
    display: flex;
    align-items: center;
    gap: var(--sp-3);
    flex-wrap: wrap;
  }
  .ok {
    color: var(--ok);
  }
  .err {
    color: var(--err);
  }
  .disclosure {
    appearance: none;
    background: none;
    border: none;
    color: var(--text);
    font: inherit;
    font-weight: 600;
    cursor: pointer;
    padding: 0;
  }
  .kv {
    margin: var(--sp-3) 0 0;
    display: grid;
    grid-template-columns: 7rem 1fr;
    gap: var(--sp-2) var(--sp-3);
    font-size: var(--fs-sm);
  }
  .kv dt {
    color: var(--text-dim);
  }
  .kv dd {
    margin: 0;
    display: flex;
    align-items: center;
    gap: var(--sp-2);
    word-break: break-all;
  }
  .danger {
    margin-top: var(--sp-5);
    border-top: 1px solid var(--hairline);
    padding-top: var(--sp-4);
  }
  .danger h3 {
    font-size: var(--fs-sm);
    color: var(--err);
  }
  .danger .dim {
    font-size: var(--fs-sm);
    margin: var(--sp-2) 0 var(--sp-3);
  }
  .foot {
    font-size: var(--fs-xs);
    margin-top: var(--sp-3);
  }
</style>
