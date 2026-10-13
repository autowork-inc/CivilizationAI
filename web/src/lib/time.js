// Time zones done right: an event is planned on the WALL CLOCK of its own IANA zone; the server stores the instant and expands
// repetitions in that zone. Everything here converts between "a date and a time on a wall clock in zone X" and a real instant.

const PARTS = {};

function formatter(timeZone) {
  return (PARTS[timeZone] ??= new Intl.DateTimeFormat("en-CA", {
    timeZone,
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }));
}

/** Wall-clock fields of an instant in a zone. */
export function partsInZone(date, timeZone) {
  const out = {};
  for (const { type, value } of formatter(timeZone).formatToParts(date)) out[type] = Number(value);
  return out;
}

/** "YYYY-MM-DD" of an instant on the wall clock of a zone. */
export function dayKey(date, timeZone) {
  const p = partsInZone(date, timeZone);
  return `${p.year}-${String(p.month).padStart(2, "0")}-${String(p.day).padStart(2, "0")}`;
}

/** "HH:MM" of an instant on the wall clock of a zone. */
export function timeKey(date, timeZone) {
  const p = partsInZone(date, timeZone);
  return `${String(p.hour).padStart(2, "0")}:${String(p.minute).padStart(2, "0")}`;
}

/** The instant at which the wall clock of `timeZone` shows `dateKey` (YYYY-MM-DD) `time` (HH:MM). DST-safe (two refinements). */
export function zonedToUtc(dateKeyValue, time, timeZone) {
  const [y, m, d] = dateKeyValue.split("-").map(Number);
  const [hh, mm] = time.split(":").map(Number);
  const wanted = Date.UTC(y, m - 1, d, hh, mm);
  let guess = wanted;
  for (let i = 0; i < 2; i++) {
    const p = partsInZone(new Date(guess), timeZone);
    const shown = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
    guess = wanted - (shown - guess);
  }
  return new Date(guess);
}

export function addDaysKey(dateKeyValue, days) {
  const [y, m, d] = dateKeyValue.split("-").map(Number);
  const moved = new Date(Date.UTC(y, m - 1, d + days));
  return moved.toISOString().slice(0, 10);
}

export const todayKey = (timeZone) => dayKey(new Date(), timeZone);

/** RRULE weekday code of a calendar date (the date has no zone: it is the user's own calendar day). */
export function weekdayCode(dateKeyValue) {
  const [y, m, d] = dateKeyValue.split("-").map(Number);
  return ["SU", "MO", "TU", "WE", "TH", "FR", "SA"][new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
}

export function formatDayHeading(dateKeyValue) {
  const [y, m, d] = dateKeyValue.split("-").map(Number);
  const text = new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("ru-RU", {
    weekday: "long",
    day: "numeric",
    month: "long",
    timeZone: "UTC",
  });
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** "3 окт., 14:20": an instant (ISO string from the server) on the wall clock of a zone. */
export function formatMoment(iso, timeZone) {
  return new Date(iso).toLocaleString("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: timeZone || undefined,
  });
}

/** "3 октября 2026": the calendar day of an instant in a zone. */
export function formatDay(iso, timeZone) {
  return new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "long", year: "numeric", timeZone: timeZone || undefined });
}

export function formatRangeTitle(startKey, endKeyInclusive) {
  const fmt = (key) => {
    const [y, m, d] = key.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("ru-RU", { day: "numeric", month: "short", timeZone: "UTC" });
  };
  return `${fmt(startKey)} – ${fmt(endKeyInclusive)}`;
}

/** UNTIL value of an RRULE: the END of the chosen local day, as UTC (`YYYYMMDDTHHMMSSZ`). */
export function untilValue(dateKeyValue, timeZone) {
  const end = zonedToUtc(addDaysKey(dateKeyValue, 1), "00:00", timeZone);
  const last = new Date(end.getTime() - 1000);
  return last.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
}

export function parseUntil(rule) {
  const match = /UNTIL=(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})Z/.exec(rule ?? "");
  return match ? new Date(Date.UTC(+match[1], +match[2] - 1, +match[3], +match[4], +match[5], +match[6])) : null;
}

export const allTimeZones = () => {
  try {
    return Intl.supportedValuesOf("timeZone");
  } catch {
    return [];
  }
};
