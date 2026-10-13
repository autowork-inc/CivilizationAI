<script>
  let { variant = "primary", loading = false, disabled = false, onclick = undefined, type = "button", children } = $props();
</script>

<button class="btn {variant}" {type} disabled={disabled || loading} {onclick}>
  {#if loading}<span class="dots" aria-hidden="true">…</span>{:else}{@render children?.()}{/if}
</button>

<style>
  .btn {
    width: 100%;
    min-height: 50px;
    padding: 12px 16px;
    border: 0;
    border-radius: var(--radius);
    font-weight: 600;
    font-size: 16px;
    transition: opacity 0.15s, transform 0.1s;
  }
  .btn:active:not(:disabled) {
    transform: scale(0.98);
  }
  .btn:disabled {
    opacity: 0.5;
  }
  .primary {
    background: var(--accent);
    color: var(--accent-text);
  }
  .secondary {
    background: color-mix(in srgb, var(--accent) 14%, transparent);
    color: var(--link);
  }
  .danger {
    background: var(--danger-bg);
    color: var(--danger);
  }
  .plain {
    background: none;
    color: var(--link);
  }
</style>
