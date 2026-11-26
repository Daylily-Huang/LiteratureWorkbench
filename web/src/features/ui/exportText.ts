export function markdownText(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/([\\`*_{}\[\]#!|])/g, "\\$1");
}
export function downloadText(filename: string, text: string, mime = "text/markdown;charset=utf-8") {
  downloadBlob(filename, new Blob([text], { type: mime }));
}
export function downloadBlob(filename: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename.replace(/[<>:"/\\|?*\x00-\x1f]/g, "_");
  document.body.append(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
