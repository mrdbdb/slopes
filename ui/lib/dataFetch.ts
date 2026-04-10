// iOS Safari aggressively caches static JSON even with no-store headers.
// Append a per-session cache-busting query param that's constant during
// a single page load (so we don't re-download the same file multiple
// times) but fresh on every reload.
const SESSION_VERSION = typeof window !== "undefined"
  ? Date.now().toString(36)
  : "ssr"

export function dataUrl(path: string): string {
  const sep = path.includes("?") ? "&" : "?"
  return `${path}${sep}v=${SESSION_VERSION}`
}

export function fetchData(path: string, init?: RequestInit): Promise<Response> {
  return fetch(dataUrl(path), { cache: "no-store", ...init })
}
