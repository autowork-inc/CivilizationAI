<script>
  // An empty state: what this list is for and what to do about it. `children` (optional) holds extra, smaller text.
  let { emoji = "", title = "", text = "", children } = $props();
</script>

<div class="empty">
  {#if emoji}<div class="emoji" aria-hidden="true">{emoji}</div>{/if}
  <strong>{title}</strong>
  {#if text}<p>{text}</p>{/if}
  {#if children}<div class="extra">{@render children()}</div>{/if}
</div>

<style>
  .empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: 6px;
    padding: 24px 20px;
    margin-bottom: 14px;
    background: var(--surface);
    border-radius: var(--radius);
  }
  .emoji {
    font-size: 40px;
    line-height: 1.1;
  }
  strong {
    font-size: 17px;
  }
  p {
    font-size: 14px;
    color: var(--hint);
    overflow-wrap: anywhere;
  }
  .extra {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .extra :global(p) {
    font-size: 14px;
    color: var(--hint);
  }
  .extra :global(p.small) {
    font-size: 12px;
    color: var(--hint);
    margin-top: 4px;
  }
</style>
