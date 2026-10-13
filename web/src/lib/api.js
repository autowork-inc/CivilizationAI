import { initData } from "./tg.js";

export class ApiError extends Error {
  constructor(status, body) {
    const error = body?.error ?? {};
    super(error.message || "Что-то пошло не так. Попробуйте ещё раз.");
    this.status = status;
    this.code = error.code ?? "unknown";
    this.details = error; // e.g. current_fact_id / current_updated_at on a 409
  }
  get isConflict() {
    return this.status === 409;
  }
  get isAuth() {
    return this.status === 401 || this.status === 403;
  }
}

async function request(method, path, { body, query } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(query ?? {})) url.searchParams.set(key, value);
  let response;
  try {
    response = await fetch(url, {
      method,
      cache: "no-store",
      headers: {
        Authorization: `tma ${initData()}`,
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, { error: { code: "network", message: "Нет связи с сервером." } });
  }
  if (response.status === 204) return null;
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    /* an empty or non-JSON error body */
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

/** An image behind the signed header (an <img src> cannot send it): fetched as a blob and shown through an object URL. null = no picture. */
async function requestImage(path) {
  let response;
  try {
    response = await fetch(new URL(path, window.location.origin), { cache: "no-store", headers: { Authorization: `tma ${initData()}` } });
  } catch {
    return null;
  }
  if (!response.ok) return null;
  return URL.createObjectURL(await response.blob());
}

/** The avatar goes up as the raw request body (the backend re-encodes it); not JSON, not multipart. */
async function uploadImage(path, file) {
  let response;
  try {
    response = await fetch(new URL(path, window.location.origin), {
      method: "POST",
      cache: "no-store",
      headers: { Authorization: `tma ${initData()}`, "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });
  } catch {
    throw new ApiError(0, { error: { code: "network", message: "Нет связи с сервером." } });
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    /* an empty or non-JSON error body */
  }
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

const enc = encodeURIComponent;

export const api = {
  me: () => request("GET", "/api/me"),

  profile: () => request("GET", "/api/profile"),
  editFact: (key, body) => request("PUT", `/api/profile/facts/${enc(key)}`, { body }),
  deleteFact: (key, expectedFactId) =>
    request("DELETE", `/api/profile/facts/${enc(key)}`, { query: { expected_fact_id: expectedFactId } }),
  setTimezone: (timezone) => request("PUT", "/api/profile/timezone", { body: { timezone } }),

  principles: () => request("GET", "/api/principles"),
  createPrinciple: (body) => request("POST", "/api/principles", { body }),
  editPrinciple: (key, body) => request("PUT", `/api/principles/${enc(key)}`, { body }),
  restorePrinciple: (key, expectedFactId) =>
    request("POST", `/api/principles/${enc(key)}/restore`, { body: { expected_fact_id: expectedFactId } }),
  deletePrinciple: (key, expectedFactId) =>
    request("DELETE", `/api/principles/${enc(key)}`, { query: { expected_fact_id: expectedFactId } }),

  events: (start, end) => request("GET", "/api/events", { query: { start: start.toISOString(), end: end.toISOString() } }),
  createEvent: (body) => request("POST", "/api/events", { body }),
  patchEvent: (id, body) => request("PATCH", `/api/events/${id}`, { body }),
  deleteEvent: (id, expectedUpdatedAt) =>
    request("DELETE", `/api/events/${id}`, { query: { expected_updated_at: expectedUpdatedAt } }),
  getEvent: (id) => request("GET", `/api/events/${id}`),
  cancelOccurrence: (id, originalStart) =>
    request("POST", `/api/events/${id}/occurrences/cancel`, { body: { original_start: originalStart } }),
  overrideOccurrence: (id, body) => request("POST", `/api/events/${id}/occurrences/override`, { body }),
  personas: () => request("GET", "/api/personas"),

  reminders: () => request("GET", "/api/reminders"),
  setReminder: (triggerKey, body) => request("PUT", `/api/reminders/${enc(triggerKey)}`, { body }),
  setQuietHours: (start, end) => request("PUT", "/api/reminders/quiet-hours", { body: { start, end } }),
  setCheckins: (perDay) => request("PUT", "/api/reminders/checkins", { body: { per_day: perDay } }),

  // Every mutation answers with the whole fresh snapshot of the screen (contacts, open invites, limits).
  connections: () => request("GET", "/api/connections"),
  createInvite: () => request("POST", "/api/connections/invites"),
  revokeInvite: (id) => request("DELETE", `/api/connections/invites/${enc(id)}`),
  revokeAllInvites: () => request("DELETE", "/api/connections/invites"),
  renameContact: (id, alias) => request("PUT", `/api/connections/contacts/${enc(id)}/alias`, { body: { alias } }),
  blockContact: (id) => request("POST", `/api/connections/contacts/${enc(id)}/block`),
  unblockContact: (id) => request("POST", `/api/connections/contacts/${enc(id)}/unblock`),
  removeContact: (id) => request("DELETE", `/api/connections/contacts/${enc(id)}`),
  setGrant: (id, scope, enabled) =>
    request("PUT", `/api/connections/contacts/${enc(id)}/grants/${enc(scope)}`, { body: { enabled } }),
  audit: (before) => request("GET", "/api/connections/audit", { query: before ? { before } : {} }),

  character: () => request("GET", "/api/character/me"),
  patchCharacter: (body) => request("PATCH", "/api/character/settings", { body }),
  uploadAvatar: (file) => uploadImage("/api/character/avatar", file),
  avatar: () => requestImage("/api/character/avatar"),

  tavern: () => request("GET", "/api/tavern/feed"),
  friendAvatar: (url) => requestImage(url),
  cheer: (contactId, kind = "focus") => request("POST", "/api/tavern/cheer", { body: { contact_id: contactId, kind } }),

  dashboard: (days = 14) => request("GET", "/api/dashboard/stats", { query: { days } }),
};
