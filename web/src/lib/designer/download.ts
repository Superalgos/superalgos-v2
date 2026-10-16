/** Save a text file through the browser (no server): an object URL and a click on a hidden link. */
export function saveText(fileName: string, text: string, mime = "application/json"): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** `ema-crossover.json`, or `ema-crossover.invalid.json` when the document did not pass validation. */
export function fileNameFor(name: string, valid: boolean): string {
  const stem = name.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "") || "strategy";
  return `${stem}${valid ? "" : ".invalid"}.json`;
}
