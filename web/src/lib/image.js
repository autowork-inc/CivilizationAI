// An avatar is stored at 256x256 at most and the server refuses more than 256 KB: a photo from the phone camera is scaled down HERE first (canvas -> JPEG),
// so the user never meets the size limit. The server still re-encodes whatever arrives (metadata is stripped there).
const SIDE = 256;
const LIMIT = 250 * 1024;

export async function shrinkImage(file) {
  if (!file.type.startsWith("image/")) throw new Error("Выберите картинку.");
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  for (const quality of [0.9, 0.8, 0.65, 0.5]) {
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (blob && blob.size <= LIMIT) return blob;
  }
  throw new Error("Не получилось уменьшить картинку. Выберите другую.");
}
