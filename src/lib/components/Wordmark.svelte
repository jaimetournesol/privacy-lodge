<script lang="ts">
  import Sunflower from "./Sunflower.svelte";

  let { size = "md" }: { size?: "sm" | "md" | "lg" } = $props();

  // Match the glyph to the wordmark's font size.
  const glyphPx = $derived({ sm: 18, md: 22, lg: 38 }[size]);
</script>

<span class="wordmark {size}">
  <Sunflower size={glyphPx} />
  <span class="name">Privacy Lodge</span>
</span>

<style>
  .wordmark {
    display: inline-flex;
    align-items: center;
    gap: 0.45em;
    font-weight: 700;
    letter-spacing: -0.01em;
    color: var(--text);
  }

  .sm {
    font-size: var(--fs-md);
  }

  .md {
    font-size: var(--fs-lg);
  }

  .lg {
    font-size: var(--fs-hero);
  }
</style>
