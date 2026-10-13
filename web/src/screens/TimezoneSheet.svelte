<script>
  import { api } from "../lib/api.js";
  import { app, fail } from "../lib/app.svelte.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { allTimeZones } from "../lib/time.js";
  import { deviceTimeZone, haptic } from "../lib/tg.js";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";

  let { open = false, current = "", onclose, onsaved } = $props();

  let value = $state("");
  let busy = $state(false);
  const zones = allTimeZones();
  const device = deviceTimeZone();

  $effect(() => {
    if (open) value = current;
  });

  const valid = $derived(value === "UTC" || (value.includes("/") && (zones.length === 0 || zones.includes(value))));

  async function save() {
    busy = true;
    try {
      const result = await api.setTimezone(value.trim());
      app.timezone = result.timezone;
      haptic("success");
      showToast("Часовой пояс обновлён");
      onsaved();
    } catch (error) {
      fail(error);
    } finally {
      busy = false;
    }
  }
</script>

<Sheet {open} title="Часовой пояс" {onclose}>
  <div class="stack">
    <div class="card">
      <Field
        label="Пояс (IANA)"
        bind:value
        list="zones"
        placeholder="Europe/Copenhagen"
        hint={valid || !value ? "Начните вводить город или регион." : "Такого пояса нет. Пример: Europe/Copenhagen."}
        error=""
      />
      <datalist id="zones">
        {#each zones as zone (zone)}<option value={zone}></option>{/each}
      </datalist>
    </div>
    {#if device !== value}
      <Button variant="secondary" onclick={() => (value = device)}>Взять с устройства: {device}</Button>
    {/if}
    <Button onclick={save} disabled={!valid || value === current} loading={busy}>Сохранить</Button>
    <p class="muted note">Напоминания и тихие часы будут считаться по этому поясу.</p>
  </div>
</Sheet>

<style>
  .card {
    border-radius: var(--radius);
    overflow: hidden;
  }
  .note {
    font-size: 13px;
    text-align: center;
  }
</style>
