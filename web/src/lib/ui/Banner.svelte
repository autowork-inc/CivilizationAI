<script>
  let { tone = "info", title = "", text = "", action = "", onaction = undefined } = $props();
</script>

<div class="banner {tone}" role="status">
  <div class="copy">
    {#if title}<strong>{title}</strong>{/if}
    {#if text}<span>{text}</span>{/if}
  </div>
  {#if action}<button type="button" onclick={onaction}>{action}</button>{/if}
</div>

<style>
  .banner {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 14px;
    border-radius: var(--radius);
    margin-bottom: 14px;
    font-size: 14px;
  }
  .info {
    background: color-mix(in srgb, var(--accent) 12%, var(--surface));
  }
  .warning {
    background: var(--warning-bg);
  }
  .danger {
    background: var(--danger-bg);
  }
  .copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    flex: 1;
    min-width: 0;
  }
  button {
    border: 0;
    background: none;
    color: var(--link);
    font-weight: 600;
    padding: 6px 4px;
    white-space: nowrap;
  }
</style>
