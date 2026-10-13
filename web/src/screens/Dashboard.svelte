<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { MOOD_LABELS } from "../lib/labels.js";
  import BarChart from "../lib/charts/BarChart.svelte";
  import LineChart from "../lib/charts/LineChart.svelte";
  import WeekMood from "../lib/charts/WeekMood.svelte";
  import Empty from "../lib/ui/Empty.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";

  const DAYS = 14;
  const minMessages = 3; // the server judges a day only from this many of the user's messages (MOOD_MIN_MESSAGES)
  let stats = $state(null);

  async function load() {
    try {
      stats = await api.dashboard(DAYS);
    } catch (error) {
      fail(error);
    }
  }

  onMount(load);

  const percent = (ratio) => (ratio === null || ratio === undefined ? "—" : `${Math.round(ratio * 100)}%`);
  const moodDays = $derived(stats ? stats.mood.labels.map((label, i) => ({ date: stats.dates[i], label })).filter((d) => d.label) : []);
  const hasWeek = $derived(!!stats && stats.mood.week.some((d) => d.score !== null && d.score !== undefined));
  const noData = $derived(
    !!stats &&
      stats.mood.days_with_data === 0 &&
      stats.mood.daily.days_with_data === 0 &&
      stats.tasks.created === 0 &&
      stats.tasks.done === 0 &&
      stats.xp.earned === 0 &&
      stats.xp.penalty === 0 &&
      stats.cards.reviewed === 0,
  );
</script>

<div class="screen">
  <h1 class="screen-title">Дашборд</h1>
  {#if !stats}
    <Spinner />
  {:else}
    <p class="mb-4 px-1 text-sm text-hint">Последние {stats.days} дней · {stats.timezone}</p>

    {#if noData}
      <Empty emoji="📊" title="Пока нечего показывать" text="Графики оживут, когда вы начнёте закрывать задачи, отвечать на карточки и отмечать настроение." />
    {/if}

    <Section
      header="Настроение по дням недели"
      footer="Это оценка, а не диагноз: по вашим сообщениям и голосовым за день (нужно хотя бы {minMessages}). Дни без записи остаются пустыми, а не «хорошими»."
    >
      <div class="bg-surface p-4">
        {#if !hasWeek}
          <p class="text-sm text-hint">На этой неделе пока нет оценок: напишите мне пару сообщений, и график появится.</p>
        {/if}
        <WeekMood week={stats.mood.week} />
      </div>
    </Section>

    {#if stats.mood.daily.days_with_data > 0}
      <Section header="Настроение за {stats.days} дней">
        <div class="bg-surface p-4">
          <LineChart labels={stats.dates} values={stats.mood.daily.score} min={-2} max={2} ariaLabel="Настроение по дням" />
        </div>
      </Section>
    {/if}

    {#if stats.mood.days_with_data > 0}
      <Section header="Подтверждённые состояния" footer="Только то, что вы сами подтвердили в недельном обзоре.">
        <div class="bg-surface p-4">
          <div class="flex flex-wrap gap-1.5">
            {#each moodDays as day (day.date)}
              <span class="rounded-full bg-bg px-2 py-0.5 text-xs">{day.date.slice(8, 10)}.{day.date.slice(5, 7)} {MOOD_LABELS[day.label]?.icon ?? ""} {MOOD_LABELS[day.label]?.label ?? day.label}</span>
            {/each}
          </div>
        </div>
      </Section>
    {/if}

    <Section header="Задачи" footer="Шаги планов: закрытые, созданные и отменённые по дням.">
      <div class="bg-surface p-4">
        <div class="mb-3 grid grid-cols-3 gap-2 text-center">
          <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold text-body">{stats.tasks.done}</div><div class="text-[11px] text-hint">закрыто</div></div>
          <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{stats.tasks.open_backlog}</div><div class="text-[11px] text-hint">в работе</div></div>
          <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold text-hint">{stats.tasks.dropped}</div><div class="text-[11px] text-hint">отменено</div></div>
        </div>
        <BarChart
          labels={stats.dates}
          series={[{ name: "закрыто", values: stats.tasks.daily.done, color: "#22c55e" }]}
          height={90}
          ariaLabel="Закрытые шаги по дням"
        />
      </div>
    </Section>

    <Section header="XP: набрано и потеряно" footer="Столбцы вверх — награды, вниз — штрафы за паузу в тренировках и просроченные задачи.">
      <div class="bg-surface p-4">
        <div class="mb-3 grid grid-cols-3 gap-2 text-center">
          <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold text-body">+{stats.xp.earned}</div><div class="text-[11px] text-hint">набрано</div></div>
          <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold {stats.xp.penalty ? 'text-danger' : ''}">{stats.xp.penalty}</div><div class="text-[11px] text-hint">штрафы</div></div>
          <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{stats.xp.level}</div><div class="text-[11px] text-hint">уровень</div></div>
        </div>
        <BarChart
          labels={stats.dates}
          series={[
            { name: "набрано", values: stats.xp.daily.earned, color: "#22c55e" },
            { name: "штраф", values: stats.xp.daily.penalty, color: "var(--danger)" },
          ]}
          height={130}
          ariaLabel="Начисление и штрафы XP по дням"
        />
        <div class="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-hint">
          <span>🎯 Фокус: {stats.xp.by_category.focus}</span>
          <span>🧠 Интеллект: {stats.xp.by_category.intellect}</span>
          <span>💪 Тело: {stats.xp.by_category.body}</span>
          <span>🤝 Связи: {stats.xp.by_category.connection}</span>
        </div>
      </div>
    </Section>

    <Section header="Карточки" footer="Точность считается по последнему ответу на каждую карточку.">
      <div class="grid grid-cols-3 gap-2 bg-surface p-4 text-center">
        <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{stats.cards.reviewed}</div><div class="text-[11px] text-hint">пройдено</div></div>
        <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{percent(stats.cards.accuracy)}</div><div class="text-[11px] text-hint">верно</div></div>
        <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{stats.cards.due_today}</div><div class="text-[11px] text-hint">на сегодня</div></div>
      </div>
    </Section>

    <Section header="Живой разговор" footer="Сколько раз я писал первым и как часто вы отвечали в течение {stats.proactive.answer_window_hours} часов.">
      <div class="grid grid-cols-3 gap-2 bg-surface p-4 text-center">
        <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{stats.proactive.checkins_sent}</div><div class="text-[11px] text-hint">отправлено</div></div>
        <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{stats.proactive.checkins_answered}</div><div class="text-[11px] text-hint">с ответом</div></div>
        <div class="rounded-lg bg-bg p-2"><div class="text-lg font-semibold">{percent(stats.proactive.response_rate)}</div><div class="text-[11px] text-hint">доля ответов</div></div>
      </div>
    </Section>
  {/if}
</div>
