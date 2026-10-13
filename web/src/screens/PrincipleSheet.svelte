<script>
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { confirmNative, haptic } from "../lib/tg.js";
  import Banner from "../lib/ui/Banner.svelte";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";

  let { principle, onclose, onsaved } = $props();

  // svelte-ignore state_referenced_locally
  const isNew = principle === null;
  // svelte-ignore state_referenced_locally
  let text = $state(principle?.text ?? "");
  // svelte-ignore state_referenced_locally
  let priority = $state(principle?.priority ?? 70);
  let busy = $state(false);

  const changed = $derived(isNew || text.trim() !== principle.text || Number(priority) !== principle.priority);
  const valid = $derived(text.trim().length > 0);

  async function save() {
    busy = true;
    try {
      if (isNew) {
        await api.createPrinciple({ text: text.trim(), priority: Number(priority) });
        showToast("Принцип добавлен");
      } else {
        const body = { expected_fact_id: principle.fact_id };
        if (text.trim() !== principle.text) body.text = text.trim();
        if (Number(priority) !== principle.priority) body.priority = Number(priority);
        const result = await api.editPrinciple(principle.key, body);
        showToast(result.overrides_cleared ? "Сохранил. Правило снова в полной силе" : "Сохранил");
      }
      haptic("success");
      onsaved();
    } catch (error) {
      fail(error, onsaved);
    } finally {
      busy = false;
    }
  }

  async function restore() {
    busy = true;
    try {
      await api.restorePrinciple(principle.key, principle.fact_id);
      haptic("success");
      showToast("Правило снова в силе");
      onsaved();
    } catch (error) {
      fail(error, onsaved);
    } finally {
      busy = false;
    }
  }

  async function remove() {
    if (!(await confirmNative("Удалить принцип? Я перестану его учитывать."))) return;
    busy = true;
    try {
      await api.deletePrinciple(principle.key, principle.fact_id);
      showToast("Удалил");
      onsaved();
    } catch (error) {
      fail(error, onsaved);
    } finally {
      busy = false;
    }
  }
</script>

<Sheet open title={isNew ? "Новый принцип" : "Принцип"} {onclose}>
  <div class="stack">
    {#if !isNew && principle.overridden}
      <Banner
        tone="warning"
        title="Перекрыто состоянием «{principle.overridden_by}»"
        text="Пока оно длится, я не настаиваю на этом правиле."
        action="Вернуть в силу"
        onaction={restore}
      />
    {/if}

    <div class="card">
      <Field label="Правило" multiline bind:value={text} maxlength={300} placeholder="Например: не есть сладкое после шести" />
      <label class="slider">
        <span class="row">
          <span class="label">Важность</span>
          <span class="spacer"></span>
          <strong>{priority}</strong>
        </span>
        <input type="range" min="0" max="100" step="5" bind:value={priority} />
        <span class="hint">Состояние с большей важностью может временно перекрыть это правило.</span>
      </label>
    </div>

    <Button onclick={save} disabled={!changed || !valid} loading={busy}>{isNew ? "Добавить" : "Сохранить"}</Button>
    {#if !isNew}
      <Button variant="danger" onclick={remove} disabled={busy}>Удалить принцип</Button>
    {/if}
  </div>
</Sheet>

<style>
  .card {
    border-radius: var(--radius);
    overflow: hidden;
  }
  .slider {
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 12px 16px;
    background: var(--surface);
    border-top: 0.5px solid var(--separator);
  }
  .label {
    font-size: 13px;
    color: var(--hint);
  }
  .hint {
    font-size: 12px;
    color: var(--hint);
  }
  input[type="range"] {
    width: 100%;
    accent-color: var(--accent);
  }
</style>
