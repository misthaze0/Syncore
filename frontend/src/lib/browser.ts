/** Only web addresses may enter the embedded browser or the external opener. */
export function normalizeBrowserUrl(raw: string): string | null {
  const value = raw.trim();
  if (!value || /\s/.test(value)) return null;
  if (/^[a-z][a-z\d+.-]*:/i.test(value) && !/^https?:\/\//i.test(value)
    && !/^[\w.-]+:\d+(?:[/?#]|$)/.test(value)) return null;
  const local = /^(?:localhost|[\w.-]+\.local|\d{1,3}(?:\.\d{1,3}){3}|\[[\da-f:]+\])(?::\d+)?(?:[/?#]|$)/i.test(value);
  try {
    const url = new URL(/^https?:\/\//i.test(value) ? value : `${local ? 'http' : 'https'}://${value}`);
    return /^https?:$/.test(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
