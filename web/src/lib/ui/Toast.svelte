<script>
  import { toast } from "../toast.svelte.js";
</script>

{#if toast.text}
  {#key toast.id}
    <div class="toast {toast.tone}" role="status">{toast.text}</div>
  {/key}
{/if}

<style>
  .toast {
    position: fixed;
    left: 16px;
    right: 16px;
    bottom: calc(86px + var(--safe-bottom));
    max-width: 480px;
    margin: 0 auto;
    z-index: 40;
    padding: 12px 16px;
    border-radius: 14px;
    background: color-mix(in srgb, var(--text) 88%, transparent);
    color: var(--bg);
    font-size: 14px;
    text-align: center;
    animation: pop 0.2s ease-out;
  }
  .error {
    background: var(--danger);
    color: #fff;
  }
  @keyframes pop {
    from {
      transform: translateY(8px);
      opacity: 0;
    }
  }
</style>
