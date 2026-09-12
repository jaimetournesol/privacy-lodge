<script lang="ts">
  /**
   * The Privacy Lodge brand mark — a proper eight-petal sunflower.
   * Eight gold petal-circles ring a darker seed-head centre.
   *
   * - `size`  px (default 20) — sets both width and height.
   * - colour comes from the brand gold token (--accent) by default;
   *   pass color="currentColor" to inherit the surrounding text colour.
   */
  let {
    size = 20,
    color = "var(--accent)",
  }: { size?: number; color?: string } = $props();

  // Eight petals evenly around the centre. Petals sit on a ring of radius 30
  // from the 50,50 centre (viewBox is 100×100), each a circle of radius 15.
  const petals = Array.from({ length: 8 }, (_, i) => {
    const a = (i / 8) * Math.PI * 2;
    return {
      cx: 50 + Math.cos(a) * 30,
      cy: 50 + Math.sin(a) * 30,
    };
  });
</script>

<svg
  class="sunflower"
  width={size}
  height={size}
  viewBox="0 0 100 100"
  role="img"
  aria-hidden="true"
  style="color: {color}"
>
  {#each petals as p}
    <circle cx={p.cx} cy={p.cy} r="15" fill="currentColor" />
  {/each}
  <!-- darker seed-head centre -->
  <circle cx="50" cy="50" r="20" fill="var(--accent-ink, #1a1a1a)" />
</svg>

<style>
  .sunflower {
    display: inline-block;
    flex: none;
    vertical-align: middle;
  }
</style>
