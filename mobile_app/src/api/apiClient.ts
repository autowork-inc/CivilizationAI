import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios';

import { API_URL, API_URL_CONFIGURED, AUTH_PREFIX } from './config';
import { tokenStorage } from './tokenStorage';
import type { ApiErrorBody, TokenPair } from './types';

const TIMEOUT_MS = 15_000;

/**
 * Bare client for /auth/* (no interceptors): login/register/refresh/logout must never
 * trigger the refresh machinery themselves, otherwise a failed refresh would loop.
 */
export const authHttp = axios.create({ baseURL: API_URL, timeout: TIMEOUT_MS });

/** Client for every other endpoint: adds the Bearer token and transparently refreshes it on 401. */
export const api = axios.create({ baseURL: API_URL, timeout: TIMEOUT_MS });

// ------------------------------------------------------------------ session-expired hook

let onSessionExpired: (() => void) | null = null;

/** AuthContext registers itself here to be signed out when the refresh token is rejected. */
export function setSessionExpiredHandler(handler: (() => void) | null): void {
  onSessionExpired = handler;
}

class SessionExpiredError extends Error {
  constructor() {
    super('Session expired');
    this.name = 'SessionExpiredError';
  }
}

// ------------------------------------------------------------------ refresh (single flight)

let inflightRefresh: Promise<string> | null = null;

/**
 * Exchanges the stored refresh token for a new pair. Concurrent callers share ONE request:
 * the backend rotates the refresh token, so a second parallel call would use an already-rotated one.
 *
 * - refresh token rejected (4xx) -> tokens are wiped, the app is signed out, SessionExpiredError;
 * - network trouble / 5xx        -> the session is KEPT (being offline is not being logged out), the error is rethrown.
 */
function refreshAccessToken(): Promise<string> {
  if (!inflightRefresh) {
    inflightRefresh = (async () => {
      const refreshToken = await tokenStorage.getRefresh();
      if (!refreshToken) {
        await expireSession();
        throw new SessionExpiredError();
      }
      try {
        const { data } = await authHttp.post<TokenPair>(`${AUTH_PREFIX}/refresh`, { refresh_token: refreshToken });
        await tokenStorage.save(data);
        return data.access_token;
      } catch (error) {
        const status = axios.isAxiosError(error) ? error.response?.status : undefined;
        if (status !== undefined && status >= 400 && status < 500 && status !== 408 && status !== 429) {
          await expireSession();
          throw new SessionExpiredError();
        }
        throw error;
      }
    })().finally(() => {
      inflightRefresh = null;
    });
  }
  return inflightRefresh;
}

async function expireSession(): Promise<void> {
  await tokenStorage.clear();
  onSessionExpired?.();
}

// ------------------------------------------------------------------ interceptors

type RetriableConfig = InternalAxiosRequestConfig & { _retried?: boolean };

api.interceptors.request.use(async (config) => {
  const token = await tokenStorage.getAccess();
  if (token) config.headers.set('Authorization', `Bearer ${token}`);
  return config;
});

api.interceptors.response.use(
  (response) => response,
  async (error: unknown) => {
    if (!axios.isAxiosError(error)) throw error;
    const original = error.config as RetriableConfig | undefined;
    if (error.response?.status !== 401 || !original || original._retried) throw error;
    original._retried = true; // one retry per request, never a loop

    try {
      // If a parallel request already refreshed while this one was in flight, just reuse the new token.
      const sent = String(original.headers.get('Authorization') ?? '').replace(/^Bearer /, '');
      const stored = await tokenStorage.getAccess();
      const token = stored && stored !== sent ? stored : await refreshAccessToken();
      original.headers.set('Authorization', `Bearer ${token}`);
    } catch (refreshError) {
      if (refreshError instanceof SessionExpiredError) throw error; // caller sees the original 401; the app is already signed out
      throw refreshError; // offline etc.: surface the network error, keep the session
    }
    return api(original);
  },
);

// ------------------------------------------------------------------ error text for the UI

export const NETWORK_ERROR_TEXT = API_URL_CONFIGURED
  ? 'Не удаётся связаться с сервером. Проверьте сеть и адрес сервера.'
  : 'Адрес сервера не настроен: замените YOUR_LAN_IP в mobile_app/.env на IPv4 компьютера (для эмулятора Android — 10.0.2.2) и перезапустите Expo с флагом -c.';

/** Human-readable (Russian) text for any error thrown by the API layer. */
export function getErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const body = (error as AxiosError<ApiErrorBody>).response?.data;
    const message = body?.error?.message;
    if (typeof message === 'string' && message.trim()) return message;
    if (!error.response) return NETWORK_ERROR_TEXT;
    if (error.response.status === 429) return 'Слишком много попыток. Подождите немного.';
    if (error.response.status >= 500) return 'Ошибка на сервере. Попробуйте позже.';
  }
  return 'Что-то пошло не так. Попробуйте ещё раз.';
}
