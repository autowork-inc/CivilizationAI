import { ApiError } from "./api.js";
import { showToast } from "./toast.svelte.js";
import { haptic } from "./tg.js";

// Global state shared by the screens: the profile time zone (events are shown on that wall clock) and "cannot authenticate".
export const app = $state({ timezone: "", authError: null });

/**
 * One place for what a failed call means to the user.
 *   401/403  -> the whole app shows the explanation (open from Telegram / finish the interview);
 *   409      -> somebody (the bot, another device) changed the data first: tell, then `refresh()` shows the fresh state;
 *   others   -> the server's own (Russian) message.
 */
export function fail(error, refresh) {
  if (!(error instanceof ApiError)) {
    console.error(error);
    showToast("Что-то пошло не так. Попробуйте ещё раз.", "error");
    return;
  }
  if (error.isAuth) {
    app.authError = { code: error.code, message: error.message };
    return;
  }
  haptic("error");
  showToast(error.message, "error");
  if (error.isConflict) refresh?.();
}
