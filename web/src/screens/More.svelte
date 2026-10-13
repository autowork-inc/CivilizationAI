<script>
  // "Ещё": the settings screens that used to be tabs of their own (the bottom bar now belongs to the game: Character, Tavern, Dashboard). A list of rows;
  // a tap opens the screen under a small "back" bar. The choice is remembered while the app is open.
  import Brain from "./Brain.svelte";
  import Connections from "./Connections.svelte";
  import Constitution from "./Constitution.svelte";
  import Reminders from "./Reminders.svelte";
  import Schedule from "./Schedule.svelte";
  import Cell from "../lib/ui/Cell.svelte";
  import Section from "../lib/ui/Section.svelte";

  const ITEMS = [
    { id: "brain", icon: "🧠", title: "Мозг", subtitle: "Что я о вас помню" },
    { id: "constitution", icon: "📜", title: "Конституция", subtitle: "Ваши принципы" },
    { id: "schedule", icon: "🗓", title: "Расписание", subtitle: "События и серии" },
    { id: "reminders", icon: "🔔", title: "Напоминания", subtitle: "Когда мне писать первым" },
    { id: "connections", icon: "🤝", title: "Связи", subtitle: "Друзья и доступы" },
  ];

  let open = $state(null);
</script>

{#if open === null}
  <div class="screen">
    <h1 class="screen-title">Ещё</h1>
    <Section header="Настройки и данные">
      {#each ITEMS as item (item.id)}
        <Cell title="{item.icon} {item.title}" subtitle={item.subtitle} chevron onclick={() => (open = item.id)} />
      {/each}
    </Section>
  </div>
{:else}
  <div class="sticky top-0 z-10 border-b border-line bg-surface/90 px-4 backdrop-blur" style="padding-top: calc(var(--safe-top) + 6px); padding-bottom: 6px">
    <button type="button" class="text-base text-link" onclick={() => (open = null)}>‹ Назад</button>
  </div>
  {#if open === "brain"}<Brain />{/if}
  {#if open === "constitution"}<Constitution />{/if}
  {#if open === "schedule"}<Schedule />{/if}
  {#if open === "reminders"}<Reminders />{/if}
  {#if open === "connections"}<Connections />{/if}
{/if}
