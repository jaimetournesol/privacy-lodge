<script lang="ts">
  import { onMount } from "svelte";
  import {
    startStatusPolling,
    stopStatusPolling,
    status,
    type Status,
  } from "$lib/api";
  import Wordmark from "$lib/components/Wordmark.svelte";
  import StepBrowserSetup from "$lib/components/StepBrowserSetup.svelte";
  import MainWindow from "$lib/components/MainWindow.svelte";

  onMount(() => {
    startStatusPolling();
    return () => stopStatusPolling();
  });

  type View = "loading" | "browser-setup" | "main";

  // Feature A: first-run setup happens in the browser (a loopback web page the
  // box serves + opens). The GUI window just shows a "finish setup in your
  // browser" screen until the box is running, then the dashboard.
  function pickView(s: Status | null): View {
    if (!s) return "loading";
    switch (s.phase) {
      case "fresh":
      case "setting_up":
        return "browser-setup";
      default: // running / stopped / error
        return "main";
    }
  }

  const view = $derived(pickView($status));
</script>

<svelte:head>
  <title>Privacy Lodge</title>
</svelte:head>

{#if view === "loading"}
  <div class="splash">
    <Wordmark size="lg" />
    <p class="dim">Checking your box…</p>
  </div>
{:else if view === "browser-setup"}
  <div class="onboard">
    <div class="onboard-inner">
      <StepBrowserSetup st={$status} />
    </div>
  </div>
{:else if $status}
  <MainWindow st={$status} />
{/if}

<style>
  .splash {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--sp-4);
  }

  .onboard {
    min-height: 100vh;
    display: flex;
    justify-content: center;
    padding: var(--sp-7) var(--sp-6);
  }

  .onboard-inner {
    width: min(40rem, 100%);
  }
</style>
