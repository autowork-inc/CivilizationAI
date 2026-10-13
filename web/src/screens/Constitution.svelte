<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Button from "../lib/ui/Button.svelte";
  import Cell from "../lib/ui/Cell.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";
  import PrincipleSheet from "./PrincipleSheet.svelte";

  let principles = $state(null);
  let sheet = $state(null); // { principle: object | null }

  async function load() {
    try {
      principles = await api.principles();
    } catch (error) {
      fail(error);
    }
  }

  onMount(load);

  const overridden = $derived((principles ?? []).filter((p) => p.overridden));
</script>

<div class="screen">
  <h1 class="screen-title">Конституция</h1>

  {#if !principles}
    <Spinner />
  {:else}
    {#if overridden.length}
      <Banner
        tone="warning"
        title="Часть правил временно не действует"
        text="Их перекрыло ваше текущее состояние. Откройте правило, чтобы вернуть его в силу."
      />
    {/if}

    {#if principles.length}
      <Section
        header="Ваши принципы"
        footer="Чем выше важность, тем труднее правило перекрыть настроением. Правка правила возвращает ему полную силу."
      >
        {#each principles as principle (principle.fact_id)}
          <Cell
            title={principle.text}
            subtitle="Важность {principle.priority}"
            note={principle.overridden ? `Перекрыто состоянием «${principle.overridden_by}»` : ""}
            chevron
            onclick={() => (sheet = { principle })}
          />
        {/each}
      </Section>
    {:else}
      <p class="muted empty">Принципов пока нет. Добавьте то, чего я должен держаться всегда: например, «не есть сладкое после шести».</p>
    {/if}

    <Button variant="secondary" onclick={() => (sheet = { principle: null })}>＋ Новый принцип</Button>
  {/if}
</div>

{#if sheet}
  {#key sheet.principle?.fact_id ?? "new"}
    <PrincipleSheet
      principle={sheet.principle}
      onclose={() => (sheet = null)}
      onsaved={() => {
        sheet = null;
        load();
      }}
    />
  {/key}
{/if}

<style>
  .empty {
    margin: 8px 16px 16px;
    font-size: 14px;
  }
</style>
