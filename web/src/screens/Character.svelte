<script>
  import { onDestroy, onMount } from "svelte";
  import { ApiError, api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { BRANCHES, PENALTY_LABELS, plural } from "../lib/labels.js";
  import { shrinkImage } from "../lib/image.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { haptic } from "../lib/tg.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";
  import Switch from "../lib/ui/Switch.svelte";

  let data = $state(null); // the server's last word (GET /api/character/me)
  let avatarUrl = $state(null);
  let nameOpen = $state(false);
  let nameDraft = $state("");
  let busy = $state(false);
  let fileInput = $state();

  async function load() {
    try {
      data = await api.character();
      await showAvatar();
    } catch (error) {
      fail(error);
    }
  }

  async function showAvatar() {
    const fresh = data?.has_avatar ? await api.avatar() : null;
    if (avatarUrl) URL.revokeObjectURL(avatarUrl);
    avatarUrl = fresh;
  }

  onMount(load);
  onDestroy(() => {
    if (avatarUrl) URL.revokeObjectURL(avatarUrl);
  });

  const branches = $derived(
    BRANCHES.map((b) => {
      const xp = data?.xp_by_category?.[b.id] ?? 0;
      return { ...b, xp, level: Math.floor(xp / 100) + 1, progress: xp % 100 };
    }),
  );
  const initial = $derived((data?.character_name ?? "?").trim().slice(0, 1).toUpperCase());
  const penalties = $derived(data?.penalties);
  const days = (n) => plural(n, "день", "дня", "дней");

  async function saveName() {
    busy = true;
    try {
      data = await api.patchCharacter({ character_name: nameDraft.trim() === "" ? null : nameDraft.trim() });
      nameOpen = false;
      haptic("success");
      showToast("Имя сохранено");
    } catch (error) {
      fail(error);
    } finally {
      busy = false;
    }
  }

  async function pickAvatar(event) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    try {
      const small = await shrinkImage(file);
      data = await api.uploadAvatar(small);
      await showAvatar();
      haptic("success");
      showToast("Аватар обновлён");
    } catch (error) {
      if (error instanceof ApiError) fail(error);
      else showToast(error?.message || "Не получилось прочитать картинку.", "error"); // a local problem (not an image, cannot be shrunk): said in the user's words
    }
  }

  async function setFlag(flag, value) {
    try {
      data = await api.patchCharacter({ [flag]: value });
    } catch (error) {
      fail(error);
      load(); // a refused switch must not stay flipped on screen
    }
  }
</script>

