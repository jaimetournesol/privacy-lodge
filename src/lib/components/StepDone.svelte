<script lang="ts">
  import type { Status } from "$lib/api";

  let { st, onOpen }: { st: Status | null; onOpen: () => void } = $props();

  // Don't claim "running" while the box is still settling — derive the headline
  // from the live phase so setup reads as amber "almost there", green only once
  // the box is actually up.
  const ready = $derived(st?.phase === "running");
</script>

<div class="step">
  <p class="eyebrow">Step 3 of 3 &middot; Done</p>
  <h1>That's everything</h1>

  <div class="card summary">
    {#if ready}
      <p class="headline">
        Your box is running <span class="dot-ok" aria-hidden="true">&#9679;</span>
      </p>
    {:else}
      <p class="headline">
        Finishing setup — almost there
        <span class="dot-warn" aria-hidden="true">&#9679;</span>
      </p>
    {/if}
    {#if st}
      <dl class="meta">
        <div><dt class="dim">Box</dt><dd>{st.box_name}</dd></div>
        {#if st.onion}
          <div>
            <dt class="dim">Private address</dt>
            <dd class="mono">{st.onion}</dd>
          </div>
        {/if}
      </dl>
    {/if}
    <p class="dim note">
      Your recovery kit is safe. To put your box in your pocket, open Privacy Lodge
      and tap &ldquo;show QR&rdquo; on your private address — any time you like.
    </p>
  </div>

  <div class="actions">
    <button class="btn btn-primary" onclick={onOpen}>Open Privacy Lodge</button>
  </div>
</div>

<style>
  .step {
    display: flex;
    flex-direction: column;
    gap: var(--sp-5);
  }

  h1 {
    font-size: var(--fs-xl);
  }

  .summary {
    max-width: 34rem;
    display: flex;
    flex-direction: column;
    gap: var(--sp-4);
  }

  .headline {
    font-size: var(--fs-lg);
    font-weight: 650;
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

  .note {
    font-size: var(--fs-sm);
  }
</style>
