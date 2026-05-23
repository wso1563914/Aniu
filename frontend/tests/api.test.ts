import assert from 'node:assert/strict'
import test from 'node:test'

import { api, setStoredToken } from '../src/services/api.ts'

class MemoryStorage implements Storage {
  private readonly store = new Map<string, string>()

  get length() {
    return this.store.size
  }

  clear() {
    this.store.clear()
  }

  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null
  }

  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null
  }

  removeItem(key: string) {
    this.store.delete(key)
  }

  setItem(key: string, value: string) {
    this.store.set(key, value)
  }
}

function installBrowserMocks() {
  const localStorage = new MemoryStorage()
  const sessionStorage = new MemoryStorage()
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  const originalLocalStorage = globalThis.localStorage
  const originalSessionStorage = globalThis.sessionStorage
  const originalLocation = globalThis.location

  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: globalThis,
  })

  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: localStorage,
  })

  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: sessionStorage,
  })

  Object.defineProperty(globalThis, 'location', {
    configurable: true,
    value: {
      pathname: '/settings',
      search: '',
      hash: '',
      href: '/settings',
    },
  })

  return {
    setFetch(handler: typeof fetch) {
      globalThis.fetch = handler
    },
    restore() {
      Object.defineProperty(globalThis, 'window', {
        configurable: true,
        value: originalWindow,
      })
      Object.defineProperty(globalThis, 'localStorage', {
        configurable: true,
        value: originalLocalStorage,
      })
      Object.defineProperty(globalThis, 'sessionStorage', {
        configurable: true,
        value: originalSessionStorage,
      })
      Object.defineProperty(globalThis, 'location', {
        configurable: true,
        value: originalLocation,
      })
      globalThis.fetch = originalFetch
    },
  }
}

test('importSkillArchive 使用认证头并携带超时 signal', async () => {
  const browser = installBrowserMocks()

  try {
    setStoredToken('demo-token')

    browser.setFetch(async (_input, init) => {
      const headers = new Headers(init?.headers)
      assert.equal(headers.get('Authorization'), 'Bearer demo-token')
      assert.ok(init?.body instanceof FormData)
      assert.ok(init?.signal instanceof AbortSignal)
      return new Response(JSON.stringify({ id: 'demo-skill' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })

    const result = await api.importSkillArchive(
      new File(['skill'], 'demo-skill.zip', { type: 'application/zip' }),
    )

    assert.equal(result.id, 'demo-skill')
  } finally {
    browser.restore()
  }
})

test('uploadChatAttachment 使用认证头并携带超时 signal', async () => {
  const browser = installBrowserMocks()

  try {
    setStoredToken('chat-token')

    browser.setFetch(async (_input, init) => {
      const headers = new Headers(init?.headers)
      assert.equal(headers.get('Authorization'), 'Bearer chat-token')
      assert.ok(init?.body instanceof FormData)
      assert.ok(init?.signal instanceof AbortSignal)
      return new Response(JSON.stringify({
        id: 7,
        filename: 'note.txt',
        mime_type: 'text/plain',
        size: 4,
        url: '/api/aniu/chat/uploads/7',
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })

    const attachment = await api.uploadChatAttachment(
      new File(['note'], 'note.txt', { type: 'text/plain' }),
      3,
    )

    assert.equal(attachment.id, 7)
    assert.equal(attachment.filename, 'note.txt')
  } finally {
    browser.restore()
  }
})

test('fetchChatAttachmentBlob 使用认证头并携带超时 signal', async () => {
  const browser = installBrowserMocks()

  try {
    setStoredToken('blob-token')

    browser.setFetch(async (_input, init) => {
      const headers = new Headers(init?.headers)
      assert.equal(headers.get('Authorization'), 'Bearer blob-token')
      assert.ok(init?.signal instanceof AbortSignal)
      return new Response('demo-blob', {
        status: 200,
        headers: { 'Content-Type': 'text/plain' },
      })
    })

    const blob = await api.fetchChatAttachmentBlob(8)

    assert.equal(await blob.text(), 'demo-blob')
  } finally {
    browser.restore()
  }
})

test('deletePersistentSession 使用认证头并发送 DELETE 请求', async () => {
  const browser = installBrowserMocks()

  try {
    setStoredToken('persistent-token')

    browser.setFetch(async (_input, init) => {
      const headers = new Headers(init?.headers)
      assert.equal(headers.get('Authorization'), 'Bearer persistent-token')
      assert.equal(init?.method, 'DELETE')
      assert.ok(init?.signal instanceof AbortSignal)
      return new Response(null, {
        status: 204,
      })
    })

    await api.deletePersistentSession()
  } finally {
    browser.restore()
  }
})
