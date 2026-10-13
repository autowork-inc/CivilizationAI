<script>
  // A bottom sheet (Telegram's modal pattern): backdrop tap, the × button or Escape closes it.
  let { open = false, title = "", onclose, children } = $props();

  function onkey(event) {
    if (event.key === "Escape") onclose?.();
  }
</script>

<svelte:window onkeydown={open ? onkey : undefined} />

{#if open}
  <div class="backdrop" role="presentation" onclick={() => onclose?.()}></div>
  <div class="sheet" role="dialog" aria-modal="true" aria-label={title}>
    <div class="grip" aria-hidden="true"></div>
    <header>
      <h2>{title}</h2>
      <button class="close" type="button" aria-label="Закрыть" onclick={() => onclose?.()}>✕</button>
    </header>
    <div class="body">{@render children?.()}</div>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.45);
    z-index: 20;
    animation: fade 0.18s ease-out;
  }
  .sheet {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 21;
    max-height: 92vh;
    display: flex;
    flex-direction: column;
    background: var(--bg);
    border-radius: 20px 20px 0 0;
    max-width: 640px;
    margin: 0 auto;
    animation: rise 0.22s cubic-bezier(0.2, 0.8, 0.2, 1);
  }
  .grip {
    width: 36px;
    height: 5px;
    border-radius: 3px;
    background: var(--hint);
    opacity: 0.4;
    margin: 8px auto 0;
  }
  header {
    display: flex;
    align-items: center;
    padding: 8px 16px 4px 20px;
  }
  h2 {
    flex: 1;
    font-size: 18px;
    font-weight: 700;
  }
  .close {
    width: 32px;
    height: 32px;
    border: 0;
    border-radius: 50%;
    background: color-mix(in srgb, var(--hint) 22%, transparent);
    color: var(--hint);
    font-size: 14px;
  }
  .body {
    overflow-y: auto;
    padding: 8px 16px calc(20px + var(--safe-bottom));
    -webkit-overflow-scrolling: touch;
  }
  @keyframes rise {
    from {
      transform: translateY(30px);
      opacity: 0;
    }
  }
  @keyframes fade {
    from {
      opacity: 0;
    }
  }
</style>
