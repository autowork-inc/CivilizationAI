<script>
  let { header = "", footer = "", children } = $props();
</script>

<section class="section">
  {#if header}<h2 class="header">{header}</h2>{/if}
  <div class="cells">{@render children?.()}</div>
  {#if footer}<p class="footer">{footer}</p>{/if}
</section>

<style>
  .section {
    margin-bottom: 22px;
  }
  .header {
    font-size: 13px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.2px;
    color: var(--section-header);
    margin: 0 16px 7px;
  }
  .cells {
    background: var(--surface);
    border-radius: var(--radius);
    overflow: hidden;
  }
  .footer {
    font-size: 13px;
    color: var(--hint);
    margin: 7px 16px 0;
  }
</style>
