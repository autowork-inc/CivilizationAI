<script>
  import { haptic } from "../tg.js";

  let { options = [], value = $bindable("") } = $props();
</script>

<div class="segmented" role="radiogroup">
  {#each options as option (option.value)}
    <button
      type="button"
      role="radio"
      aria-checked={value === option.value}
      class:active={value === option.value}
      onclick={() => {
        if (value !== option.value) haptic("select");
        value = option.value;
      }}
    >
      {option.label}
    </button>
  {/each}
</div>

<style>
  .segmented {
    display: flex;
    gap: 2px;
    padding: 2px;
    border-radius: 10px;
    background: color-mix(in srgb, var(--hint) 20%, transparent);
  }
  button {
    flex: 1;
    border: 0;
    border-radius: 8px;
    padding: 7px 4px;
    background: none;
    font-size: 14px;
    font-weight: 500;
    transition: background 0.15s;
  }
  .active {
    background: var(--surface);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
    font-weight: 600;
  }
</style>
