<script>
  import { haptic } from "../tg.js";

  let { label = "", checked = $bindable(false), disabled = false, onchange = undefined } = $props();
</script>

<label class="switch">
  <span>{label}</span>
  <input
    type="checkbox"
    role="switch"
    bind:checked
    {disabled}
    onchange={(e) => {
      haptic("select");
      onchange?.(e.currentTarget.checked);
    }}
  />
  <span class="track" aria-hidden="true"><span class="thumb"></span></span>
</label>

<style>
  .switch {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 16px;
    min-height: 50px;
    background: var(--surface);
    position: relative;
  }
  input {
    position: absolute;
    opacity: 0;
    inset: 0;
    margin: 0;
    cursor: pointer;
  }
  .track {
    width: 51px;
    height: 31px;
    border-radius: 16px;
    background: color-mix(in srgb, var(--hint) 38%, transparent);
    padding: 2px;
    transition: background 0.2s;
    flex: none;
  }
  .thumb {
    display: block;
    width: 27px;
    height: 27px;
    border-radius: 50%;
    background: #fff;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.25);
    transition: transform 0.2s;
  }
  input:checked + .track {
    background: #34c759;
  }
  input:checked + .track .thumb {
    transform: translateX(20px);
  }
  input:focus-visible + .track {
    outline: 2px solid var(--link);
    outline-offset: 2px;
  }
</style>
