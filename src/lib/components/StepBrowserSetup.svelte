<script lang="ts">
  import { onMount } from "svelte";
  import Wordmark from "./Wordmark.svelte";
  import { getSetupUrl, openSetupPage, hasTauri, type Status } from "../api";

  let { st }: { st: Status | null } = $props();

  let url = $state("");
  let opening = $state(false);

  onMount(async () => {
    if (!hasTauri()) return;
    try {
      url = await getSetupUrl();
    } catch {
      /* server not up yet — the button still works */
    }
  });

  async function open() {
    opening = true;
    try {
      await openSetupPage();
    } catch {
      /* ignore — user can copy the URL below */
    }
    opening = false;
  }

  const settingUp = $derived(st?.phase === "setting_up");
</script>

<div class="step">
  <Wordmark size="lg" />

  {#if settingUp}
    <h1>Setting up your box…</h1>
    <p class="lead">
      Your box is coming up now. Go back to the <strong>setup tab in your browser</strong> —
      when it’s ready it’ll show a QR code to scan with the Privacy Lodge app on your phone.
    </p>
  {:else}
    <h1>Finish setup in your browser</h1>
    <p class="lead">
      We opened a private setup page in your browser. Choose a username and password there,
      then scan the QR code with the Privacy Lodge app on your phone. Once your phone is
      connected, everything else is managed from the app.
    </p>
  {/if}

  <div class="actions">
    <button class="btn btn-primary" onclick={open} disabled={opening}>
      {opening ? "Opening…" : "Open the setup page"}
    </button>
  </div>

  {#if url}
    <p class="url dim">Or open this in any browser on this computer:<br /><code>{url}</code></p>
  {/if}

  <p class="foot dim">
    The setup page runs only on this computer and shuts down automatically once your phone is
    connected.
  </p>
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--sp-5);
    align-items: flex-start;
  }
  h1 {
    font-size: var(--fs-xl);
    max-width: 22ch;
  }
  .lead {
    font-size: var(--fs-md);
    line-height: 1.6;
    max-width: 46ch;
    margin: 0;
  }
  .actions {
    margin-top: var(--sp-2);
  }
  .url {
    font-size: var(--fs-sm);
    line-height: 1.7;
  }
  .url code {
    font-family: var(--mono);
    background: rgba(255, 255, 255, 0.06);
    padding: 2px 7px;
    border-radius: 6px;
  }
  .foot {
    font-size: var(--fs-xs);
    max-width: 46ch;
  }
</style>
