<script>
  // A small hand-made SVG bar chart (no chart library: the whole app is ~30 KB gzipped). Each series is an array aligned with `labels`; a series may be
  // positive (bars go up from the axis) or negative (they go down), so "earned" and "lost" XP can share one axis. Series are STACKED per day.
  // `series`: [{ name, values, color }]  colour = any CSS colour / var(...).
  let { labels = [], series = [], height = 120, ariaLabel = "График" } = $props();

  const W = 320;
  const PAD = { top: 8, bottom: 18, side: 4 };

  const stacks = $derived(
    labels.map((_, i) => {
      let up = 0;
      let down = 0;
      const parts = [];
      for (const s of series) {
        const v = s.values[i] ?? 0;
        if (v > 0) {
          parts.push({ s, y0: up, y1: up + v });
          up += v;
        } else if (v < 0) {
          parts.push({ s, y0: down, y1: down + v });
          down += v;
        }
      }
      return { up, down, parts };
    }),
  );
  const top = $derived(Math.max(1, ...stacks.map((s) => s.up)));
  const bottom = $derived(Math.min(0, ...stacks.map((s) => s.down)));
  const inner = $derived(height - PAD.top - PAD.bottom);
  const unit = $derived(inner / (top - bottom));
  const zero = $derived(PAD.top + top * unit);
  const step = $derived((W - PAD.side * 2) / Math.max(labels.length, 1));
  const barW = $derived(Math.max(2, step * 0.62));
  const y = (v) => zero - v * unit;

  function tick(i) {
    const d = labels[i];
    if (!d) return "";
    return d.slice(8, 10);
  }
</script>

<svg viewBox="0 0 {W} {height}" class="block w-full" role="img" aria-label={ariaLabel}>
  <line x1={PAD.side} x2={W - PAD.side} y1={zero} y2={zero} stroke="var(--separator)" stroke-width="1" />
  {#each stacks as stack, i (i)}
    {#each stack.parts as part (part.s.name)}
      <rect
        x={PAD.side + i * step + (step - barW) / 2}
        y={Math.min(y(part.y0), y(part.y1))}
        width={barW}
        height={Math.max(0, Math.abs(y(part.y1) - y(part.y0)))}
        rx="1.5"
        fill={part.s.color}
      >
        <title>{labels[i]}: {part.s.name} {part.s.values[i]}</title>
      </rect>
    {/each}
    {#if labels.length <= 16 || i % 5 === 0 || i === labels.length - 1}
      <text x={PAD.side + i * step + step / 2} y={height - 4} text-anchor="middle" font-size="9" fill="var(--hint)">{tick(i)}</text>
    {/if}
  {/each}
</svg>
