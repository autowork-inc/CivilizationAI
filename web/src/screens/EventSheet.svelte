<script>
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { REPEAT_OPTIONS, VISIBILITY, VISIBILITY_HINT } from "../lib/labels.js";
  import { addDaysKey, dayKey, parseUntil, timeKey, untilValue, weekdayCode, zonedToUtc } from "../lib/time.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { confirmNative, deviceTimeZone, haptic } from "../lib/tg.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Segmented from "../lib/ui/Segmented.svelte";
  import SelectField from "../lib/ui/SelectField.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";
  import Switch from "../lib/ui/Switch.svelte";

  // `event` is an occurrence as the API returns it (null = a new event). The parent re-creates the sheet per occurrence.
  let { event, defaultDate = "", profileTz, personas = [], onclose, onsaved } = $props();

  /* svelte-ignore state_referenced_locally */
  const isNew = event === null;
  /* svelte-ignore state_referenced_locally */
  const isSeries = event?.kind === "series";
  const device = deviceTimeZone();

  const SIMPLE_RULE = /^FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(?:;BYDAY=([A-Z]{2}))?(?:;UNTIL=(\d{8}T\d{6}Z))?$/;

  /* svelte-ignore state_referenced_locally */
  const tz0 = event?.timezone ?? profileTz;
  // A series is edited as a whole: its form shows the series' own first start, not the clicked occurrence.
  /* svelte-ignore state_referenced_locally */
  const base = event ? new Date(isSeries ? event.series_starts_at : event.starts_at) : null;
  /* svelte-ignore state_referenced_locally */
  const duration = event ? new Date(event.ends_at) - new Date(event.starts_at) : 3_600_000;
  /* svelte-ignore state_referenced_locally */
  const allDay0 = event?.all_day ?? false;
  /* svelte-ignore state_referenced_locally */
  const date0 = event ? dayKey(base, tz0) : defaultDate;
  /* svelte-ignore state_referenced_locally */
  const start0 = event && !allDay0 ? timeKey(base, tz0) : "09:00";
  /* svelte-ignore state_referenced_locally */
  const end0 = event && !allDay0 ? timeKey(new Date(base.getTime() + duration), tz0) : "10:00";

  function parseRule(rule) {
    if (!rule) return { mode: "", until: "" };
    const match = SIMPLE_RULE.exec(rule);
    if (match && (!match[2] || match[2] === weekdayCode(date0))) {
      return { mode: match[1], until: match[3] ? dayKey(parseUntil(rule), tz0) : "" };
    }
    return { mode: "__keep", until: "" }; // a rule this form cannot express: keep it as it is unless the user picks another
  }
  /* svelte-ignore state_referenced_locally */
  const rule0 = parseRule(event?.recurrence_rule);

  /* svelte-ignore state_referenced_locally */
  let title = $state(event?.title ?? "");
  /* svelte-ignore state_referenced_locally */
  let description = $state(event?.description ?? "");
  /* svelte-ignore state_referenced_locally */
  let date = $state(date0);
  /* svelte-ignore state_referenced_locally */
  let allDay = $state(allDay0);
  /* svelte-ignore state_referenced_locally */
  let startTime = $state(start0);
  /* svelte-ignore state_referenced_locally */
  let endTime = $state(end0);
  /* svelte-ignore state_referenced_locally */
  let tz = $state(tz0);
  /* svelte-ignore state_referenced_locally */
  let repeat = $state(rule0.mode);
  /* svelte-ignore state_referenced_locally */
  let until = $state(rule0.until);
  /* svelte-ignore state_referenced_locally */
  let visibility = $state(event?.visibility ?? "busy_only");
  /* svelte-ignore state_referenced_locally */
  let personaId = $state(event?.persona_id ?? "");
  let busy = $state(false);

  const zoneOptions = $derived.by(() => {
    const seen = new Set();
    const out = [];
    for (const [zone, tag] of [[profileTz, "профиль"], [device, "устройство"], [tz0, "событие"]]) {
      if (seen.has(zone)) continue;
      seen.add(zone);
      out.push({ value: zone, label: `${zone} (${tag})` });
    }
    return out;
  });
  /* svelte-ignore state_referenced_locally */
  const repeatOptions = rule0.mode === "__keep" ? [...REPEAT_OPTIONS, { value: "__keep", label: "Своё правило (оставить)" }] : REPEAT_OPTIONS;
  const personaOptions = $derived([{ value: "", label: "Без темы" }, ...personas.map((p) => ({ value: p.id, label: p.title }))]);

  function instants() {
    if (allDay) return [zonedToUtc(date, "00:00", tz), zonedToUtc(addDaysKey(date, 1), "00:00", tz)];
    const endDate = endTime < startTime ? addDaysKey(date, 1) : date; // 22:00 -> 01:00 runs over midnight
    return [zonedToUtc(date, startTime, tz), zonedToUtc(endDate, endTime, tz)];
  }

  function buildRule() {
    if (repeat === "") return null;
    if (repeat === "__keep") return event.recurrence_rule;
    let rule = `FREQ=${repeat}`;
    if (repeat === "WEEKLY") rule += `;BYDAY=${weekdayCode(date)}`;
    if (until) rule += `;UNTIL=${untilValue(until, tz)}`;
    return rule;
  }

  const problem = $derived.by(() => {
    if (!title.trim()) return "Введите название.";
    if (!date) return "Выберите дату.";
    if (!allDay && endTime === startTime) return "Начало и конец совпадают.";
    if (repeat && repeat !== "__keep" && until && until < date) return "Повторы не могут заканчиваться раньше первого события.";
    return "";
  });

  const timesDirty = $derived(date !== date0 || startTime !== start0 || endTime !== end0 || tz !== tz0 || allDay !== allDay0);
  const dirty = $derived(
    isNew ||
      timesDirty ||
      title.trim() !== event.title ||
      description.trim() !== (event.description ?? "") ||
      visibility !== event.visibility ||
      (personaId || null) !== (event.persona_id ?? null) ||
      repeat !== rule0.mode ||
      until !== rule0.until,
  );

  async function save() {
    busy = true;
    try {
      const [start, end] = instants();
      const common = {
        title: title.trim(),
        description: description.trim() || null,
        all_day: allDay,
        timezone: tz,
        visibility,
        persona_id: personaId || null,
        recurrence_rule: buildRule(),
      };
      if (isNew) {
        await api.createEvent({ ...common, starts_at: start.toISOString(), ends_at: end.toISOString() });
      } else {
        const patch = { ...common, expected_updated_at: event.updated_at };
        if (timesDirty) Object.assign(patch, { starts_at: start.toISOString(), ends_at: end.toISOString() });
        const result = await api.patchEvent(event.id, patch);
        if (result.exceptions_reset) showToast("Серия изменилась, отмены отдельных повторений сброшены");
      }
      haptic("success");
      showToast("Сохранил");
      onsaved();
    } catch (error) {
      fail(error, onsaved);
    } finally {
      busy = false;
    }
  }

  async function run(action, doneText) {
    busy = true;
    try {
      await action();
      haptic("success");
      showToast(doneText);
      onsaved();
    } catch (error) {
      fail(error, onsaved);
    } finally {
      busy = false;
    }
  }

  async function removeSingle() {
    if (await confirmNative("Удалить событие?")) run(() => api.deleteEvent(event.id, event.updated_at), "Удалил");
  }
  async function removeSeries() {
    if (await confirmNative("Удалить всю серию целиком? Вместе со всеми изменениями отдельных повторений.")) {
      run(() => api.deleteEvent(event.id, event.updated_at), "Серия удалена");
    }
  }
