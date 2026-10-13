<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { app, fail } from "../lib/app.svelte.js";
  import { addDaysKey, dayKey, formatDayHeading, formatRangeTitle, timeKey, todayKey, zonedToUtc } from "../lib/time.js";
  import { deviceTimeZone } from "../lib/tg.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Button from "../lib/ui/Button.svelte";
  import Cell from "../lib/ui/Cell.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";
  import EventSheet from "./EventSheet.svelte";
  import OccurrenceSheet from "./OccurrenceSheet.svelte";

  const DAYS = 14;
  const device = deviceTimeZone();

  let anchor = $state(todayKey(app.timezone || device));
  let data = $state(null);
  let personas = $state([]);
  let sheet = $state(null); // { event: occurrence | null, date?: string }
  let loadId = 0;

  const tz = $derived(data?.profile_timezone ?? app.timezone ?? device);

  async function load() {
    const mine = ++loadId;
    try {
      const zone = app.timezone || device;
      const start = zonedToUtc(anchor, "00:00", zone);
      const end = zonedToUtc(addDaysKey(anchor, DAYS), "00:00", zone);
      const result = await api.events(start, end);
      if (mine === loadId) data = result; // a slow answer for an old window must not overwrite the current one
    } catch (error) {
      fail(error);
    }
  }

  onMount(async () => {
    try {
      personas = await api.personas();
    } catch (error) {
      fail(error);
    }
    await load();
  });

  function closeAndReload() {
    sheet = null;
    load();
  }

  /** "Edit the whole series" from one occurrence. An exception row does not carry its series' data: fetch the master. */
  async function openSeries(occurrence) {
    if (occurrence.kind === "series") {
      sheet = { event: occurrence, whole: true };
      return;
    }
    try {
      sheet = { event: await api.getEvent(occurrence.series_id), whole: true };
    } catch (error) {
      fail(error, closeAndReload);
    }
  }

  function shift(days) {
    anchor = addDaysKey(anchor, days);
    data = null;
    load();
  }

  const days = $derived.by(() => {
    const byDay = new Map();
    for (const occurrence of data?.occurrences ?? []) {
      const key = dayKey(new Date(occurrence.starts_at), tz);
      if (!byDay.has(key)) byDay.set(key, []);
      byDay.get(key).push(occurrence);
    }
    return [...byDay.entries()].sort(([a], [b]) => (a < b ? -1 : 1));
  });

  function when(o) {
    if (o.all_day) return "весь день";
    const start = new Date(o.starts_at);
    const end = new Date(o.ends_at);
    return `${timeKey(start, tz)}–${timeKey(end, tz)}`;
  }

  function details(o) {
    const parts = [when(o)];
    if (o.kind === "series") parts.push("🔁");
    if (o.kind === "exception") parts.push("✏️ изменено");
    if (o.visibility === "private") parts.push("🔒");
    if (o.visibility === "shared") parts.push("👥");
    return parts.join("  ");
  }

  function zoneNote(o) {
    if (!o.timezone_mismatch || o.all_day) return "";
    return `Запланировано по времени ${o.timezone}: ${timeKey(new Date(o.starts_at), o.timezone)}`;
  }

  const rangeTitle = $derived(formatRangeTitle(anchor, addDaysKey(anchor, DAYS - 1)));
  const today = $derived(todayKey(tz));
</script>

<div class="screen">
  <h1 class="screen-title">Расписание</h1>

  {#if device !== tz}
    <Banner
      tone="info"
      title="Вы сейчас в другом часовом поясе"
      text="Время здесь показано по профилю ({tz}). В форме события можно выбрать пояс устройства ({device})."
    />
  {/if}

  <div class="row nav">
    <button type="button" class="step" aria-label="Раньше" onclick={() => shift(-DAYS)}>‹</button>
    <button type="button" class="range" onclick={() => ((anchor = todayKey(tz)), (data = null), load())}>{rangeTitle}</button>
    <button type="button" class="step" aria-label="Позже" onclick={() => shift(DAYS)}>›</button>
  </div>

  {#if !data}
    <Spinner />
  {:else}
    {#each days as [key, items] (key)}
      <Section header={key === today ? `Сегодня · ${formatDayHeading(key)}` : formatDayHeading(key)}>
        {#each items as occurrence (occurrence.id + ":" + (occurrence.original_start ?? ""))}
          <Cell
            title={occurrence.title}
            subtitle={details(occurrence)}
            note={zoneNote(occurrence)}
            chevron
            onclick={() => (sheet = { event: occurrence })}
          />
        {/each}
      </Section>
    {/each}

    {#if !days.length}
      <p class="muted empty">На эти две недели ничего не запланировано.</p>
    {/if}
  {/if}

  <Button variant="secondary" onclick={() => (sheet = { event: null, date: anchor > today ? anchor : today })}>＋ Новое событие</Button>
</div>

{#if sheet}
  {#key (sheet.event?.id ?? "new") + ":" + (sheet.event?.original_start ?? "") + (sheet.whole ? ":series" : "")}
    {#if sheet.event && sheet.event.kind !== "single" && !sheet.whole}
      <OccurrenceSheet
        event={sheet.event}
        {personas}
        onclose={() => (sheet = null)}
        onseries={() => openSeries(sheet.event)}
        onsaved={closeAndReload}
      />
    {:else}
      <EventSheet
        event={sheet.event}
        defaultDate={sheet.date}
        profileTz={tz}
        {personas}
        onclose={() => (sheet = null)}
        onsaved={closeAndReload}
      />
    {/if}
  {/key}
{/if}

<style>
  .nav {
    margin-bottom: 16px;
    justify-content: space-between;
  }
  .step {
    width: 40px;
    height: 36px;
    border: 0;
    border-radius: 10px;
    background: var(--surface);
    font-size: 24px;
    line-height: 1;
    color: var(--link);
  }
  .range {
    flex: 1;
    border: 0;
    background: none;
    font-weight: 600;
    font-size: 16px;
  }
  .empty {
    margin: 8px 16px 18px;
    text-align: center;
    font-size: 14px;
  }
</style>
