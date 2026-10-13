/** Response of /auth/register, /auth/login and /auth/refresh (backend: TokenPair). */
export type TokenPair = {
  access_token: string;
  refresh_token: string;
  token_type: string;
  /** Lifetime of the access token, seconds. */
  expires_in: number;
  user_id: string;
  timezone: string;
};

/** Who is signed in. Persisted next to the tokens so it survives an app restart. */
export type SessionUser = {
  userId: string;
  timezone: string;
};

/** Backend error envelope: {"error": {"code", "message", ...}}. */
export type ApiErrorBody = {
  error?: {
    code?: string;
    message?: string;
    details?: { reason?: string } & Record<string, unknown>;
  };
};
