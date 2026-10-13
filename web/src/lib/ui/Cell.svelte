<script>
  // One row of a Section. With `onclick` it is a real <button> (full-width tap target), without it a plain row.
  let { title, subtitle = "", after = "", note = "", tone = "", chevron = false, onclick = undefined, children } = $props();
</script>

{#snippet content()}
  <span class="main">
    <span class="title" class:danger={tone === "danger"}>{title}</span>
    {#if subtitle}<span class="subtitle">{subtitle}</span>{/if}
    {#if note}<span class="note">{note}</span>{/if}
    {@render children?.()}
  </span>
  {#if after}<span class="after">{after}</span>{/if}
  {#if chevron}<span class="chevron" aria-hidden="true">›</span>{/if}
{/snippet}

{#if onclick}
  <button class="cell tap" type="button" {onclick}>{@render content()}</button>
{:else}
  <div class="cell">{@render content()}</div>
{/if}

<style>
  .cell {
    display: flex;
    align-items: center;
    gap: 12px;
    width: 100%;
    min-height: 50px;
    padding: 10px 16px;
    background: none;
    border: 0;
    text-align: left;
    position: relative;
  }
  .cell + :global(.cell)::before {
    content: "";
    position: absolute;
    top: 0;
    left: 16px;
    right: 0;
    border-top: 0.5px solid var(--separator);
  }
  .tap:active {
    background: color-mix(in srgb, var(--hint) 16%, transparent);
  }
  .main {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }
  .title {
    overflow-wrap: anywhere;
  }
  .title.danger {
    color: var(--danger);
  }
  .subtitle {
    font-size: 14px;
    color: var(--hint);
    overflow-wrap: anywhere;
  }
  .note {
    font-size: 13px;
    color: #c77d00;
  }
  .after {
    color: var(--hint);
    font-size: 15px;
    max-width: 55%;
    text-align: right;
    overflow-wrap: anywhere;
  }
  .chevron {
    color: var(--hint);
    font-size: 22px;
    line-height: 1;
    opacity: 0.6;
  }
</style>