</script>

<!-- new event, a single event, or a WHOLE series (one occurrence of a series is edited in OccurrenceSheet) -->
<Sheet open title={isNew ? "Новое событие" : isSeries ? "Вся серия" : "Событие"} {onclose}>
  <div class="stack">
    {#if tz !== device}
      <Banner
        tone="info"
        title="Время по поясу {tz}"
        text="Сейчас вы в {device}. Событие запланировано по времени {tz}, а на других экранах пересчитается."
        action="Взять {device}"
        onaction={() => (tz = device)}
      />
    {/if}

    {#if isSeries}
      <Banner
        tone="warning"
        text="Изменения коснутся всех повторений. Если поменять время, дату или правило повторения, отдельно изменённые и убранные повторения сбросятся."
      />
    {/if}

    <div class="card">
      <Field label="Название" bind:value={title} maxlength={200} placeholder="Например, тренировка" />
      <Field label={isSeries ? "Дата первого события" : "Дата"} type="date" bind:value={date} />
      <Switch label="Весь день" bind:checked={allDay} />
      {#if !allDay}
        <div class="times">
          <Field label="Начало" type="time" bind:value={startTime} />
          <Field label="Конец" type="time" bind:value={endTime} hint={endTime < startTime ? "Закончится на следующий день" : ""} />
        </div>
      {/if}
      <SelectField label="Часовой пояс события" bind:value={tz} options={zoneOptions} />
    </div>

    <div class="card">
      <SelectField label="Повторять" bind:value={repeat} options={repeatOptions} />
      {#if repeat && repeat !== "__keep"}
        <Field label="Повторять до (необязательно)" type="date" bind:value={until} />
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
    <Button onclick={save} disabled={!!problem || !dirty} loading={busy}>{isNew ? "Добавить" : "Сохранить"}</Button>

    {#if !isNew}
      {#if isSeries}
        <Button variant="danger" onclick={removeSeries} disabled={busy}>Удалить всю серию</Button>
      {:else}
        <Button variant="danger" onclick={removeSingle} disabled={busy}>Удалить событие</Button>
      {/if}
    {/if}
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
