<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import { app, fail } from "../lib/app.svelte.js";
  import { formatMoment } from "../lib/time.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { confirmNative, haptic } from "../lib/tg.js";
  import Button from "../lib/ui/Button.svelte";
  import Cell from "../lib/ui/Cell.svelte";
  import Empty from "../lib/ui/Empty.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Spinner from "../lib/ui/Spinner.svelte";
  import ContactSheet from "./ContactSheet.svelte";
  import InviteSheet from "./InviteSheet.svelte";

  let data = $state(null); // the server's last word: contacts, open invites, limits, offered scopes
  let audit = $state(null); // { entries, next_before }
  let contactId = $state(null); // which contact's sheet is open
  let invite = $state(null); // the link just created (it is never shown again)
  let busy = $state(false);
  let moreBusy = $state(false);

  async function load() {
    try {
      [data, audit] = await Promise.all([api.connections(), api.audit()]);
    } catch (error) {
      fail(error);
    }
  }

  onMount(load);

  /** Names in the journal are the user's OWN aliases, so a rename or a removal changes it: re-read it (best effort). */
  async function refreshAudit() {
    try {
      audit = await api.audit();
    } catch {
      /* stale names are harmless; a real problem (session) shows on the next action */
    }
  }

  const contact = $derived(contactId && data ? (data.contacts.find((c) => c.id === contactId) ?? null) : null);

  function summary(c) {
    if (c.status === "blocked") return "Заблокирован";
    if (c.status !== "accepted") return "Ожидает подтверждения";
    const on = data.scopes.filter((s) => c.grants[s.key]).map((s) => s.title);
    return on.length ? `Можно узнать: ${on.join(", ").toLowerCase()}` : "Ничего не разрешено";
  }

  async function createInvite() {
    busy = true;
    try {
      const created = await api.createInvite();
      data = created.connections;
      invite = created;
      haptic("success");
    } catch (error) {
      fail(error, load);
    } finally {
      busy = false;
    }
  }

  async function revoke(item) {
    if (!(await confirmNative("Отозвать ссылку? Человек, у которого она есть, больше не сможет по ней связаться."))) return;
    try {
      data = await api.revokeInvite(item.id);
      showToast("Ссылка отозвана");
    } catch (error) {
      fail(error);
      load(); // most likely it was used or expired meanwhile: show what is really there
    }
  }

  async function revokeAll() {
    if (!(await confirmNative("Отозвать все открытые ссылки?"))) return;
    try {
      data = await api.revokeAllInvites();
      showToast("Все ссылки отозваны");
    } catch (error) {
      fail(error, load);
    }
  }

  async function more() {
    moreBusy = true;
    try {
      const older = await api.audit(audit.next_before);
      audit = { entries: [...audit.entries, ...older.entries], next_before: older.next_before };
    } catch (error) {
      fail(error);
    } finally {
      moreBusy = false;
    }
  }
</script>

<div class="screen">
  <h1 class="screen-title">Связи</h1>

  {#if !data}
    <Spinner />
  {:else}
    {#if data.contacts.length}
      <Section
        header="Контакты"
        footer="Связь сама по себе ничего не открывает: что именно человек может узнать обо мне, решаете вы — откройте контакт."
      >
        {#each data.contacts as c (c.id)}
          <Cell title={c.alias ?? "Без имени"} subtitle={summary(c)} chevron onclick={() => (contactId = c.id)} />
        {/each}
      </Section>
    {:else}
      <Empty
        emoji="👥"
        title="Пока ни с кем не связаны"
        text="Пригласите близкого человека: он откроет вашу ссылку в Telegram, и вы окажетесь в контактах друг у друга. Доступа к вашим данным при этом у него не появится."
      />
    {/if}

    <Button onclick={createInvite} loading={busy}>＋ Пригласить человека</Button>

    {#if data.invites.length}
      <div class="gap"></div>
      <Section
        header="Открытые ссылки"
        footer="Открыто {data.invites.length} из {data.limits.max_open_invites}. Саму ссылку после создания показать нельзя: если потеряли — отзовите её и создайте новую."
      >
        {#each data.invites as item (item.id)}
          <Cell
            title="Ссылка от {formatMoment(item.created_at, app.timezone)}"
            subtitle="Действует до {formatMoment(item.expires_at, app.timezone)}"
            after="Отозвать"
            onclick={() => revoke(item)}
          />
        {/each}
      </Section>
      {#if data.invites.length > 1}
        <Button variant="plain" onclick={revokeAll}>Отозвать все ссылки</Button>
      {/if}
    {/if}

    <div class="gap"></div>
    {#if audit?.entries.length}
      <Section
        header="Журнал запросов"
        footer="Кто и когда запрашивал ваши данные и что ему ответили. Записи нельзя изменить или удалить. Имена — те, какими вы назвали контакты."
      >
        {#each audit.entries as entry (entry.id)}
          <Cell
            title={entry.requester ?? "Бывший контакт"}
            subtitle="{entry.scope_title} · {formatMoment(entry.created_at, app.timezone)}"
            after={entry.decision === "allowed" ? "✓ Разрешено" : "✕ Отказано"}
          />
        {/each}
      </Section>
      {#if audit.next_before}
        <Button variant="plain" onclick={more} loading={moreBusy}>Показать ещё</Button>
      {/if}
    {:else}
      <p class="audit-title">Журнал запросов</p>
      <Empty emoji="🗒" title="Пока никто ничего не запрашивал">
        <p>Когда контакт запросит ваши данные, здесь появится запись: кто, что и получил ли ответ — разрешено или отказано.</p>
      </Empty>
    {/if}
  {/if}
</div>

{#if contact}
  {#key contact.id}
    <ContactSheet
      {contact}
      scopes={data.scopes}
      onclose={() => (contactId = null)}
      onupdate={(fresh) => {
        data = fresh;
        refreshAudit();
      }}
      onreload={load}
    />
  {/key}
{/if}

{#if invite}
  <InviteSheet {invite} onclose={() => (invite = null)} />
{/if}

<style>
  .gap {
    height: 22px;
  }
  .audit-title {
    font-size: 13px;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.2px;
    color: var(--section-header);
    margin: 0 16px 7px;
  }
</style>
