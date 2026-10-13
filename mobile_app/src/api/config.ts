const PLACEHOLDER = 'YOUR_LAN_IP';

const raw = (process.env.EXPO_PUBLIC_API_URL ?? '').trim().replace(/\/+$/, '');

/** Base URL of the backend (no trailing slash). Comes from mobile_app/.env. */
export const API_URL = raw;

/** False while .env still has the YOUR_LAN_IP stub (or the variable is missing). */
export const API_URL_CONFIGURED = raw !== '' && !raw.includes(PLACEHOLDER);

export const AUTH_PREFIX = '/api/v1/mobile/auth';

if (!API_URL_CONFIGURED) {
  console.warn(
    'EXPO_PUBLIC_API_URL is not set: replace YOUR_LAN_IP in mobile_app/.env with this PC\'s IPv4 ' +
      '(or 10.0.2.2 for the Android emulator) and restart Metro with `npx expo start -c`.',
  );
}
