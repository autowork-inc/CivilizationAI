// Fact names and sections now come from the server (web/facts.py: ``group`` / ``title``): the page never shows or builds a name from a machine key.

// The interview stores these in English (they go into the model prompt as they are); the screen shows them in Russian.
const VALUE_LABELS = {
  "address the user informally (ты)": "На «ты»",
  "address the user formally (вы)": "На «вы»",
  "short replies": "Короткие ответы",
  "balanced replies": "Ответы средней длины",
  "detailed replies": "Подробные ответы",
  "gentle, supportive tone": "Мягкий, поддерживающий",
  "neutral tone": "Нейтральный",
  "strict, demanding tone, no coddling": "Строгий, без поблажек",
};

/** The text of a fact that has no name of its own: just what it says (a number-only fact shows the number). */
export function factText(fact) {
  if (fact.value_text) return VALUE_LABELS[fact.value_text] ?? fact.value_text;
  return factValue(fact);
}

export function factValue(fact) {
  const parts = [];
  if (fact.value_num !== null && fact.value_num !== undefined) {
    parts.push(fact.value_unit ? `${fact.value_num} ${fact.value_unit}` : String(fact.value_num));
  }
  if (fact.value_text) parts.push(VALUE_LABELS[fact.value_text] ?? fact.value_text);
  return parts.join(" · ") || "—";
}

export const VISIBILITY = [
  { value: "private", label: "Личное" },
  { value: "busy_only", label: "Занят" },
  { value: "shared", label: "Общее" },
];

export const VISIBILITY_HINT = {
  private: "Никто, кроме вас, не увидит событие.",
  busy_only: "Другим видно только, что вы заняты, без подробностей.",
  shared: "Контакты, которым вы разрешили, увидят название и время.",
};

export const REPEAT_OPTIONS = [
  { value: "", label: "Не повторять" },
  { value: "DAILY", label: "Каждый день" },
  { value: "WEEKLY", label: "Каждую неделю" },
  { value: "MONTHLY", label: "Каждый месяц" },
  { value: "YEARLY", label: "Каждый год" },
];

// The four branches of the character (RPG, migration 0016) and what the decay system takes XP away for (migration 0023).
export const BRANCHES = [
  { id: "focus", label: "Фокус", icon: "🎯", bar: "bg-focus", text: "text-focus" },
  { id: "intellect", label: "Интеллект", icon: "🧠", bar: "bg-intellect", text: "text-intellect" },
  { id: "body", label: "Тело", icon: "💪", bar: "bg-body", text: "text-body" },
  { id: "connection", label: "Связи", icon: "🤝", bar: "bg-connection", text: "text-connection" },
];

export const PENALTY_LABELS = {
  body: "Давно не было тренировок",
  task: "Просрочена задача",
  other: "Штраф",
};

// The closed vocabulary of shared moods (civ.shared_mood): a word the user confirmed, never a guess.
export const MOOD_LABELS = {
  energetic: { icon: "⚡", label: "Бодрое" },
  good: { icon: "🙂", label: "Хорошее" },
  calm: { icon: "😌", label: "Спокойное" },
  tired: { icon: "😴", label: "Усталость" },
  stressed: { icon: "😬", label: "Стресс" },
  irritated: { icon: "😤", label: "Раздражение" },
  sad: { icon: "😔", label: "Грусть" },
  overwhelmed: { icon: "🤯", label: "Перегруз" },
};

/** "1 день / 2 дня / 5 дней" */
export function plural(n, one, few, many) {
  const m10 = n % 10;
  const m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}
