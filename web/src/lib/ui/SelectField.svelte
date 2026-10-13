<script>
  let { label = "", value = $bindable(""), options = [], hint = "" } = $props();
</script>

<label class="field">
  <span class="label">{label}</span>
  <span class="control">
    <select bind:value>
      {#each options as option (option.value)}
        <option value={option.value}>{option.label}</option>
      {/each}
    </select>
    <span class="caret" aria-hidden="true">⌄</span>
  </span>
  {#if hint}<span class="hint">{hint}</span>{/if}
</label>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 10px 16px;
    background: var(--surface);
  }
  .label {
    font-size: 13px;
    color: var(--hint);
  }
  .control {
    position: relative;
    display: flex;
  }
  select {
    width: 100%;
    appearance: none;
    -webkit-appearance: none;
    border: 0;
    outline: 0;
    background: none;
    padding: 2px 24px 2px 0;
    font-size: 17px;
    min-width: 0;
  }
  .caret {
    position: absolute;
    right: 2px;
    top: -2px;
    color: var(--hint);
    font-size: 20px;
    pointer-events: none;
  }
  .hint {
    font-size: 12px;
    color: var(--hint);
  }
</style>
