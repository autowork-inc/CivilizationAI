// A single global toast. `toast.show("text")` from anywhere; <Toast /> renders it.
export const toast = $state({ text: "", tone: "info", id: 0 });

let timer;

export function showToast(text, tone = "info") {
  toast.text = text;
  toast.tone = tone;
  toast.id += 1;
  clearTimeout(timer);
  timer = setTimeout(() => (toast.text = ""), tone === "error" ? 4500 : 2600);
}
