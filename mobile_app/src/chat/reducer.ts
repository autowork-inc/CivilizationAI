// State machine of one chat. Pure (no React, no network): every transition is unit-testable.
import type { ChatMessage } from '../components/chat/types';

export type ChatError = {
  message: string;
  /** The local message that failed: "Повторить" resends exactly this one (same client_message_id). */
  clientId: string;
};

export type ChatState = {
  messages: ChatMessage[];
  /** "ИИ печатает…": a request is in flight and no token has arrived yet. */
  typing: boolean;
  /** A request is in flight (typing + streaming). The input stays locked until `done` / `error`. */
  sending: boolean;
  error: ChatError | null;
};

export type ChatAction =
  | { type: 'reset'; messages: ChatMessage[] }
  | { type: 'send'; clientId: string; text: string; retry: boolean }
  | { type: 'token'; clientId: string; text: string }
  | { type: 'card'; clientId: string; card: Record<string, unknown> }
  | { type: 'done'; clientId: string }
  | { type: 'fail'; clientId: string; message: string }
  | { type: 'dismiss_error' };

export const initialChatState: ChatState = { messages: [], typing: false, sending: false, error: null };

export const assistantIdFor = (clientId: string) => `a:${clientId}`;

export function chatReducer(state: ChatState, action: ChatAction): ChatState {
  switch (action.type) {
    case 'reset':
      return { ...initialChatState, messages: action.messages };

    case 'send': {
      const assistantId = assistantIdFor(action.clientId);
      // a retry reuses the user's bubble and drops whatever half-reply was left of the failed attempt
      const kept = state.messages.filter((m) => m.id !== assistantId);
      const hasUser = kept.some((m) => m.id === action.clientId);
      const messages = action.retry && hasUser ? kept : [...kept, { id: action.clientId, role: 'user' as const, text: action.text }];
      return { messages, typing: true, sending: true, error: null };
    }

    case 'token': {
      const id = assistantIdFor(action.clientId);
      const exists = state.messages.some((m) => m.id === id);
      const messages = exists
        ? state.messages.map((m) => (m.id === id ? { ...m, text: m.text + action.text } : m))
        : [...state.messages, { id, role: 'assistant' as const, text: action.text }];
      return { ...state, messages, typing: false };
    }

    case 'card': {
      const id = assistantIdFor(action.clientId);
      const exists = state.messages.some((m) => m.id === id);
      const messages = exists
        ? state.messages.map((m) => (m.id === id ? { ...m, cards: [...(m.cards ?? []), action.card] } : m))
        : [...state.messages, { id, role: 'assistant' as const, text: '', cards: [action.card] }];
      return { ...state, messages };
    }

    case 'done':
      return { ...state, typing: false, sending: false };

    case 'fail':
      return {
        // the server contract: throw away what was received, the retry will bring the whole reply
        messages: state.messages.filter((m) => m.id !== assistantIdFor(action.clientId)),
        typing: false,
        sending: false,
        error: { message: action.message, clientId: action.clientId },
      };

    case 'dismiss_error':
      return { ...state, error: null };
  }
}
