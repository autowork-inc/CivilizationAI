import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';

import { setSessionExpiredHandler } from '@/api/apiClient';
import { loginRequest, logoutRequest, registerRequest } from '@/api/authApi';
import { tokenStorage } from '@/api/tokenStorage';
import type { SessionUser, TokenPair } from '@/api/types';

export type AuthStatus = 'loading' | 'signedIn' | 'signedOut';

type AuthContextValue = {
  status: AuthStatus;
  user: SessionUser | null;
  /** Throws an axios error on failure; screens turn it into text with getErrorMessage(). */
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<SessionUser | null>(null);

  // App start: tokens in SecureStore -> straight into the app. If the access token is already
  // stale, the first API call refreshes it; if the refresh token is dead, the interceptor signs us out.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let signedIn = false;
      let stored: SessionUser | null = null;
      try {
        signedIn = Boolean(await tokenStorage.getRefresh());
        stored = signedIn ? await tokenStorage.getUser() : null;
      } catch {
        // an unreadable keystore is treated as "not signed in"
      }
      if (cancelled) return;
      setUser(stored);
      setStatus(signedIn ? 'signedIn' : 'signedOut');
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // The refresh token was rejected somewhere deep in the network layer -> back to the login screen.
  useEffect(() => {
    setSessionExpiredHandler(() => {
      setUser(null);
      setStatus('signedOut');
    });
    return () => setSessionExpiredHandler(null);
  }, []);

  const startSession = useCallback(async (pair: TokenPair) => {
    await tokenStorage.save(pair);
    setUser({ userId: pair.user_id, timezone: pair.timezone });
    setStatus('signedIn');
  }, []);

  const login = useCallback(
    async (email: string, password: string) => startSession(await loginRequest(email.trim().toLowerCase(), password)),
    [startSession],
  );

  const register = useCallback(
    async (email: string, password: string) => startSession(await registerRequest(email.trim().toLowerCase(), password)),
    [startSession],
  );

  const logout = useCallback(async () => {
    const refreshToken = await tokenStorage.getRefresh().catch(() => null);
    // Local sign-out first: it must work offline and must not wait for the server.
    await tokenStorage.clear();
    setUser(null);
    setStatus('signedOut');
    // Then revoke the session on the server, best effort (if it fails, the token just expires with its TTL).
    if (refreshToken) logoutRequest(refreshToken).catch(() => undefined);
  }, []);

  const value = useMemo(() => ({ status, user, login, register, logout }), [status, user, login, register, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
}
