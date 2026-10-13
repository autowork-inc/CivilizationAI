<script>
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { VISIBILITY, VISIBILITY_HINT } from "../lib/labels.js";
  import { addDaysKey, dayKey, timeKey, zonedToUtc } from "../lib/time.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { confirmNative, haptic } from "../lib/tg.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Segmented from "../lib/ui/Segmented.svelte";
  import SelectField from "../lib/ui/SelectField.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";

  // ONE occurrence of a series. `event` is a generated occurrence (kind "series": the first change creates an exception row) or an
  // already changed one (kind "exception": it is edited with its own version). The zone and the repetition belong to the whole
  // series, so they are not here; `onseries` opens the editor of the whole series instead.
  let { event, personas = [], onclose, onsaved, onseries } = $props();

  /* svelte-ignore state_referenced_locally */
  const isException = event.kind === "exception";
  /* svelte-ignore state_referenced_locally */
  const seriesId = isException ? event.series_id : event.id;
  /* svelte-ignore state_referenced_locally */
  const tz = event.timezone;
  /* svelte-ignore state_referenced_locally */
  const allDay = event.all_day;
  /* svelte-ignore state_referenced_locally */
  const start = new Date(event.starts_at);
  /* svelte-ignore state_referenced_locally */
  const end = new Date(event.ends_at);
  const date0 = dayKey(start, tz);
  const start0 = allDay ? "00:00" : timeKey(start, tz);
  const end0 = allDay ? "00:00" : timeKey(end, tz);

  /* svelte-ignore state_referenced_locally */
  let title = $state(event.title);
  /* svelte-ignore state_referenced_locally */
  let description = $state(event.description ?? "");
  let date = $state(date0);
  let startTime = $state(start0);
  let endTime = $state(end0);
  /* svelte-ignore state_referenced_locally */
  let visibility = $state(event.visibility);
  /* svelte-ignore state_referenced_locally */
  let personaId = $state(event.persona_id ?? "");
  let busy = $state(false);

  const personaOptions = $derived([{ value: "", label: "Без темы" }, ...personas.map((p) => ({ value: p.id, label: p.title }))]);
  const timesDirty = $derived(date !== date0 || startTime !== start0 || endTime !== end0);
  const dirty = $derived(
    timesDirty ||
      title.trim() !== event.title ||
      description.trim() !== (event.description ?? "") ||
      visibility !== event.visibility ||
      (personaId || null) !== (event.persona_id ?? null),
  );
  const problem = $derived.by(() => {
    if (!title.trim()) return "Введите название.";
    if (!date) return "Выберите дату.";
    if (!allDay && endTime === startTime) return "Начало и конец совпадают.";
    return "";
  });

  function instants() {
    if (allDay) return [zonedToUtc(date, "00:00", tz), zonedToUtc(addDaysKey(date, 1), "00:00", tz)];
    const endDate = endTime < startTime ? addDaysKey(date, 1) : date; // 22:00 -> 01:00 runs over midnight
    return [zonedToUtc(date, startTime, tz), zonedToUtc(endDate, endTime, tz)];
  }

  // only what the user changed is sent: the rest keeps following the series
  function changes() {
    const body = {};
    if (title.trim() !== event.title) body.title = title.trim();
    if (description.trim() !== (event.description ?? "")) body.description = description.trim();
    if (visibility !== event.visibility) body.visibility = visibility;
    if ((personaId || null) !== (event.persona_id ?? null)) body.persona_id = personaId || null;
    if (timesDirty) {
      const [s, e] = instants();
      body.starts_at = s.toISOString();
      body.ends_at = e.toISOString();
    }
    return body;
  }

  async function run(action, doneText) {
    busy = true;
    try {
      await action();
      haptic("success");
      showToast(doneText);
      onsaved();
    } catch (error) {
      fail(error, onsaved); // a 409 means it changed elsewhere: close and show the fresh state
    } finally {
      busy = false;
    }
  }

  const save = () =>
    run(
      () =>
        isException
          ? api.patchEvent(event.id, { expected_updated_at: event.updated_at, ...changes() })
          : api.overrideOccurrence(event.id, { original_start: event.original_start, ...changes() }),
      "Сохранил",
    );

  async function remove() {
    if (await confirmNative("Убрать только это повторение? Остальные останутся.")) {
      run(() => api.cancelOccurrence(seriesId, event.original_start), "Повторение убрано");
    }
  }

  const revert = () => run(() => api.deleteEvent(event.id, event.updated_at), "Повторение снова как в серии");
</script>

<Sheet open title="Одно повторение" {onclose}>
  <div class="stack">
    <Banner
      tone="info"
      title={isException ? "Это повторение изменено" : "Это повторение серии"}
      text={isException
        ? "Оно отличается от остальных. Остальные повторения не затронуты."
        : "Изменения коснутся только его. Остальные повторения останутся как были."}
    />

    <div class="card">
      <Field label="Название" bind:value={title} maxlength={200} />
      <Field label="Дата" type="date" bind:value={date} hint="Перенесите на другой день, поменяв дату." />
      {#if !allDay}
        <div class="times">
          <Field label="Начало" type="time" bind:value={startTime} />
          <Field label="Конец" type="time" bind:value={endTime} hint={endTime < startTime ? "Закончится на следующий день" : ""} />
        </div>
      {/if}
    </div>

    <div class="card">
      <div class="seg">
        <span class="seglabel">Кто видит</span>
        <Segmented options={VISIBILITY} bind:value={visibility} />
        <span class="hint">{VISIBILITY_HINT[visibility]}</span>
      </div>
      <SelectField label="Тема" bind:value={personaId} options={personaOptions} />
      <Field label="Заметка" multiline bind:value={description} maxlength={2000} />
    </div>

    {#if problem}<p class="problem">{problem}</p>{/if}
    <Button onclick={save} disabled={!!problem || !dirty} loading={busy}>Сохранить это повторение</Button>
    {#if isException}
      <Button variant="secondary" onclick={revert} disabled={busy}>Вернуть, как в серии</Button>
    {/if}
    <Button variant="secondary" onclick={onseries} disabled={busy}>Изменить всю серию…</Button>
    <Button variant="danger" onclick={remove} disabled={busy}>Убрать только это повторение</Button>
  </div>
</Sheet>

<style>
  .card {
    border-radius: var(--radius);
    overflow: hidden;
  }
  .card > :global(* + *) {
    border-top: 0.5px solid var(--separator);
  }
  .times {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
  .times > :global(* + *) {
    border-left: 0.5px solid var(--separator);
  }
  .seg {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 16px 12px;
    background: var(--surface);
  }
  .seglabel,
  .hint {
    font-size: 13px;
    color: var(--hint);
  }
  .hint {
    font-size: 12px;
  }
  .problem {
    color: var(--danger);
    font-size: 14px;
    text-align: center;
  }
</style>
