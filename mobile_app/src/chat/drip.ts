/**
 * "Typewriter" smoothing. The model's tokens arrive in uneven bursts (and a stored reply arrives as one big
 * piece); showing them as they come looks jerky. The drip releases the backlog a few characters per tick,
 * faster when the backlog is large, so the text always looks typed and never lags far behind the server.
 */

export const TICK_MS = 28;

/** How many characters to show on this tick for a backlog of `backlog` characters (always >= 1, never more than the backlog). */
export function dripStep(backlog: number): number {
  if (backlog <= 0) return 0;
  return Math.min(backlog, Math.max(1, Math.ceil(backlog / 8)));
}

export class Drip {
  private pending = '';
  private timer: ReturnType<typeof setInterval> | null = null;
  private readonly emit: (text: string) => void;

  constructor(emit: (text: string) => void) {
    this.emit = emit;
  }

  push(text: string): void {
    this.pending += text;
    if (!this.timer) this.timer = setInterval(() => this.tick(), TICK_MS);
  }

  /** Shows everything that is still waiting, right now (the stream is complete). */
  flush(): void {
    this.stop();
    if (this.pending) {
      const rest = this.pending;
      this.pending = '';
      this.emit(rest);
    }
  }

  /** Forgets the backlog without showing it (the attempt failed or was cancelled). */
  discard(): void {
    this.stop();
    this.pending = '';
  }

  private tick(): void {
    const n = dripStep(this.pending.length);
    if (n === 0) {
      this.stop();
      return;
    }
    // never cut a surrogate pair (emoji) in half
    let cut = n;
    const code = this.pending.charCodeAt(cut - 1);
    if (code >= 0xd800 && code <= 0xdbff && cut < this.pending.length) cut += 1;
    const piece = this.pending.slice(0, cut);
    this.pending = this.pending.slice(cut);
    this.emit(piece);
  }

  private stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }
}
