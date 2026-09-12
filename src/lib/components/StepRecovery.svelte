<script lang="ts">
  import { onMount } from "svelte";
  import {
    confirmRecoveryWord,
    getRecoveryKit,
    saveRecoveryKitHtml,
    type RecoveryKit,
    type Status,
  } from "$lib/api";
  import { mapError } from "$lib/errors";
  import Sunflower from "./Sunflower.svelte";

  let { st, onNext }: { st: Status | null; onNext: () => void } = $props();

  let kit = $state<RecoveryKit | null>(null);
  let wordIndex = $state(0);
  let guess = $state("");
  let checked = $state<"none" | "right" | "wrong">("none");
  let confirmed = $state(false);
  let savedPath = $state("");
  let saveError = $state("");
  let saving = $state(false);

  const words = $derived(kit ? kit.phrase.trim().split(/\s+/) : []);

  const addressReady = $derived(
    st?.phase === "running" || st?.setup_stage === "ready"
  );

  onMount(() => {
    let alive = true;
    const load = async () => {
      if (!alive) return;
      try {
        const k = await getRecoveryKit();
        if (!alive) return;
        kit = k;
        const n = k.phrase.trim().split(/\s+/).length;
        wordIndex = Math.floor(Math.random() * Math.max(n, 1));
      } catch {
        // phrase may still be minting — try again shortly
        setTimeout(load, 800);
      }
    };
    void load();
    return () => {
      alive = false;
    };
  });

  function printKit() {
    window.print();
  }

  async function saveKit() {
    saving = true;
    saveError = "";
    try {
      savedPath = await saveRecoveryKitHtml();
    } catch (e) {
      console.error("saveRecoveryKitHtml failed:", e);
      saveError = mapError(e);
    } finally {
      saving = false;
    }
  }

  async function checkWord(e: SubmitEvent) {
    e.preventDefault();
    if (!guess.trim()) return;
    try {
      const ok = await confirmRecoveryWord({
        index: wordIndex,
        word: guess.trim().toLowerCase(),
      });
      checked = ok ? "right" : "wrong";
      if (ok) confirmed = true;
    } catch {
      checked = "wrong";
    }
  }

  function formatCreated(created: string): string {
    const d = new Date(created);
    return isNaN(d.getTime()) ? created : d.toLocaleString();
  }
</script>

<div class="step">
  <div class="head">
    <div>
      <p class="eyebrow">Step 2 of 3 &middot; Your recovery kit</p>
      <h1>Keep these six words safe</h1>
    </div>
    <span class="chip" class:ready={addressReady}>
      {#if addressReady}
        <span class="dot-ok" aria-hidden="true">&#9679;</span> address ready
      {:else}
        <span class="spin" aria-hidden="true">&#10227;</span> building address…
      {/if}
    </span>
  </div>

  <p class="dim lead">
    This phrase is the only way back into your box if this computer is lost.
    Print it or save it — then prove to yourself you've kept it.
  </p>

  {#if kit}
    <div class="card kit print-area">
      <div class="kit-head">
        <Sunflower size={20} />
        <strong>Privacy Lodge recovery kit</strong>
      </div>
      <ol class="phrase mono">
        {#each words as w, i}
          <li><span class="dim n">{i + 1}.</span> {w}</li>
        {/each}
      </ol>
      <dl class="meta">
        <div><dt class="dim">Box</dt><dd>{kit.box_name}</dd></div>
        <div><dt class="dim">Created</dt><dd>{formatCreated(kit.created)}</dd></div>
        {#if kit.onion}
          <div>
            <dt class="dim">Private address</dt>
            <dd class="mono">{kit.onion}</dd>
          </div>
        {/if}
      </dl>
    </div>

    <div class="kit-actions">
      <button class="btn btn-subtle" onclick={printKit}>Print kit</button>
      <button class="btn btn-subtle" onclick={saveKit} disabled={saving}>
        {saving ? "Saving…" : "Save kit"}
      </button>
    </div>

    {#if savedPath}
      <p class="saved">
        <span class="dot-ok" aria-hidden="true">&#9679;</span> saved — kit written
        to <span class="mono">{savedPath}</span>
      </p>
    {/if}
    {#if saveError}
      <p class="error" role="alert">
        <span class="dot-err" aria-hidden="true">&#9679;</span> {saveError}
      </p>
    {/if}

    <form class="confirm card" onsubmit={checkWord}>
      <label class="field-label" for="confirm-word">
        Type word {wordIndex + 1} of your phrase
      </label>
      <div class="confirm-row">
        <input
          id="confirm-word"
          class="input mono"
          type="text"
          bind:value={guess}
          autocomplete="off"
          spellcheck="false"
          disabled={confirmed}
        />
        <button type="submit" class="btn-mini" disabled={confirmed || !guess.trim()}>
          check
        </button>
      </div>
      {#if confirmed}
        <p class="right">
          <span class="dot-ok" aria-hidden="true">&#9679;</span> right — that's the
          one.
        </p>
      {:else if checked === "wrong"}
        <p class="error">
          <span class="dot-err" aria-hidden="true">&#9679;</span> wrong — that's not
          word {wordIndex + 1}. Check your kit and try again.
        </p>
      {/if}
    </form>
  {:else}
    <div class="card kit-loading">
      <p class="dim">
        <span class="spin" aria-hidden="true">&#10227;</span> preparing — writing
        your recovery phrase…
      </p>
    </div>
  {/if}

  <div class="actions">
    <button class="btn btn-primary" onclick={onNext} disabled={!confirmed}>
      I've kept it &rarr;
    </button>
  </div>
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
  }

  .head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: var(--sp-4);
  }

  h1 {
    font-size: var(--fs-xl);
    margin-top: var(--sp-1);
  }

  .lead {
    max-width: 46ch;
  }

  .chip {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    font-size: var(--fs-xs);
    font-weight: 600;
    color: var(--warn);
    border: 1px solid var(--hairline-strong);
    border-radius: 999px;
    padding: 0.25rem 0.7rem;
    background: var(--surface);
  }

  .chip.ready {
    color: var(--ok);
  }

  .spin {
    display: inline-block;
    animation: spin 1.4s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .kit-head {
    display: flex;
    align-items: baseline;
    gap: var(--sp-2);
    margin-bottom: var(--sp-4);
  }

  .phrase {
    list-style: none;
    margin: 0 0 var(--sp-4);
    padding: 0;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: var(--sp-3);
    font-size: var(--fs-lg);
  }

  .phrase .n {
    font-size: var(--fs-xs);
    margin-right: var(--sp-1);
  }

  .meta {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--sp-1);
    font-size: var(--fs-sm);
  }

  .meta div {
    display: flex;
    gap: var(--sp-2);
  }

  .meta dt {
    min-width: 8.5rem;
  }

  .meta dd {
    margin: 0;
  }

  .kit-actions {
    display: flex;
    gap: var(--sp-3);
  }

  .confirm {
    max-width: 32rem;
  }

  .confirm-row {
    display: flex;
    gap: var(--sp-2);
    align-items: center;
  }

  .saved {
    font-size: var(--fs-sm);
    color: var(--ok);
  }

  .saved .mono {
    color: var(--text);
  }

  .right {
    color: var(--ok);
    font-size: var(--fs-sm);
    margin-top: var(--sp-3);
  }

  .error {
    color: var(--err);
    font-size: var(--fs-sm);
    margin-top: var(--sp-3);
  }

  .actions {
    margin-top: var(--sp-2);
  }
</style>
