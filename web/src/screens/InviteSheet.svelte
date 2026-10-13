<script>
  import { app } from "../lib/app.svelte.js";
  import { formatMoment } from "../lib/time.js";
  import { showToast } from "../lib/toast.svelte.js";
  import { copyText, haptic, openTelegramLink } from "../lib/tg.js";
  import Button from "../lib/ui/Button.svelte";
  import Sheet from "../lib/ui/Sheet.svelte";

  // `invite` is the answer of POST /invites: the link exists ONLY here (the server keeps just its hash).
  let { invite, onclose } = $props();

  let copied = $state(false);

  function share() {
    haptic("select");
    openTelegramLink(invite.share_url);
  }

  async function copy() {
    if (await copyText(invite.link)) {
      copied = true;
      haptic("success");
      showToast("Ссылка скопирована");
    } else {
      showToast("Не удалось скопировать. Нажмите на ссылку и скопируйте вручную.", "error");
    }
  }
</script>

<Sheet open title="Ссылка для связи" {onclose}>
  <div class="stack">
    <p class="lead">
      Отправьте её человеку, с которым хотите связаться. Он откроет ссылку в Telegram и сам решит, принимать ли.
    </p>

    <input
      class="link"
      readonly
      value={invite.link}
      aria-label="Ссылка-приглашение"
      onfocus={(event) => event.currentTarget.select()}
    />

    <Button onclick={share}>Отправить в Telegram</Button>
    <Button variant="secondary" onclick={copy}>{copied ? "Скопировано ✓" : "Скопировать ссылку"}</Button>

    <p class="hint">
      Ссылка одноразовая и действует до {formatMoment(invite.expires_at, app.timezone)}. Когда человек её примет, вы увидите
      его в контактах, но доступа к вашим данным у него не появится: разрешения выдаёте вы сами.
    </p>
    <p class="hint">
      Показать ссылку второй раз нельзя. Если потеряли — отзовите её в списке открытых ссылок и создайте новую.
    </p>
  </div>
</Sheet>

<style>
  .lead {
    font-size: 15px;
  }
  .link {
    width: 100%;
    padding: 12px 14px;
    border: 0;
    border-radius: var(--radius);
    background: var(--surface);
    font-size: 14px;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    text-overflow: ellipsis;
  }
  .hint {
    font-size: 13px;
    color: var(--hint);
  }
</style>
