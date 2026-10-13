<script>
  import { api } from "../lib/api.js";
  import { app, fail } from "../lib/app.svelte.js";
  import { formatDay } from "../lib/time.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { confirmNative, haptic } from "../lib/tg.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Section from "../lib/ui/Section.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";
  import Switch from "../lib/ui/Switch.svelte";

  // `contact` comes from the screen's snapshot (so it is always the server's last word); every action answers with a fresh
  // snapshot, handed back through `onupdate`. `onreload` re-reads the server after a refusal.
  let { contact, scopes, onclose, onupdate, onreload } = $props();

  // svelte-ignore state_referenced_locally
  let alias = $state(contact.alias ?? "");
  let busy = $state(false);
  let version = $state(0); // bumped after every action: the switches are rebuilt from the server's state, so a refused one flips back

  const accepted = $derived(contact.status === "accepted");
  const blocked = $derived(contact.status === "blocked");
  const name = $derived(contact.alias ?? "Без имени");
  const aliasChanged = $derived(alias.trim() !== (contact.alias ?? ""));
  const aliasValid = $derived(alias.trim().length > 0);

  async function run(call, { message, done } = {}) {
    busy = true;
    try {
      onupdate(await call());
      haptic("success");
      if (message) showToast(message);
      done?.();
    } catch (error) {
      fail(error);
      onreload(); // a refused or stale action must not stay on screen as if it had happened
    } finally {
      version += 1;
      busy = false;
    }
  }

  const saveAlias = () =>
    run(() => api.renameContact(contact.id, alias), {
      message: "Имя сохранено",
      done: () => (alias = alias.trim().split(/\s+/).join(" ")),
    });

  const toggle = (scope, enabled) =>
    run(() => api.setGrant(contact.id, scope, enabled), { message: enabled ? "Разрешил" : "Запретил" });

  async function block() {
    const ok = await confirmNative(
      "Заблокировать? Человек не узнает об этом. Все ваши разрешения для него будут сняты, и новых он не получит.",
    );
    if (ok) await run(() => api.blockContact(contact.id), { message: "Заблокировал" });
  }

  const unblock = () =>
    run(() => api.unblockContact(contact.id), { message: "Разблокировал. Разрешения придётся включить заново" });

  async function remove() {
    const ok = await confirmNative(
      "Удалить контакт? Связь исчезнет у вас обоих, разрешения в обе стороны будут сняты. Чтобы связаться снова, понадобится новая ссылка.",
    );
    if (ok) await run(() => api.removeContact(contact.id), { message: "Контакт удалён", done: onclose });
  }
</script>

<Sheet open title={name} {onclose}>
  <div class="stack">
    {#if blocked}
      <Banner
        tone="warning"
        title="Контакт заблокирован"
        text="Он не знает об этом и ничего не получает. Все разрешения сняты."
      />
    {:else if !accepted}
      <Banner tone="info" title="Ожидает подтверждения" text="Пока связь не подтверждена, разрешения выдать нельзя." />
    {/if}

    <Section header="Как вы его называете" footer="Имя видите только вы. Для него в контактах у вас ничего не меняется.">
      <Field label="Имя" bind:value={alias} maxlength={40} placeholder="Например: Саша из зала" />
    </Section>
    {#if aliasChanged}
      <Button onclick={saveAlias} disabled={!aliasValid} loading={busy}>Сохранить имя</Button>
    {/if}

    {#if accepted}
      <Section
        header="Что ему можно узнавать обо мне"
        footer="По умолчанию закрыто всё. Включённое действует, пока вы не выключите. Каждый запрос контакта попадает в журнал ниже."
      >
        {#key version}
          {#each scopes as scope (scope.key)}
            <div class="grant">
              <Switch
                label={scope.title}
                checked={contact.grants[scope.key]}
                disabled={busy}
                onchange={(enabled) => toggle(scope.key, enabled)}
              />
              <p class="desc">{scope.description}</p>
            </div>
          {/each}
        {/key}
      </Section>
    {/if}

    <p class="since">В контактах с {formatDay(contact.connected_since, app.timezone)}</p>

    {#if blocked}
      <Button onclick={unblock} loading={busy}>Разблокировать</Button>
      <p class="note">
        Заблокированный контакт остаётся в списке, чтобы блокировка не пропала. Чтобы удалить его совсем, сначала разблокируйте.
      </p>
    {:else}
      {#if accepted}
        <Button variant="secondary" onclick={block} disabled={busy}>Заблокировать</Button>
      {/if}
      <Button variant="danger" onclick={remove} disabled={busy}>Удалить контакт</Button>
    {/if}
  </div>
</Sheet>

<style>
  .grant + .grant {
    border-top: 0.5px solid var(--separator);
  }
  .desc {
    font-size: 13px;
    color: var(--hint);
    padding: 0 16px 10px;
    background: var(--surface);
  }
  .since,
  .note {
    font-size: 13px;
    color: var(--hint);
    text-align: center;
  }
</style>
