// Typed view of the events POST /chat/stream pushes. Pure TypeScript (no React Native imports): testable in Node.

/** `error` is always the last event of a stream. */
export type ChatStreamEvent =
  | { type: 'token'; text: string }
  | { type: 'card'; card: Record<string, unknown> }
  | { type: 'done'; userMessageId: string; replyMessageId: string; userText?: string }
  | { type: 'error'; message: string; code: string };

function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

export const GENERIC_REPLY_ERROR = 'Не удалось получить ответ. Попробуйте ещё раз.';

// The chat service writes its error messages in English; the person gets Russian text chosen by the stable error `code`.
const ERROR_TEXT: Record<string, string> = {
  reply_failed: GENERIC_REPLY_ERROR,
  empty_message: 'Сообщение пустое.',
  one_media: 'Отправьте либо голосовое, либо файл, но не оба сразу.',
  unknown_persona: 'Такой темы нет.',
  persona_unavailable: 'В этой теме писать нельзя.',
  onboarding_required: 'Сначала завершите знакомство с ассистентом.',
  unknown_user: 'Сессия истекла. Войдите заново.',
  voice_too_long: 'Голосовое слишком длинное.',
  voice_unclear: 'Не удалось разобрать речь. Попробуйте ещё раз.',
};

/** Text for the person: a known code -> its Russian text; otherwise the server's message if it is already Russian; otherwise a generic one. */
export function chatErrorText(code: string | undefined, serverMessage: string | undefined): string {
  if (code && ERROR_TEXT[code]) return ERROR_TEXT[code];
  if (serverMessage && /[А-Яа-яЁё]/.test(serverMessage)) return serverMessage;
  return GENERIC_REPLY_ERROR;
}

/** Turns one raw SSE event into a typed one; events of an unknown kind or with a broken payload are skipped (null). */
export function toChatEvent(event: string, data: string): ChatStreamEvent | null {
  let payload: Record<string, unknown>;
  try {
    const parsed: unknown = JSON.parse(data);
    if (typeof parsed !== 'object' || parsed === null) return null;
    payload = parsed as Record<string, unknown>;
  } catch {
    console.warn(`chat stream: unreadable "${event}" payload ignored`);
    return null;
  }
  switch (event) {
    case 'token': {
      const text = asString(payload.text);
      return text ? { type: 'token', text } : null;
    }
    case 'card':
      return typeof payload.card === 'object' && payload.card !== null ? { type: 'card', card: payload.card as Record<string, unknown> } : null;
    case 'done':
      return {
        type: 'done',
        userMessageId: asString(payload.user_message_id) ?? '',
        replyMessageId: asString(payload.reply_message_id) ?? '',
        userText: asString(payload.user_text),
      };
    case 'error':
      return { type: 'error', message: chatErrorText(asString(payload.code), asString(payload.error)), code: asString(payload.code) ?? 'unknown' };
    default:
      return null;
  }
}
