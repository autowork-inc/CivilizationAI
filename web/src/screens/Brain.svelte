<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { app, fail } from "../lib/app.svelte.js";
  import { factText, factValue } from "../lib/labels.js";
  import { deviceTimeZone } from "../lib/tg.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Cell from "../lib/ui/Cell.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";
  import FactSheet from "./FactSheet.svelte";
  import TimezoneSheet from "./TimezoneSheet.svelte";

  let data = $state(null);
  let editing = $state(null);
  let tzOpen = $state(false);
  const device = deviceTimeZone();

  async function load() {
    try {
      data = await api.profile();
      app.timezone = data.timezone;
    } catch (error) {
      fail(error);
    }
  }

  onMount(load);

  // The sections and their human names come from the server (/api/profile -> groups): the page never builds a name out of a machine key.
  const groups = $derived(data?.groups ?? []);

  async function useDeviceZone() {
    try {
      await api.setTimezone(device);
      await load();
    } catch (error) {
      fail(error, load);
    }
  }
</script>

<div class="screen">
  <h1 class="screen-title">Мозг</h1>

  {#if !data}
    <Spinner />
  {:else}
    {#if device !== data.timezone}
      <Banner
        tone="warning"
        title="Часовой пояс устройства отличается"
        text="Сейчас у вас {device}, а в профиле {data.timezone}. Напоминания приходят по времени профиля."
        action="Обновить"
        onaction={useDeviceZone}
      />
    {/if}

    <Section header="Часовой пояс" footer="По нему приходят напоминания и считаются тихие часы.">
      <Cell title="Часовой пояс" after={data.timezone} chevron onclick={() => (tzOpen = true)} />
    </Section>

    {#each groups as group (group.group)}
      <Section header={group.label}>
        {#each group.facts as fact (fact.id)}
          {#if fact.title}
            <Cell title={fact.title} after={factValue(fact)} chevron onclick={() => (editing = fact)} />
          {:else}
            <Cell title={factText(fact)} chevron onclick={() => (editing = fact)} />
          {/if}
        {/each}
      </Section>
    {/each}

    {#if !groups.length}
      <p class="muted empty">Пока я ничего о вас не знаю. Расскажите о себе в чате — я запомню главное.</p>
    {:else}
      <p class="muted footnote">
        Это то, что я помню о вас. Если что-то неверно — поправьте: я сразу начну отвечать с учётом новых данных.
      </p>
    {/if}
  {/if}
</div>

{#if editing}
  {#key editing.id}
    <FactSheet
      fact={editing}
      onclose={() => (editing = null)}
      onsaved={() => {
        editing = null;
        load();
      }}
    />
  {/key}
{/if}

<TimezoneSheet
  open={tzOpen}
  current={data?.timezone ?? ""}
  onclose={() => (tzOpen = false)}
  onsaved={() => {
    tzOpen = false;
    load();
  }}
/>

<style>
  .empty,
  .footnote {
    margin: 8px 16px;
    font-size: 14px;
  }
</style>
