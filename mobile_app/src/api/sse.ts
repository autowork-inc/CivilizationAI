// Server-Sent Events over a plain ReadableStream (React Native has no EventSource).
// Pure TypeScript with no React Native imports, so it can be tested in Node.

export type SseEvent = { event: string; data: string };

/** No bytes (not even the server's keep-alive comments) arrived within the idle window. */
export class SseIdleTimeout extends Error {
  constructor() {
    super('SSE stream went silent');
    this.name = 'SseIdleTimeout';
  }
}

function concat(a: Uint8Array, b: Uint8Array): Uint8Array {
  if (a.length === 0) return b;
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

/**
 * Incremental parser of the text/event-stream format.
 *
 * Works on BYTES and decodes one complete line at a time: a line break (0x0A / 0x0D) never occurs inside a
 * multi-byte UTF-8 sequence, so Cyrillic text split across network chunks is never torn
 * (and nothing depends on TextDecoder's streaming mode, which React Native does not document).
 */
export class SseParser {
  private buffer: Uint8Array = new Uint8Array(0);
  private eventName = '';
  private dataLines: string[] = [];
  private readonly decoder = new TextDecoder('utf-8');

  /** Feeds a network chunk, returns the events it completed (possibly none, possibly several). */
  feed(chunk: Uint8Array): SseEvent[] {
    const bytes = concat(this.buffer, chunk);
    const out: SseEvent[] = [];
    let start = 0;
    let i = 0;
    while (i < bytes.length) {
      const c = bytes[i];
      if (c === 0x0a) {
        this.line(bytes.subarray(start, i), out);
        i += 1;
        start = i;
      } else if (c === 0x0d) {
        if (i + 1 >= bytes.length) break; // "\r" at the very end: a "\n" may follow in the next chunk
        this.line(bytes.subarray(start, i), out);
        i += bytes[i + 1] === 0x0a ? 2 : 1;
        start = i;
      } else {
        i += 1;
      }
    }
    this.buffer = bytes.slice(start);
    return out;
  }

  private line(raw: Uint8Array, out: SseEvent[]): void {
    const text = this.decoder.decode(raw);
    if (text === '') {
      // a blank line ends the event; an event without data lines is dropped (per the spec)
      if (this.dataLines.length > 0) out.push({ event: this.eventName || 'message', data: this.dataLines.join('\n') });
      this.eventName = '';
      this.dataLines = [];
      return;
    }
    if (text.startsWith(':')) return; // comment / keep-alive ping
    const colon = text.indexOf(':');
    const field = colon === -1 ? text : text.slice(0, colon);
    let value = colon === -1 ? '' : text.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') this.eventName = value;
    else if (field === 'data') this.dataLines.push(value);
    // `id` and `retry` are not used by this app
  }
}

export type ReadSseOptions = {
  /** Fail with SseIdleTimeout if no bytes arrive for this long. Omit = wait forever. */
  idleMs?: number;
};

async function readWithTimeout<T>(read: Promise<T>, idleMs: number | undefined): Promise<T> {
  if (!idleMs) return read;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      read,
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new SseIdleTimeout()), idleMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Yields the events of an SSE response body until the server closes the stream. */
export async function* readSse(body: ReadableStream<Uint8Array>, options: ReadSseOptions = {}): AsyncGenerator<SseEvent> {
  const reader = body.getReader();
  const parser = new SseParser();
  try {
    for (;;) {
      const { done, value } = await readWithTimeout(reader.read(), options.idleMs);
      if (done) return;
      if (value) yield* parser.feed(value);
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // the stream is already closed or errored: nothing left to release
    }
  }
}
