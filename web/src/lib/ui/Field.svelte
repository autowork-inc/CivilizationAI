<script>
  // A labelled input row inside a Section-like card. `multiline` -> textarea.
  let {
    label = "",
    value = $bindable(""),
    type = "text",
    placeholder = "",
    multiline = false,
    hint = "",
    error = "",
    list = undefined,
    min = undefined,
    max = undefined,
    maxlength = undefined,
    inputmode = undefined,
    onchange = undefined, // fires when the user COMMITS a value (blur / picker closed), with the new value
  } = $props();
</script>

<label class="field">
  {#if label}<span class="label">{label}</span>{/if}
  {#if multiline}
    <textarea bind:value {placeholder} {maxlength} rows="3" onchange={(e) => onchange?.(e.currentTarget.value)}></textarea>
  {:else}
    <input
      bind:value
      {type}
      {placeholder}
      {list}
      {min}
      {max}
      {maxlength}
      {inputmode}
      onchange={(e) => onchange?.(e.currentTarget.value)}
    />
  {/if}
  {#if error}<span class="error">{error}</span>{:else if hint}<span class="hint">{hint}</span>{/if}
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
  input,
  textarea {
    width: 100%;
    border: 0;
    outline: 0;
    padding: 2px 0;
    background: none;
    resize: none;
    font-size: 17px;
    min-width: 0;
  }
  input::placeholder,
  textarea::placeholder {
    color: var(--hint);
  }
  .hint {
    font-size: 12px;
    color: var(--hint);
  }
  .error {
    font-size: 12px;
    color: var(--danger);
  }
</style>
