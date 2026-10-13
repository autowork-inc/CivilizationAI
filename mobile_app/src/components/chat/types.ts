export type ChatRole = 'user' | 'assistant';

export type ChatMessage = {
  /** React key. Local messages: the client_message_id (user) / `a:<client_message_id>` (assistant); from history: the server id. */
  id: string;
  role: ChatRole;
  text: string;
  /** Cards the assistant attached to this message (SSE `card` events). Not rendered yet. */
  cards?: Record<string, unknown>[];
};
