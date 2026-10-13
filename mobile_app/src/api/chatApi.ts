import axios from 'axios';

import { NETWORK_ERROR_TEXT, api, authorizedFetch } from './apiClient';
import { GENERIC_REPLY_ERROR, chatErrorText, toChatEvent, type ChatStreamEvent } from './chatEvents';
import { readSse } from './sse';
import type { ApiErrorBody } from './types';

const PREFIX = '/api/v1/mobile';

/** If the server sends nothing (not even its keep-alive pings) for this long, the stream is considered dead. */
const STREAM_IDLE_MS = 60_000;

// ------------------------------------------------------------------ personas & history

export type Persona = { id: string; code: string; title: string; mode: string };

export type HistoryMessage = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  created_at: string;
  persona_id: string | null;
  client_message_id: string | null;
  reply_to_id: string | null;
  cards: Record<string, unknown>[];
  attachment: { kind: string; content_kind: string; mime_type: string; file_name: string | null; size_bytes: number } | null;
};

export async function fetchPersonas(): Promise<Persona[]> {
  const { data } = await api.get<Persona[]>(`${PREFIX}/personas`);
  return data;
}

/** The newest `limit` messages of one chat (`personaId` null = the main chat), oldest first. */
export async function fetchHistory(personaId: string | null, limit = 30): Promise<{ messages: HistoryMessage[]; has_more: boolean }> {
  const { data } = await api.get<{ messages: HistoryMessage[]; has_more: boolean }>(`${PREFIX}/chat/history`, {
    params: { limit, ...(personaId ? { persona_id: personaId } : {}) },
  });
  return data;
}

// ------------------------------------------------------------------ streaming

export type { ChatStreamEvent };

/** A failure that happened BEFORE the stream started or while reading it (not an `error` event of the server). */
export class ChatRequestError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = 'ChatRequestError';
  }
}

export function isAbort(error: unknown): boolean {
  return error instanceof Error && error.name === 'AbortError';
}

async function httpFailure(res: { status: number; json(): Promise<unknown> }): Promise<ChatRequestError> {
  let message: string | undefined;
  let code: string | undefined;
  try {
    const body = (await res.json()) as ApiErrorBody;
    message = body.error?.message;
    code = body.error?.code;
  } catch {
    // not JSON: fall through to the generic text
  }
  const byStatus =
    res.status === 401
      ? 'Сессия истекла. Войдите заново.'
      : res.status === 413
        ? 'Файл слишком большой.'
        : res.status === 429
          ? 'Слишком много сообщений. Подождите немного.'
          : res.status === 503
            ? 'Чат сейчас недоступен. Попробуйте позже.'
            : res.status >= 500
              ? 'Ошибка на сервере. Попробуйте позже.'
              : 'Не удалось отправить сообщение.';
  // a known code wins, then a server message that is already Russian, then the text for the HTTP status
  const text = code && chatErrorText(code, undefined) !== GENERIC_REPLY_ERROR ? chatErrorText(code, undefined) : message && /[А-Яа-яЁё]/.test(message) ? message : byStatus;
  return new ChatRequestError(text, res.status, code);
}

export type StreamChatArgs = {
  /** null = the main chat. */
  personaId: string | null;
  text: string;
  /** The idempotency key: resending the SAME id after a failure returns the stored reply instead of asking the model again. */
  clientMessageId: string;
  signal?: AbortSignal;
};

/**
 * POST /chat/stream and yield the typed events as they arrive.
 * Throws ChatRequestError (HTTP failure, no network, silent stream); an AbortError when `signal` is aborted.
 * Errors the SERVER reports mid-stream come as an `error` event instead.
 */
export async function* streamChat({ personaId, text, clientMessageId, signal }: StreamChatArgs): AsyncGenerator<ChatStreamEvent> {
  let response;
  try {
    response = await authorizedFetch(`${PREFIX}/chat/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({ client_message_id: clientMessageId, persona_id: personaId, text }),
      signal,
    });
  } catch (error) {
    if (isAbort(error) || axios.isCancel(error)) throw error;
    throw new ChatRequestError(NETWORK_ERROR_TEXT);
  }
  if (!response.ok) throw await httpFailure(response);
  if (!response.body) throw new ChatRequestError('Не удалось открыть поток ответа.');

  try {
    for await (const raw of readSse(response.body, { idleMs: STREAM_IDLE_MS })) {
      const event = toChatEvent(raw.event, raw.data);
      if (event) yield event;
    }
  } catch (error) {
    if (isAbort(error)) throw error;
    if (error instanceof Error && error.name === 'SseIdleTimeout') {
      throw new ChatRequestError('Ответ слишком долго не приходит. Попробуйте ещё раз.');
    }
    throw new ChatRequestError('Соединение прервано. Попробуйте ещё раз.');
  }
}
