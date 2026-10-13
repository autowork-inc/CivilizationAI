import * as Crypto from 'expo-crypto';
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';

import { getErrorMessage } from '@/api/apiClient';
import { ChatRequestError, fetchHistory, fetchPersonas, isAbort, streamChat, type HistoryMessage } from '@/api/chatApi';
import { chatReducer, initialChatState } from '@/chat/reducer';
import { Drip } from '@/chat/drip';
import type { ChatMessage } from '@/components/chat/types';

type ChatTarget = { personaId: string | null; title: string };

/** The backend's "main chat" (persona_id = null): used while the personas are not known and when the person has none. */
const MAIN_CHAT: ChatTarget = { personaId: null, title: 'Основной чат' };

const CONNECTION_LOST = 'Соединение прервано. Попробуйте ещё раз.';

function fromHistory(m: HistoryMessage): ChatMessage {
  const attachment = m.attachment ? `📎 ${m.attachment.file_name ?? 'вложение'}` : '';
  return {
    id: m.id,
    role: m.role,
    text: m.text || attachment,
    cards: m.cards.length > 0 ? m.cards : undefined,
  };
}

/**
 * Everything the chat screen needs: which chat is open (the first active persona), its history,
 * and sending a message with a live SSE reply.
 */
export function useChat() {
  const [state, dispatch] = useReducer(chatReducer, initialChatState);
  const [target, setTarget] = useState<ChatTarget>(MAIN_CHAT);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const targetRef = useRef<ChatTarget>(MAIN_CHAT);
  const abortRef = useRef<AbortController | null>(null);
  const busyRef = useRef(false); // synchronous guard: state updates are one render late
  const aliveRef = useRef(true);
  const textsRef = useRef(new Map<string, string>()); // client_message_id -> text, for "Повторить"

  // ---------------------------------------------------------------- personas + history

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);

    let next = MAIN_CHAT;
    let failure: string | null = null;
    try {
      const personas = await fetchPersonas();
      if (personas[0]) next = { personaId: personas[0].id, title: personas[0].title };
    } catch (error) {
      failure = getErrorMessage(error); // fall back to the main chat; the banner offers a retry
    }

    let history: ChatMessage[] = [];
    try {
      history = (await fetchHistory(next.personaId)).messages.map(fromHistory);
    } catch (error) {
      failure ??= getErrorMessage(error);
    }

    if (!aliveRef.current) return;
    targetRef.current = next;
    setTarget(next);
    dispatch({ type: 'reset', messages: history });
    setLoadError(failure);
    setLoading(false);
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    load();
    return () => {
      aliveRef.current = false;
      abortRef.current?.abort(); // the server still finishes and stores the reply; the next history load shows it
    };
  }, [load]);

  // ---------------------------------------------------------------- sending

  const run = useCallback(async (clientId: string, text: string, retry: boolean) => {
    busyRef.current = true;
    const controller = new AbortController();
    abortRef.current = controller;
    dispatch({ type: 'send', clientId, text, retry });

    const drip = new Drip((piece) => dispatch({ type: 'token', clientId, text: piece }));
    let finished = false;
    try {
      for await (const event of streamChat({ personaId: targetRef.current.personaId, text, clientMessageId: clientId, signal: controller.signal })) {
        if (controller.signal.aborted) return;
        switch (event.type) {
          case 'token':
            drip.push(event.text);
            break;
          case 'card':
            // TODO(cards): render/store cards. For now they are only logged and kept on the message.
            console.log('chat card', JSON.stringify(event.card));
            dispatch({ type: 'card', clientId, card: event.card });
            break;
          case 'done':
            drip.flush();
            dispatch({ type: 'done', clientId });
            finished = true;
            break;
          case 'error':
            drip.discard();
            dispatch({ type: 'fail', clientId, message: event.message });
            finished = true;
            break;
        }
      }
      if (!finished) {
        drip.discard();
        dispatch({ type: 'fail', clientId, message: CONNECTION_LOST });
      }
    } catch (error) {
      drip.discard();
      if (controller.signal.aborted || isAbort(error)) return;
      dispatch({ type: 'fail', clientId, message: error instanceof ChatRequestError ? error.message : getErrorMessage(error) });
    } finally {
      drip.discard(); // no-op after a flush; stops the timer on every other exit
      if (abortRef.current === controller) abortRef.current = null;
      busyRef.current = false;
    }
  }, []);

  const send = useCallback(
    (raw: string) => {
      const text = raw.trim();
      if (!text || busyRef.current || !aliveRef.current) return;
      const clientId = Crypto.randomUUID(); // the idempotency key of this message
      textsRef.current.set(clientId, text);
      void run(clientId, text, false);
    },
    [run],
  );

  const retry = useCallback(() => {
    const failed = state.error;
    if (!failed || busyRef.current) return;
    const text = textsRef.current.get(failed.clientId);
    if (text) void run(failed.clientId, text, true); // the SAME client_message_id: the server will not answer twice
  }, [run, state.error]);

  const dismissError = useCallback(() => dispatch({ type: 'dismiss_error' }), []);

  return {
    title: loading ? '…' : target.title,
    messages: state.messages,
    typing: state.typing,
    sending: state.sending,
    /** Personas/history are being loaded: the input is locked. */
    loading,
    /** Personas or history could not be loaded (the chat falls back to what it has). */
    loadError,
    /** The last message failed (server `error` event, HTTP or network failure). */
    error: state.error,
    send,
    retry,
    dismissError,
    reload: load,
  };
}
