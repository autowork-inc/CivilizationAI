<script>
  // The mood of the current week, Monday..Sunday: one column per weekday. `week` = [{ date, weekday (0 = Mon), score (-2..2 | null), mood, future }].
  // A day without a reading is an empty slot with a dash (never a zero); a day that has not come yet is dimmed.
  import { MOOD_LABELS } from "../labels.js";

  let { week = [], height = 120, ariaLabel = "Настроение по дням недели" } = $props();

  const NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
  const W = 320;
  const PAD = { top: 18, bottom: 22, side: 10 };
  const inner = $derived(height - PAD.top - PAD.bottom);
  const colW = (W - PAD.side * 2) / 7;
  const mid = $derived(PAD.top + inner / 2);
  const unit = $derived(inner / 2 / 2); // pixels per score point (the scale is -2..2)
  const cx = (i) => PAD.side + colW * i + colW / 2;
</script>

<svg viewBox="0 0 {W} {height}" class="block w-full" role="img" aria-label={ariaLabel}>
  <line x1={PAD.side} x2={W - PAD.side} y1={mid} y2={mid} stroke="var(--separator)" stroke-width="1" stroke-dasharray="3 3" />
  {#each week as day, i (day.date)}
    {#if day.score !== null && day.score !== undefined}
      {@const h = Math.max(Math.abs(day.score) * unit, 3)}
      <rect
        x={cx(i) - colW * 0.28}
        y={day.score >= 0 ? mid - h : mid}
        width={colW * 0.56}
        height={h}
        rx="3"
        fill={day.score >= 0 ? "var(--accent)" : "#ef4444"}
        opacity={day.future ? 0.3 : 1}
      ><title>{NAMES[day.weekday]} {day.date.slice(8, 10)}.{day.date.slice(5, 7)}: {day.score > 0 ? "+" : ""}{day.score}</title></rect>
      {#if day.mood && MOOD_LABELS[day.mood]}
        <text x={cx(i)} y={day.score >= 0 ? mid - h - 3 : mid + h + 11} text-anchor="middle" font-size="11">{MOOD_LABELS[day.mood].icon}</text>
      {/if}
    {:else}
      <text x={cx(i)} y={mid + 3} text-anchor="middle" font-size="10" fill="var(--hint)" opacity={day.future ? 0.4 : 0.8}>–</text>
    {/if}
    <text x={cx(i)} y={height - 5} text-anchor="middle" font-size="10" fill="var(--hint)" font-weight={day.future ? "400" : "600"}>{NAMES[day.weekday]}</text>
  {/each}
</svg>
