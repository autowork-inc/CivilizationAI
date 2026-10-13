<script>
  import { haptic } from "../tg.js";

  let { tabs = [], active = $bindable("") } = $props();
</script>

<nav class="tabbar" aria-label="Разделы">
  {#each tabs as tab (tab.id)}
    <button
      type="button"
      class:active={active === tab.id}
      aria-current={active === tab.id ? "page" : undefined}
      onclick={() => {
        if (active !== tab.id) haptic("select");
        active = tab.id;
      }}
    >
      <span class="icon" aria-hidden="true">{tab.icon}</span>
      <span class="label">{tab.label}</span>
    </button>
  {/each}
</nav>

<style>
  .tabbar {
    position: fixed;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 10;
    display: flex;
    padding: 6px 8px calc(6px + var(--safe-bottom));
    background: color-mix(in srgb, var(--surface) 88%, transparent);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-top: 0.5px solid var(--separator);
  }
  button {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 2px;
    border: 0;
    background: none;
    color: var(--hint);
    padding: 4px 0;
  }
  .icon {
    font-size: 22px;
    line-height: 1.1;
    filter: grayscale(1);
    opacity: 0.75;
    transition: transform 0.15s;
  }
  .label {
    font-size: 11px;
    font-weight: 500;
  }
  .active {
    color: var(--link);
  }
  .active .icon {
    filter: none;
    opacity: 1;
    transform: scale(1.08);
  }
</style>
