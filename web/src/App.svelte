<script>
  import { onMount } from "svelte";
  import { api } from "./lib/api.js";
  import { app, fail } from "./lib/app.svelte.js";
  import { initTelegram, tg } from "./lib/tg.js";
  import Spinner from "./lib/ui/Spinner.svelte";
  import TabBar from "./lib/ui/TabBar.svelte";
  import Toast from "./lib/ui/Toast.svelte";
  import Character from "./screens/Character.svelte";
  import Dashboard from "./screens/Dashboard.svelte";
  import More from "./screens/More.svelte";
  import Tavern from "./screens/Tavern.svelte";

  // The bottom bar belongs to the game screens; the older settings screens (Brain, Constitution, Schedule, Reminders, Connections) live under "Ещё".
  const TABS = [
    { id: "character", label: "Персонаж", icon: "🧙" },
    { id: "tavern", label: "Таверна", icon: "🍻" },
    { id: "dashboard", label: "Дашборд", icon: "📊" },
    { id: "more", label: "Ещё", icon: "⚙️" },
  ];

  let tab = $state("character");
  let ready = $state(false);

  onMount(async () => {
    initTelegram();
    try {
      const me = await api.me();
      app.timezone = me.timezone;
      ready = true;
    } catch (error) {
      fail(error);
    }
  });

  const outsideTelegram = $derived(!tg || !tg.initData);
</script>

{#if app.authError}
  <main class="gate">
    <div class="emoji" aria-hidden="true">{outsideTelegram ? "📱" : "🔒"}</div>
    <h1>{outsideTelegram ? "Откройте из Telegram" : "Не получилось войти"}</h1>
    <p class="muted">{app.authError.message}</p>
  </main>
{:else if !ready}
  <Spinner />
{:else}
  {#if tab === "character"}<Character />{/if}
  {#if tab === "tavern"}<Tavern />{/if}
  {#if tab === "dashboard"}<Dashboard />{/if}
  {#if tab === "more"}<More />{/if}
  <TabBar tabs={TABS} bind:active={tab} />
{/if}

<Toast />

<style>
  .gate {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    gap: 10px;
    padding: 96px 32px;
  }
  .emoji {
    font-size: 56px;
  }
  h1 {
    font-size: 22px;
  }
</style>
