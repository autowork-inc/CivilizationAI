<div class="spinner" role="progressbar" aria-label="Загрузка"></div>

<style>
  .spinner {
    width: 28px;
    height: 28px;
    margin: 40px auto;
    border-radius: 50%;
    border: 3px solid color-mix(in srgb, var(--hint) 30%, transparent);
    border-top-color: var(--accent);
    animation: spin 0.8s linear infinite;
  }
  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }
</style>
