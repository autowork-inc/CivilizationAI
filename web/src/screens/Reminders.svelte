<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { todayKey } from "../lib/time.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { haptic } from "../lib/tg.js";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Segmented from "../lib/ui/Segmented.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";
  import Switch from "../lib/ui/Switch.svelte";

  let data = $state(null); // the server's last word
  let rows = $state([]); // editable copies of the reminders (inputs are bound to these)
  let qStart = $state("");
  let qEnd = $state("");
  let busy = $state(false);
  let checkinChoice = $state("auto"); // "auto" | "0".."4": bound to the segmented control; a change is sent at once
  const CHECKIN_OPTIONS = [
    { value: "auto", label: "Авто" },
    { value: "0", label: "Выкл" },
    { value: "1", label: "1" },
    { value: "2", label: "2" },
    { value: "3", label: "3" },
    { value: "4", label: "4" },
  ];
  const choiceOf = (perDay) => (perDay === null || perDay === undefined ? "auto" : String(perDay));

  function apply(fresh) {
    data = fresh;
    checkinChoice = choiceOf(fresh.checkins.per_day);
    rows = fresh.reminders.map((reminder) => ({ ...reminder }));
    qStart = fresh.quiet_hours.start;
    qEnd = fresh.quiet_hours.end;
  }

  async function load() {
    try {
      apply(await api.reminders());
    } catch (error) {
      fail(error);
    }
  }

  onMount(load);

  const quietDirty = $derived(!!data && (qStart !== data.quiet_hours.start || qEnd !== data.quiet_hours.end));
  const quietValid = $derived(!!qStart && !!qEnd && qStart !== qEnd);
  const today = $derived(data ? todayKey(data.timezone) : "");

  async function saveQuiet() {
    busy = true;
    try {
      apply(await api.setQuietHours(qStart, qEnd));
      haptic("success");
      showToast("Тихие часы сохранены");
    } catch (error) {
      fail(error); // the refusal explains itself; the edit stays on screen (with its Save button) so it can be adjusted
    } finally {
      busy = false;
    }
  }

  // The user picked another number of check-ins: the server saves it in the database right away (the dispatcher reads it on its next tick).
  $effect(() => {
    if (!data || checkinChoice === choiceOf(data.checkins.per_day)) return;
    saveCheckins(checkinChoice);
  });

  async function saveCheckins(choice) {
    try {
      apply(await api.setCheckins(choice === "auto" ? null : Number(choice)));
      haptic("success");
      showToast(choice === "0" ? "Чек-ины выключены" : "Чек-ины сохранены");
    } catch (error) {
      fail(error);
      load(); // show what the server really has
    }
  }

  async function saveRow(row) {
    if (!row.local_time) return load(); // the time field was cleared: nothing to save, show the saved value again
    try {
      const fresh = await api.setReminder(row.trigger_key, { local_time: row.local_time, enabled: row.enabled });
      apply(fresh);
      haptic("success");
      const moved = fresh.quiet_hours_adjusted; // the time fell into the quiet hours: the server moved them, and says so
      showToast(
        moved
          ? `Напоминание сохранено. Тихие часы теперь ${moved.start}–${moved.end} (было ${moved.previous_start}–${moved.previous_end})`
          : row.enabled
            ? "Напоминание сохранено"
            : "Напоминание выключено",
      );
    } catch (error) {
      fail(error);
      load(); // a refused value must not stay on screen as if it were saved: show what the server really has
    }
  }
</script>

<div class="screen">
  <h1 class="screen-title">Напоминания</h1>

  {#if !data}
    <Spinner />
  {:else}
    <Section
      header="Тихие часы и начало дня"
      footer="В тихие часы я не пишу первым. «Отбой» — когда день заканчивается, «Начало дня» — когда я могу написать первым (подъём). Окно может переходить через полночь: например, с 22:00 до 08:00."
    >
      <div class="card">
        <div class="pair">
          <Field label="Отбой (с)" type="time" bind:value={qStart} />
          <Field label="Начало дня (до)" type="time" bind:value={qEnd} />
        </div>
      </div>
    </Section>
    {#if quietDirty}
      <div class="action">
        {#if !quietValid}<p class="problem">Начало и конец не могут совпадать.</p>{/if}
        <Button onclick={saveQuiet} disabled={!quietValid} loading={busy}>Сохранить тихие часы</Button>
      </div>
    {/if}

    <Section
      header="Чек-ины"
      footer="Сколько раз в день я сам заговорю с вами, если вы молчите. «Авто» — 3–4 раза. Время я выбираю в течение дня, чтобы не писать на рассвете и перед сном."
    >
      <div class="card checkins">
        <Segmented options={CHECKIN_OPTIONS} bind:value={checkinChoice} />
        <p class="today">
          {#if data.checkins.today.length}
            Сегодня: {data.checkins.today.join(", ")}
          {:else if checkinChoice === "0"}
            Чек-ины выключены.
          {:else}
            Сегодня чек-инов не будет.
          {/if}
        </p>
      </div>
    </Section>

    <Section
      header="Когда писать первым"
      footer="Время — по часовому поясу профиля ({data.timezone}); поменять его можно на вкладке «Мозг». Каждое напоминание приходит раз в день. Если время попадёт в тихие часы, я сам подвину их, чтобы напоминание дошло. Если вы выбрали время, которое прошло меньше {data.grace_minutes} минут назад, оно придёт сегодня, иначе завтра."
    >
      {#each rows as row (row.trigger_key)}
        <div class="reminder">
          <Switch
            label={row.title}
            bind:checked={row.enabled}
            onchange={() => saveRow(row)}
          />
          <p class="desc">{row.description}</p>
          <Field label="Время" type="time" bind:value={row.local_time} onchange={() => saveRow(row)} />
          {#if row.last_fired_on === today}
            <p class="fired">Сегодня уже приходило. Новое время заработает завтра.</p>
          {/if}
        </div>
      {/each}
    </Section>
  {/if}
</div>

<style>
  .card {
    background: var(--surface);
  }
  .pair {
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
  .pair > :global(* + *) {
    border-left: 0.5px solid var(--separator);
  }
  .checkins {
    padding: 12px 16px;
  }
  .today {
    font-size: 13px;
    color: var(--hint);
    margin-top: 8px;
  }
  .action {
    margin: -8px 0 22px;
  }
  .problem {
    color: var(--danger);
    font-size: 13px;
    text-align: center;
    margin-bottom: 8px;
  }
  .reminder {
    background: var(--surface);
  }
  .reminder + .reminder {
    border-top: 0.5px solid var(--separator);
  }
  .desc {
    font-size: 13px;
    color: var(--hint);
    padding: 0 16px 6px;
  }
  .fired {
    font-size: 12px;
    color: var(--hint);
    padding: 0 16px 10px;
  }
</style>
