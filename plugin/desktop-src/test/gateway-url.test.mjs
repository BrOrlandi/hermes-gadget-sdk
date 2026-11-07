// The virtual gadget's gateway URL: a URL typed on the page wins over the one /info chose, and only
// a ws:// or wss:// URL counts.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { checkUrl, gatewayUrl } from '../src/gateway-url.js'

test('the URL /info chose is used, with where it came from', () => {
  const info = { url: 'wss://gadget.example.com/gadget', url_source: 'seen' }
  assert.deepEqual(gatewayUrl(info, ''), { url: 'wss://gadget.example.com/gadget', source: 'seen', label: 'where a paired gadget reached this Hermes' })
  assert.equal(gatewayUrl({ url: 'ws://127.0.0.1:8765/gadget' }, null).source, 'local') // an older plugin: no url_source
  assert.equal(gatewayUrl({ url: 'ws://h/g', url_source: 'surprise' }, '').source, 'local')
  assert.deepEqual(gatewayUrl(null, ''), { url: null, source: null, label: '' })
})

test('a URL typed on the page wins', () => {
  const info = { url: 'ws://127.0.0.1:8765/gadget', url_source: 'dashboard' }
  const got = gatewayUrl(info, ' wss://tunnel.example.net:8443/gadget ')
  assert.deepEqual(got, { url: 'wss://tunnel.example.net:8443/gadget', source: 'custom', label: 'set on this page' })
  assert.equal(gatewayUrl(info, 'https://example.com/gadget').source, 'dashboard') // not a WebSocket URL: ignored
})

test('only ws:// and wss:// URLs with a host are accepted', () => {
  assert.equal(checkUrl('ws://192.168.1.20:8765/gadget'), 'ws://192.168.1.20:8765/gadget')
  assert.equal(checkUrl('WSS://Gadget.Example.com/gadget'), 'wss://gadget.example.com/gadget')
  for (const bad of ['', '   ', 'gadget.example.com', 'http://x/gadget', 'javascript:alert(1)', 'ws://', 'wss://user:pw@host/g', null, undefined]) {
    assert.equal(checkUrl(bad), null, String(bad))
  }
})
