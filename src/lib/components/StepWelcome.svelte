<script lang="ts">
  import { onMount } from "svelte";
  import Wordmark from "./Wordmark.svelte";
  import { detectLegacyInstall, hasTauri } from "../api";

  let { onNext }: { onNext: () => void } = $props();

  // T-MIG: if a v0.1 Docker box is already running, warn before we set up a
  // SEPARATE new box on the new engine — never silently orphan it.
  let legacy = $state<string[] | null>(null);
  onMount(async () => {
    if (!hasTauri()) return;
    try {
      const r = await detectLegacyInstall();
      if (r.present) legacy = r.containers;
    } catch {
      /* docker absent / no perms — treat as no legacy box */
    }
  });

  const bullets = [
    "Runs on a computer you own — not someone else's cloud",
    "Your messages stay between you and the people you choose",
    "Reachable only over a private network address",
    "If you lose the machine, your recovery kit brings everything back",
  ];
</script>

<div class="step">
  <Wordmark size="lg" />

  <h1>Your messages. Your hardware. Your keys.</h1>

  <ul class="bullets">
    {#each bullets as b}
      <li><span class="tick" aria-hidden="true">&#10003;</span> {b}</li>
    {/each}
  </ul>

  {#if legacy}
    <div class="legacy card" role="status">
      <strong>You already have a Privacy Lodge box running here.</strong>
      <p>
        We found your existing Docker setup ({legacy.length} service{legacy.length ===
        1
          ? ""
          : "s"}). This app is a newer, simpler version with a different engine —
        setting up here creates a <em>separate</em> box, and the people on your old
        box won’t carry over automatically.
      </p>
      <p class="dim">
        Happy with your current box? Keep using it and close this app — nothing
        here touches it. Want to move? Back it up first
        (<code>privacy-lodge backup</code>), then set up fresh below and re-invite
        your people.
      </p>
    </div>
  {/if}

  <div class="actions">
    <button class="btn btn-primary" onclick={onNext}>Set up my box</button>
    <span class="restore">
      <button class="btn-link" disabled title="Coming soon"
        >Restore from backup</button
      >
      <span class="dim soon-note">coming soon</span>
    </span>
  </div>
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

  .bullets {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--sp-3);
    color: var(--text);
    font-size: var(--fs-md);
  }

  .tick {
    color: var(--ok);
    font-weight: 700;
    margin-right: var(--sp-2);
  }

  .actions {
    display: flex;
    align-items: center;
    gap: var(--sp-5);
    margin-top: var(--sp-3);
  }

  .restore {
    display: inline-flex;
    align-items: baseline;
    gap: var(--sp-2);
  }

  .soon-note {
    font-size: var(--fs-xs);
  }

  .legacy {
    border-left: 3px solid var(--warn);
    display: flex;
    flex-direction: column;
    gap: var(--sp-2);
    max-width: 52ch;
  }
  .legacy p {
    margin: 0;
    font-size: var(--fs-sm);
    line-height: 1.5;
  }
  .legacy code {
    font-family: var(--mono);
    font-size: 0.9em;
    background: rgba(255, 255, 255, 0.06);
    padding: 1px 5px;
    border-radius: 5px;
  }
</style>
