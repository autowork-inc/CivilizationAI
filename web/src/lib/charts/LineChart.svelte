<script>
  // A line with dots over a fixed scale. `values` may contain `null` = "no data that day" (a gap, never a zero): the line is broken there, the point is not drawn.
  let { labels = [], values = [], min = -2, max = 2, height = 120, color = "var(--accent)", ariaLabel = "График" } = $props();

  const W = 320;
  const PAD = { top: 10, bottom: 18, side: 8 };
  const inner = $derived(height - PAD.top - PAD.bottom);
  const step = $derived((W - PAD.side * 2) / Math.max(labels.length - 1, 1));
  const x = (i) => PAD.side + i * step;
  const y = (v) => PAD.top + ((max - v) / (max - min)) * inner;

  // consecutive non-null points form one polyline
  const segments = $derived.by(() => {
    const out = [];
    let run = [];
    values.forEach((v, i) => {
      if (v === null || v === undefined) {
        if (run.length) out.push(run);
        run = [];
      } else {
        run.push([x(i), y(v)]);
      }
    });
    if (run.length) out.push(run);
    return out;
  });
</script>

<svg viewBox="0 0 {W} {height}" class="block w-full" role="img" aria-label={ariaLabel}>
  <line x1={PAD.side} x2={W - PAD.side} y1={y(0)} y2={y(0)} stroke="var(--separator)" stroke-width="1" stroke-dasharray="3 3" />
  {#each segments as run, i (i)}
    {#if run.length > 1}
      <polyline points={run.map((p) => p.join(",")).join(" ")} fill="none" stroke={color} stroke-width="2" stroke-linejoin="round" stroke-linecap="round" />
    {/if}
  {/each}
  {#each values as v, i (i)}
    {#if v !== null && v !== undefined}
      <circle cx={x(i)} cy={y(v)} r="3.5" fill={color}><title>{labels[i]}: {v}</title></circle>
    {/if}
  {/each}
  {#each labels as d, i (i)}
    {#if labels.length <= 16 || i % 5 === 0 || i === labels.length - 1}
      <text x={x(i)} y={height - 4} text-anchor="middle" font-size="9" fill="var(--hint)">{d.slice(8, 10)}</text>
    {/if}
  {/each}
</svg>
