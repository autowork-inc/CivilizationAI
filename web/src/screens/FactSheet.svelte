<script>
  import { api } from "../lib/api.js";
  import { fail } from "../lib/app.svelte.js";
  import { factText } from "../lib/labels.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { confirmNative, haptic } from "../lib/tg.js";
  import Button from "../lib/ui/Button.svelte";
  import Field from "../lib/ui/Field.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";

  let { fact, onclose, onsaved } = $props();

  // The parent re-creates this sheet per fact version ({#key}), so reading the props once is intended.
  // svelte-ignore state_referenced_locally
  let text = $state(fact.value_text ?? "");
  // svelte-ignore state_referenced_locally
  let num = $state(fact.value_num ?? "");
  // svelte-ignore state_referenced_locally
  let unit = $state(fact.value_unit ?? "");
  let busy = $state(false);
  // svelte-ignore state_referenced_locally
  const hasNum = fact.value_num !== null && fact.value_num !== undefined;

  const changed = $derived(
    text.trim() !== (fact.value_text ?? "") ||
      (hasNum && (Number(num) !== fact.value_num || unit.trim() !== (fact.value_unit ?? ""))),
  );
  const valid = $derived((hasNum ? num !== "" && Number.isFinite(Number(num)) : true) && (hasNum || text.trim() !== ""));

  async function save() {
    busy = true;
    try {
      const body = { expected_fact_id: fact.id };
      if (text.trim()) body.value_text = text.trim();
      if (hasNum) {
        body.value_num = Number(num);
        if (unit.trim()) body.value_unit = unit.trim();
      }
      await api.editFact(fact.key, body);
      haptic("success");
      showToast("Сохранил");
      onsaved();
    } catch (error) {
      fail(error, onsaved);
    } finally {
      busy = false;
    }
  }

  async function forget() {
    if (!(await confirmNative(`Забыть «${fact.title ?? factText(fact)}»? Я перестану это учитывать.`))) return;
    busy = true;
    try {
      await api.deleteFact(fact.key, fact.id);
      showToast("Забыл");
      onsaved();
    } catch (error) {
      fail(error, onsaved);
    } finally {
      busy = false;
    }
  }
</script>

<Sheet open title={fact.title ?? "Запись"} {onclose}>
  <div class="stack">
    <div class="card">
      {#if hasNum}
        <Field label="Значение" type="number" inputmode="decimal" bind:value={num} />
        <Field label="Единица" bind:value={unit} placeholder="например, кг" maxlength={20} />
      {/if}
      <Field
        label={hasNum ? "Примечание" : "Значение"}
        multiline
        bind:value={text}
        maxlength={1000}
        hint="Именно это значение я подставляю в ответы."
      />
    </div>
    <Button onclick={save} disabled={!changed || !valid} loading={busy}>Сохранить</Button>
    <Button variant="danger" onclick={forget} disabled={busy}>Забыть это</Button>
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
</style>
