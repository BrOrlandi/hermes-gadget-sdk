// The Gadgets page of the Hermes desktop app: the devices this Hermes knows, read-only.
//
// The desktop half of the plugin. The app loads this file as it is, with no build step, so
// it is plain ESM with jsx() calls, and it may import only '@hermes/plugin-sdk', 'react' and
// 'react/jsx-runtime'. It reads the same route as the web dashboard's tab,
// GET /api/plugins/gadget/devices (plugin/dashboard/plugin_api.py), through ctx.rest, which
// sends the app's own credentials for the backend it is connected to, local or remote.

import {
  Badge,
  Button,
  EmptyState,
  ErrorState,
  host,
  PALETTE_AREA,
  relativeTime,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  useQuery
} from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

export const PATH = '/gadget'
export const REFRESH_MS = 10000
// A request that never answers fails after this long, and the next poll tries again.
export const REQUEST_TIMEOUT_MS = 15000

const cell = 'px-3 py-2 text-left align-top'
const muted = 'text-(--ui-text-tertiary)'

function when(ts) {
  return finite(ts) ? relativeTime(ts * 1000) : '-'
}

function fullDate(ts) {
  return finite(ts) ? new Date(ts * 1000).toLocaleString() : ''
}

// [label, Badge variant] for each badge a device shows: a connected device that is not yet
// approved is both Online and Waiting to pair, as in the web tab.
export function statusBadges(device) {
  const badges = []
  if (device.online) badges.push(['Online', 'success'])
  if (device.pending) badges.push(['Waiting to pair', 'warn'])
  if (!badges.length) badges.push(['Offline', 'outline'])
  return badges
}

const finite = value => typeof value === 'number' && Number.isFinite(value)

export function batteryText(battery) {
  if (!battery) return '-'
  const parts = []
  if (finite(battery.battery_percent)) parts.push(`${Math.round(battery.battery_percent)}%`)
  else if (finite(battery.battery_mv)) parts.push(`${(battery.battery_mv / 1000).toFixed(2)} V`)
  if (battery.charging) parts.push('charging')
  else if (battery.external_power) parts.push('on USB')
  if (battery.battery_present === false && !parts.length) parts.push('no battery')
  return parts.join(', ') || '-'
}

function DeviceRow({ device }) {
  const seen = device.online ? device.connected_at : device.last_seen
  return jsxs('tr', {
    className: 'border-t border-(--ui-stroke-secondary)',
    children: [
      jsxs('td', {
        className: cell,
        children: [
          jsx('div', { className: 'font-medium', children: device.name || device.id }),
          jsx('div', { className: `font-mono text-xs ${muted}`, children: device.id })
        ]
      }),
      jsx('td', {
        className: cell,
        children: jsx('span', {
          className: 'inline-flex gap-1',
          children: statusBadges(device).map(([label, variant]) => jsx(Badge, { variant, children: label }, label))
        })
      }),
      jsx('td', { className: cell, children: batteryText(device.online ? device.battery : null) }),
      jsx('td', { className: cell, children: device.board || '-' }),
      jsx('td', { className: `${cell} font-mono`, children: device.firmware || '-' }),
      jsx('td', { className: cell, title: fullDate(seen), children: device.online ? 'now' : when(device.last_seen) })
    ]
  })
}

function DeviceTable({ devices }) {
  const heads = ['Device', 'Status', 'Battery', 'Board', 'Firmware', 'Last seen']
  return jsx('div', {
    className: 'overflow-x-auto',
    children: jsxs('table', {
      className: 'w-full text-sm',
      children: [
        jsx('thead', {
          className: `text-xs uppercase ${muted}`,
          children: jsx('tr', { children: heads.map(h => jsx('th', { className: cell, children: h }, h)) })
        }),
        jsx('tbody', { children: devices.map(d => jsx(DeviceRow, { device: d }, d.id)) })
      ]
    })
  })
}

function GatewayNote({ gateway, count }) {
  if (!gateway || gateway.running || !count) return null
  const since = finite(gateway.updated_at) ? ` Its last report was ${when(gateway.updated_at)}.` : ''
  return jsx('p', {
    className: `text-sm ${muted}`,
    children: `The gateway is not running, or has not reported yet, so online state and battery are unknown.${since} Start it with hermes gateway run.`
  })
}

export function GadgetsView({ data, error, loading, onRefresh }) {
  const devices = (data && data.devices) || []
  const online = devices.filter(d => d.online).length
  let body = null
  if (error && !data) {
    body = jsx(ErrorState, {
      title: 'Could not load the devices',
      description: `${String((error && error.message) || error)}. The gadget plugin must be enabled on the Hermes this app is connected to (plugins.enabled), and its dashboard routes loaded.`
    })
  } else if (data && !devices.length) {
    body = jsx(EmptyState, {
      title: 'No gadgets yet',
      description: 'Set up a board from the installer, then approve it with hermes gadget pair.'
    })
  } else if (devices.length) {
    body = jsxs('div', {
      className: 'flex flex-col gap-3',
      children: [jsx(GatewayNote, { count: devices.length, gateway: data.gateway }), jsx(DeviceTable, { devices })]
    })
  }
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col gap-4 overflow-auto p-6',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsxs('div', {
            children: [
              jsx('h1', { className: 'text-lg font-semibold', children: 'Gadgets' }),
              data ? jsx('p', { className: `text-sm ${muted}`, children: `${devices.length} known, ${online} online.` }) : null
            ]
          }),
          jsx(Button, { disabled: loading, onClick: onRefresh, size: 'sm', variant: 'outline', children: 'Refresh' })
        ]
      }),
      body
    ]
  })
}

export default {
  id: 'gadget',
  name: 'Gadgets',
  description: 'The Hermes Gadget devices this Hermes knows: online state, battery, board, firmware and last seen.',
  register(ctx) {
    function GadgetsPage() {
      // The app's React Query client gives the polling rules the web tab keeps by hand: a poll
      // that comes while a request is in flight joins it instead of starting another, Refresh
      // replaces it, and only the latest answer is shown. The app adds ?profile= itself when
      // one remote Hermes serves several profiles.
      const query = useQuery({
        queryKey: ['gadget', 'devices'],
        queryFn: () => ctx.rest('/devices', { timeoutMs: REQUEST_TIMEOUT_MS }),
        refetchInterval: REFRESH_MS,
        retry: false
      })
      return jsx(GadgetsView, {
        data: query.data,
        error: query.error,
        loading: query.isFetching,
        onRefresh: () => query.refetch()
      })
    }

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(GadgetsPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, order: 60, data: { codicon: 'watch', label: 'Gadgets', path: PATH } },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: 'gadget.open',
          label: 'Open Gadgets',
          keywords: ['gadget', 'device', 'watch', 'battery'],
          run: () => host.navigate(PATH)
        }
      }
    ])
  }
}