<div class="screen">
  <h1 class="screen-title">Персонаж</h1>

  {#if !data}
    <Spinner />
  {:else}
    <section class="mb-5 rounded-card bg-surface p-4">
      <div class="flex items-center gap-4">
        <button
          type="button"
          class="relative flex h-20 w-20 shrink-0 items-center justify-center overflow-hidden rounded-full bg-bg text-3xl font-semibold text-hint"
          aria-label="Сменить аватар"
          onclick={() => fileInput.click()}
        >
          {#if avatarUrl}<img src={avatarUrl} alt="" class="h-full w-full object-cover" />{:else}{initial}{/if}
          <span class="absolute inset-x-0 bottom-0 bg-black/40 py-0.5 text-center text-[10px] text-white">изменить</span>
        </button>
        <input bind:this={fileInput} type="file" accept="image/*" class="hidden" onchange={pickAvatar} />
        <div class="min-w-0 flex-1">
          <button
            type="button"
            class="block max-w-full truncate text-left text-xl font-bold"
            onclick={() => {
              nameDraft = data.name_is_default ? "" : data.character_name;
              nameOpen = true;
            }}
          >
            {data.character_name} <span class="text-sm font-normal text-hint">✎</span>
          </button>
          <div class="mt-1 flex items-baseline gap-2">
            <span class="text-3xl font-extrabold text-accent">{data.level}</span>
            <span class="text-sm text-hint">уровень</span>
          </div>
        </div>
      </div>

      <div class="mt-4">
        <div class="mb-1 flex justify-between text-xs text-hint">
          <span>{data.current_level_xp} / 100 XP</span>
          <span>до {data.level + 1} уровня — {data.xp_to_next_level} XP</span>
        </div>
        <div class="h-3 overflow-hidden rounded-full bg-bg" role="progressbar" aria-label="Опыт до следующего уровня" aria-valuemin="0" aria-valuemax="100" aria-valuenow={data.current_level_xp}>
          <div class="h-full rounded-full bg-accent transition-all duration-500" style="width: {data.current_level_xp}%"></div>
        </div>
        <p class="mt-2 text-xs text-hint">Всего {data.total_xp} XP · сегодня {data.xp_today} из {data.daily_cap}</p>
      </div>
    </section>

    <Section header="Ветки развития">
      <div class="space-y-4 bg-surface p-4">
        {#each branches as b (b.id)}
          <div>
            <div class="mb-1 flex items-center justify-between text-sm">
              <span class="font-medium">{b.icon} {b.label}</span>
              <span class="text-hint">ур. {b.level} · {b.xp} XP</span>
            </div>
            <div class="h-2.5 overflow-hidden rounded-full bg-bg" role="progressbar" aria-label={b.label} aria-valuemin="0" aria-valuemax="100" aria-valuenow={b.progress}>
              <div class="h-full rounded-full {b.bar} transition-all duration-500" style="width: {b.progress}%"></div>
            </div>
          </div>
        {/each}
      </div>
    </Section>

    <Section
      header="Серия и штрафы"
      footer="Серия — дни подряд, когда вы получили хотя бы один XP. Ночью я снимаю XP за долгую паузу в тренировках (дольше {penalties.body_threshold_days} дней) и за задачи, просроченные больше чем на 48 часов. Ниже нуля ветка не уходит."
    >
      <div class="space-y-3 bg-surface p-4">
        <div class="flex items-center justify-between">
          <span class="text-sm font-medium">🔥 Серия</span>
          <span class="text-lg font-bold">
            {penalties.streak_days}
            <span class="text-sm font-normal text-hint">{days(penalties.streak_days)}</span>
          </span>
        </div>
        {#if penalties.streak_days === 0}
          <p class="text-xs text-hint">Серии пока нет: закройте шаг задачи, ответьте на карточку или запишите тренировку.</p>
        {/if}

        {#if penalties.body_decay_active}
          <Banner
            tone="warning"
            title="Тело теряет XP"
            text="Тренировок не было {penalties.body_idle_days} {days(penalties.body_idle_days)}: сегодня ночью спишу XP ветки «Тело». Запишите любую тренировку, чтобы это остановить."
          />
        {:else if penalties.body_idle_days !== null && penalties.body_idle_days >= penalties.body_threshold_days - 1}
          <Banner
            tone="info"
            text="Без тренировки уже {penalties.body_idle_days} {days(penalties.body_idle_days)}. После {penalties.body_threshold_days} дней XP тела начнёт снижаться."
          />
        {/if}

        <div class="grid grid-cols-3 gap-2 text-center">
          <div class="rounded-lg bg-bg p-2">
            <div class="text-base font-semibold {penalties.penalty_today ? 'text-danger' : ''}">−{penalties.penalty_today}</div>
            <div class="text-[11px] text-hint">сегодня</div>
          </div>
          <div class="rounded-lg bg-bg p-2">
            <div class="text-base font-semibold {penalties.penalty_week ? 'text-danger' : ''}">−{penalties.penalty_week}</div>
            <div class="text-[11px] text-hint">за неделю</div>
          </div>
          <div class="rounded-lg bg-bg p-2">
            <div class="text-base font-semibold {penalties.penalty_total ? 'text-danger' : ''}">−{penalties.penalty_total}</div>
            <div class="text-[11px] text-hint">всего</div>
          </div>
        </div>

        {#if penalties.recent.length}
          <ul class="divide-y divide-line text-sm">
            {#each penalties.recent as p, i (i)}
              <li class="flex items-center justify-between py-1.5">
                <span>{PENALTY_LABELS[p.kind] ?? PENALTY_LABELS.other}</span>
                <span class="text-danger">{p.amount} XP</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="text-xs text-hint">Штрафов не было. Так держать.</p>
        {/if}
      </div>
    </Section>

    <Section header="Что видят друзья в Таверне" footer="Друзья из вашего списка связей видят только то, что включено здесь. Настроение — только подтверждённое вами.">
      <Switch label="Уровень и опыт" checked={data.visibility.show_level} onchange={(v) => setFlag("show_level", v)} />
      <Switch label="Настроение" checked={data.visibility.show_mood} onchange={(v) => setFlag("show_mood", v)} />
      <Switch label="Интересы" checked={data.visibility.show_interests} onchange={(v) => setFlag("show_interests", v)} />
      <Switch label="Открыт к новым знакомствам" checked={data.visibility.open_for_intros} onchange={(v) => setFlag("open_for_intros", v)} />
    </Section>
  {/if}
</div>

<Sheet open={nameOpen} title="Имя персонажа" onclose={() => (nameOpen = false)}>
  <div class="stack">
    <Field label="Имя" bind:value={nameDraft} placeholder={data?.name_is_default ? data.character_name : "Как вас зовут в игре"} maxlength={40} hint="Пустое имя вернёт имя из Telegram." />
    <Button onclick={saveName} loading={busy}>Сохранить</Button>
  </div>
</Sheet>
