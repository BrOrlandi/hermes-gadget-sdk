// Which gateway URL the virtual gadget connects to: one typed on this page wins, else the one the
// dashboard's /info route chose (and why, in `url_source`), so a wrong guess shows and can be fixed.

const SOURCES = {
  custom: 'set on this page',
  config: 'set in the config (public_url)',
  seen: 'where a paired gadget reached this Hermes',
  dashboard: "guessed from the dashboard's address",
  local: 'this computer'
}

/** `text` as a gateway URL (ws:// or wss://, a host, a path), or null if it isn't one. */
export function checkUrl(text) {
  const t = String(text ?? '').trim()
  if (!/^wss?:\/\//i.test(t)) return null
  try {
    const u = new URL(t)
    if ((u.protocol !== 'ws:' && u.protocol !== 'wss:') || !u.hostname || u.username || u.password) return null
    return u.href
  } catch {
    return null
  }
}

/** `{ url, source, label }`: the URL to connect to and where it came from. */
export function gatewayUrl(info, custom) {
  const mine = checkUrl(custom)
  if (mine) return { url: mine, source: 'custom', label: SOURCES.custom }
  if (!info?.url) return { url: null, source: null, label: '' }
  const source = SOURCES[info.url_source] ? info.url_source : 'local'
  return { url: info.url, source, label: SOURCES[source] }
}
