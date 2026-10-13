import { authHttp } from './apiClient';
import { AUTH_PREFIX } from './config';
import type { TokenPair } from './types';

/** Device time zone as the backend wants it: an IANA region ("Europe/Copenhagen") or "UTC". */
export function deviceTimezone(): string {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (zone && (zone === 'UTC' || zone.includes('/'))) return zone;
  } catch {
    // fall through
  }
  return 'UTC';
}

export async function loginRequest(email: string, password: string): Promise<TokenPair> {
  const { data } = await authHttp.post<TokenPair>(`${AUTH_PREFIX}/login`, { email, password });
  return data;
}

export async function registerRequest(email: string, password: string): Promise<TokenPair> {
  const { data } = await authHttp.post<TokenPair>(`${AUTH_PREFIX}/register`, {
    email,
    password,
    timezone: deviceTimezone(),
  });
  return data;
}

/** Revokes the session on the server. Idempotent: the backend answers 204 even for an unknown token. */
export async function logoutRequest(refreshToken: string): Promise<void> {
  await authHttp.post(`${AUTH_PREFIX}/logout`, { refresh_token: refreshToken });
}
