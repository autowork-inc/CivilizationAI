import * as SecureStore from 'expo-secure-store';

import type { SessionUser, TokenPair } from './types';

// SecureStore keys may contain only letters, digits, ".", "-" and "_".
const ACCESS_KEY = 'civ.access_token';
const REFRESH_KEY = 'civ.refresh_token';
const USER_KEY = 'civ.session_user';

export const tokenStorage = {
  getAccess: () => SecureStore.getItemAsync(ACCESS_KEY),
  getRefresh: () => SecureStore.getItemAsync(REFRESH_KEY),

  async getUser(): Promise<SessionUser | null> {
    const raw = await SecureStore.getItemAsync(USER_KEY);
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw) as Partial<SessionUser>;
      return typeof parsed.userId === 'string' && typeof parsed.timezone === 'string'
        ? { userId: parsed.userId, timezone: parsed.timezone }
        : null;
    } catch {
      return null;
    }
  },

  /** Stores both tokens (a rotated refresh token replaces the old one) and who they belong to. */
  async save(pair: TokenPair): Promise<void> {
    await SecureStore.setItemAsync(ACCESS_KEY, pair.access_token);
    await SecureStore.setItemAsync(REFRESH_KEY, pair.refresh_token);
    await SecureStore.setItemAsync(USER_KEY, JSON.stringify({ userId: pair.user_id, timezone: pair.timezone }));
  },

  async clear(): Promise<void> {
    await Promise.all([
      SecureStore.deleteItemAsync(ACCESS_KEY),
      SecureStore.deleteItemAsync(REFRESH_KEY),
      SecureStore.deleteItemAsync(USER_KEY),
    ]);
  },
};
