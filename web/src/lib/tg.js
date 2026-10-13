// Thin wrapper over window.Telegram.WebApp. Outside Telegram (a plain browser) everything degrades to no-ops and the API
// answers 401, which the app shows as "open me from Telegram".

export const tg = typeof window !== "undefined" ? (window.Telegram?.WebApp ?? null) : null;

export function initTelegram() {
  if (!tg) return;
  tg.ready();
  tg.expand();
  try {
    tg.disableVerticalSwipes?.(); // a swipe-down inside a scrolling list must not close the app
  } catch {
    /* older clients */
  }
  tg.setHeaderColor?.("secondary_bg_color");
  tg.setBackgroundColor?.("secondary_bg_color");
}

/** The signed launch data: the ONLY proof of identity the backend accepts. */
export const initData = () => tg?.initData ?? "";

export const deviceTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

export function haptic(kind = "success") {
  try {
    if (kind === "select") tg?.HapticFeedback?.selectionChanged?.();
    else tg?.HapticFeedback?.notificationOccurred?.(kind);
  } catch {
    /* cosmetic */
  }
}

/** Open a t.me link inside Telegram (a share link then shows the "send to" picker); a plain browser gets a new tab. */
export function openTelegramLink(url) {
  if (tg?.openTelegramLink) tg.openTelegramLink(url);
  else window.open(url, "_blank", "noopener");
}

/** Copy text to the clipboard. Resolves true/false; the old `execCommand` path covers WebViews without the async API. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    /* no async clipboard here (or no permission): fall back */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch {
    return false;
  }
}

export function confirmNative(message) {
  // Telegram's own dialog when available, window.confirm otherwise
  return new Promise((resolve) => {
    if (tg?.showConfirm) tg.showConfirm(message, (ok) => resolve(!!ok));
    else resolve(window.confirm(message));
  });
}
