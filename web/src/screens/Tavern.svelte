<script>
  import { onDestroy, onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { MOOD_LABELS } from "../lib/labels.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { haptic } from "../lib/tg.js";
  import Empty from "../lib/ui/Empty.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";

  let friends = $state(null); // GET /api/tavern/feed -> friends[]
  let avatars = $state({}); // contact_id -> object URL (the picture is behind the signed header: fetched as a blob)
  let sending = $state({}); // contact_id -> true while the cheer is on its way

  async function load() {
    try {
      friends = (await api.tavern()).friends;
    } catch (error) {
      fail(error);
      return;
    }
    for (const friend of friends) {
      if (friend.avatar_url && !avatars[friend.contact_id]) {
        api.friendAvatar(friend.avatar_url).then((url) => {
          if (url) avatars[friend.contact_id] = url;
        });
      }
    }
  }

  onMount(load);
  onDestroy(() => {
    for (const url of Object.values(avatars)) URL.revokeObjectURL(url);
  });

  async function cheer(friend) {
    if (sending[friend.contact_id] || friend.you_cheered_today) return;
    sending[friend.contact_id] = true;
    try {
      const result = await api.cheer(friend.contact_id, "focus");
      friend.you_cheered_today = true;
      haptic("success");
      showToast(
        result.xp === "awarded" ? "Поддержка отправлена: +10 XP к ветке «Связи»" : result.xp === "daily_cap" ? "Поддержка отправлена. Дневной лимит XP уже набран" : "Поддержка отправлена",
      );
    } catch (error) {
      if (error.code === "already_cheered_today") friend.you_cheered_today = true; // somebody (another device) did it first: just show the state
      fail(error, load);
    } finally {
      sending[friend.contact_id] = false;
    }
  }

  const initial = (name) => (name ?? "?").trim().slice(0, 1).toUpperCase();
</script>

<div class="screen">
  <h1 class="screen-title">Таверна</h1>

  {#if !friends}
    <Spinner />
  {:else if friends.length === 0}
    <Empty
      emoji="🍻"
      title="В таверне пока пусто"
      text="Здесь появляются друзья из вашего списка связей. Пригласите человека на вкладке «Ещё» → «Связи», и он окажется за соседним столом."
    />
  {:else}
    <ul class="space-y-3">
      {#each friends as friend (friend.contact_id)}
        {@const mood = friend.mood ? MOOD_LABELS[friend.mood] : null}
        <li class="rounded-card bg-surface p-4">
          <div class="flex items-center gap-3">
            <div class="flex h-12 w-12 shrink-0 items-center justify-center overflow-hidden rounded-full bg-bg text-xl font-semibold text-hint">
              {#if avatars[friend.contact_id]}<img src={avatars[friend.contact_id]} alt="" class="h-full w-full object-cover" />{:else}{initial(friend.name)}{/if}
            </div>
            <div class="min-w-0 flex-1">
              <div class="truncate text-base font-semibold">{friend.name}</div>
              <div class="flex flex-wrap items-center gap-x-2 text-xs text-hint">
                {#if friend.level !== null}<span>ур. {friend.level} · {friend.total_xp} XP</span>{/if}
                {#if mood}<span class="rounded-full bg-bg px-2 py-0.5 text-ink">{mood.icon} {mood.label}</span>{/if}
                {#if friend.open_for_intros}<span class="text-link">открыт к знакомствам</span>{/if}
              </div>
            </div>
          </div>

          {#if friend.interests?.length}
            <div class="mt-3 flex flex-wrap gap-1.5">
              {#each friend.interests as interest, i (i)}
                <span class="rounded-full bg-bg px-2.5 py-1 text-xs">{interest}</span>
              {/each}
            </div>
          {/if}

          {#if friend.cheered_you_today}
            <p class="mt-3 text-sm text-focus">💛 Сегодня поддержал(а) вас</p>
          {/if}

          <button
            type="button"
            class="mt-3 w-full rounded-card px-4 py-3 text-sm font-semibold transition active:scale-[0.98] {friend.you_cheered_today
              ? 'bg-bg text-hint'
              : 'bg-accent text-accent-ink'} disabled:opacity-60"
            disabled={friend.you_cheered_today || sending[friend.contact_id]}
            onclick={() => cheer(friend)}
          >
            {friend.you_cheered_today ? "Сегодня уже поддержали ✓" : sending[friend.contact_id] ? "…" : "🎯 Поддержать фокус"}
          </button>
        </li>
      {/each}
    </ul>
    <p class="mt-4 px-1 text-xs text-hint">Один раз в день на человека. Каждая поддержка даёт +10 XP в ветку «Связи» вам обоим.</p>
  {/if}
</div>
