import type { AccountOverview, AppSettings, ChatAttachment, ChatRequest, ChatResponse, ChatSession, ChatSessionMessagesPayload, LoginRequest, LoginResponse, PersistentSession, PersistentSessionMessagesPayload, RawToolPreviewDetail, RunDetail, RunSummary, RunSummaryPage, ScheduleConfig, SkillInfo, SkillListItem } from '../types.ts'
import {
  LOGIN_NOTICE_STORAGE_KEY,
  LOGIN_REDIRECT_STORAGE_KEY,
  LOGIN_STORAGE_KEY,
  TOKEN_STORAGE_KEY,
} from '../constants.ts'

const API_PREFIX = '/api/aniu'
const DEFAULT_TIMEOUT_MS = 20000

function readStorageItem(key: string): string | null {
  return window.localStorage.getItem(key)
}

function writeStorageItem(key: string, value: string): void {
  window.localStorage.setItem(key, value)
}

function removeStorageItem(key: string): void {
  window.localStorage.removeItem(key)
}

function readSessionStorageItem(key: string): string | null {
  return window.sessionStorage.getItem(key)
}

function writeSessionStorageItem(key: string, value: string): void {
  window.sessionStorage.setItem(key, value)
}

function removeSessionStorageItem(key: string): void {
  window.sessionStorage.removeItem(key)
}

export function getStoredToken(): string | null {
  return readStorageItem(TOKEN_STORAGE_KEY)
}

export function setStoredToken(token: string): void {
  writeStorageItem(TOKEN_STORAGE_KEY, token)
}

export function clearStoredToken(): void {
  removeStorageItem(TOKEN_STORAGE_KEY)
}

export function getStoredLoginFlag(): boolean {
  return readStorageItem(LOGIN_STORAGE_KEY) === 'true'
}

export function setStoredLoginFlag(authenticated: boolean): void {
  writeStorageItem(LOGIN_STORAGE_KEY, String(authenticated))
}

export function clearStoredLoginFlag(): void {
  removeStorageItem(LOGIN_STORAGE_KEY)
}

export function setStoredLoginRedirect(path: string): void {
  writeSessionStorageItem(LOGIN_REDIRECT_STORAGE_KEY, path)
}

export function clearStoredLoginRedirect(): void {
  removeSessionStorageItem(LOGIN_REDIRECT_STORAGE_KEY)
}

export function consumeStoredLoginRedirect(): string | null {
  const value = readSessionStorageItem(LOGIN_REDIRECT_STORAGE_KEY)
  if (value) {
    removeSessionStorageItem(LOGIN_REDIRECT_STORAGE_KEY)
  }
  return value
}

export function setStoredLoginNotice(message: string): void {
  writeSessionStorageItem(LOGIN_NOTICE_STORAGE_KEY, message)
}

export function clearStoredLoginNotice(): void {
  removeSessionStorageItem(LOGIN_NOTICE_STORAGE_KEY)
}

export function consumeStoredLoginNotice(): string | null {
  const value = readSessionStorageItem(LOGIN_NOTICE_STORAGE_KEY)
  if (value) {
    removeSessionStorageItem(LOGIN_NOTICE_STORAGE_KEY)
  }
  return value
}

function currentLocationPath(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`
}

function handleUnauthorized(message = '登录已过期，请重新登录。'): never {
  clearStoredToken()
  clearStoredLoginFlag()
  setStoredLoginNotice(message)
  if (window.location.pathname !== '/login') {
    setStoredLoginRedirect(currentLocationPath())
    window.location.href = '/login'
  }
  throw new Error(message)
}

interface RequestOptions extends RequestInit {
  timeoutMs?: number
  skipAuthRedirect?: boolean
  setJsonContentType?: boolean
}

interface ListRunsOptions {
  limit?: number
  date?: string
  status?: string
  beforeId?: number
}

interface ListChatMessagesOptions {
  limit?: number
  beforeId?: number
}

async function request<T>(url: string, options?: RequestOptions): Promise<T> {
  const response = await fetchWithTimeout(url, options)

  if (!response.ok) {
    throw new Error(await buildErrorMessage(response))
  }

  if (response.status === 204) {
    return undefined as T
  }

  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    return undefined as T
  }

  const text = await response.text()
  if (!text.trim()) {
    return undefined as T
  }

  return JSON.parse(text) as T
}

async function fetchWithTimeout(url: string, options?: RequestOptions): Promise<Response> {
  const timeoutMs = options?.timeoutMs ?? DEFAULT_TIMEOUT_MS
  const controller = new AbortController()
  const timeoutId = window.setTimeout(() => controller.abort(), timeoutMs)

  const {
    timeoutMs: _ignored,
    skipAuthRedirect = false,
    setJsonContentType = true,
    ...fetchOptions
  } = options ?? {}

  const headers = new Headers(fetchOptions.headers ?? {})
  const token = getStoredToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  if (setJsonContentType && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(url, {
    ...fetchOptions,
    headers,
    signal: fetchOptions?.signal ?? controller.signal,
  }).catch((error: unknown) => {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error('请求超时，请稍后重试。')
    }
    throw error
  }).finally(() => {
    window.clearTimeout(timeoutId)
  })

  if (response.status === 401 && !skipAuthRedirect) {
    handleUnauthorized()
  }

  return response
}

async function buildErrorMessage(response: Response): Promise<string> {
  let message = `请求失败: ${response.status}`
  try {
    const payload = await response.json()
    if (response.status === 422 && Array.isArray(payload.detail)) {
      const fields = payload.detail
        .map((err: { loc?: string[]; msg?: string }) => {
          const field = (err.loc ?? []).filter((s: string) => s !== 'body').join('.')
          return field ? `${field}: ${err.msg ?? '验证失败'}` : (err.msg ?? '验证失败')
        })
        .join('; ')
      return fields || '请求参数验证失败'
    }
    return payload.detail ?? payload.message ?? message
  } catch {
    return message
  }
}

export const api = {
  login(payload: LoginRequest) {
    return request<LoginResponse>(`${API_PREFIX}/login`, {
      method: 'POST',
      body: JSON.stringify(payload),
      skipAuthRedirect: true,
    })
  },
  getSettings() {
    return request<AppSettings>(`${API_PREFIX}/settings`)
  },
  updateSettings(payload: Omit<AppSettings, 'id' | 'created_at' | 'updated_at'>) {
    return request<AppSettings>(`${API_PREFIX}/settings`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
  },
  listSkills() {
    return request<SkillListItem[]>(`${API_PREFIX}/skills`)
  },
  importSkillHubSkill(slugOrUrl: string) {
    return request<SkillInfo>(`${API_PREFIX}/skills/import-skillhub`, {
      method: 'POST',
      body: JSON.stringify({ slug_or_url: slugOrUrl }),
      timeoutMs: 5 * 60 * 1000,
    })
  },
  importClawHubSkill(slugOrUrl: string) {
    return request<SkillInfo>(`${API_PREFIX}/skills/import-clawhub`, {
      method: 'POST',
      body: JSON.stringify({ slug_or_url: slugOrUrl }),
      timeoutMs: 60000,
    })
  },
  async importSkillArchive(file: File): Promise<SkillInfo> {
    const formData = new FormData()
    formData.append('file', file)

    const response = await fetchWithTimeout(`${API_PREFIX}/skills/import-zip`, {
      method: 'POST',
      body: formData,
      timeoutMs: 5 * 60 * 1000,
      setJsonContentType: false,
    })
    if (!response.ok) {
      throw new Error(await buildErrorMessage(response))
    }
    return (await response.json()) as SkillInfo
  },
  reloadSkills() {
    return request<SkillListItem[]>(`${API_PREFIX}/skills/reload`, {
      method: 'POST',
    })
  },
  enableSkill(skillId: string) {
    return request<SkillInfo>(`${API_PREFIX}/skills/${encodeURIComponent(skillId)}/enable`, {
      method: 'POST',
    })
  },
  disableSkill(skillId: string) {
    return request<SkillInfo>(`${API_PREFIX}/skills/${encodeURIComponent(skillId)}/disable`, {
      method: 'POST',
    })
  },
  deleteSkill(skillId: string) {
    return request<void>(`${API_PREFIX}/skills/${encodeURIComponent(skillId)}`, {
      method: 'DELETE',
    })
  },
  getSchedule() {
    return request<ScheduleConfig[]>(`${API_PREFIX}/schedule`)
  },
  updateSchedule(payload: Array<Partial<ScheduleConfig>>) {
    return request<ScheduleConfig[]>(`${API_PREFIX}/schedule`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
  },
  runNow(scheduleId?: number, runType?: 'analysis' | 'trade') {
    const params = new URLSearchParams()
    if (typeof scheduleId === 'number') {
      params.set('schedule_id', String(scheduleId))
    }
    if (runType) {
      params.set('run_type', runType)
    }
    const suffix = params.size > 0 ? `?${params.toString()}` : ''
    return request<RunDetail>(`${API_PREFIX}/run${suffix}`, {
      method: 'POST',
      timeoutMs: 10 * 60 * 1000,
    })
  },
  runNowStream(scheduleId?: number, runType?: 'analysis' | 'trade') {
    const params = new URLSearchParams()
    if (typeof scheduleId === 'number') {
      params.set('schedule_id', String(scheduleId))
    }
    if (runType) {
      params.set('run_type', runType)
    }
    const suffix = params.size > 0 ? `?${params.toString()}` : ''
    return request<{ run_id: number }>(`${API_PREFIX}/run-stream${suffix}`, {
      method: 'POST',
    })
  },
  runEventsUrl(runId: number) {
    return `${API_PREFIX}/runs/${runId}/events`
  },
  listRuns(options: number | ListRunsOptions = 20) {
    const config = typeof options === 'number' ? { limit: options } : options
    const params = new URLSearchParams()
    params.set('limit', String(config.limit ?? 20))
    if (config.date) {
      params.set('date', config.date)
    }
    if (config.status) {
      params.set('status', config.status)
    }
    if (typeof config.beforeId === 'number') {
      params.set('before_id', String(config.beforeId))
    }
    return request<RunSummary[]>(`${API_PREFIX}/runs?${params.toString()}`)
  },
  listRunsPage(options: ListRunsOptions = {}) {
    const params = new URLSearchParams()
    params.set('limit', String(options.limit ?? 20))
    if (options.date) {
      params.set('date', options.date)
    }
    if (options.status) {
      params.set('status', options.status)
    }
    if (typeof options.beforeId === 'number') {
      params.set('before_id', String(options.beforeId))
    }
    return request<RunSummaryPage>(`${API_PREFIX}/runs-feed?${params.toString()}`)
  },
  getRun(runId: number) {
    return request<RunDetail>(`${API_PREFIX}/runs/${runId}`)
  },
  getRunRawToolPreview(runId: number, previewIndex: number) {
    return request<RawToolPreviewDetail>(
      `${API_PREFIX}/runs/${runId}/raw-tool-previews/${previewIndex}`,
    )
  },
  deleteRun(runId: number, force = false) {
    const suffix = force ? '?force=true' : ''
    return request<void>(`${API_PREFIX}/runs/${runId}${suffix}`, {
      method: 'DELETE',
    })
  },
  getAccount(forceRefresh = false) {
    const params = new URLSearchParams()
    if (forceRefresh) {
      params.set('force_refresh', 'true')
    }
    const suffix = params.size > 0 ? `?${params.toString()}` : ''
    return request<AccountOverview>(`${API_PREFIX}/account${suffix}`, {
      timeoutMs: 60000,
    })
  },
  chat(payload: ChatRequest) {
    return request<ChatResponse>(`${API_PREFIX}/chat`, {
      method: 'POST',
      body: JSON.stringify(payload),
      timeoutMs: 3 * 60 * 1000,
    })
  },
  chatStreamUrl() {
    return `${API_PREFIX}/chat-stream`
  },
  chatSessionStreamUrl() {
    return `${API_PREFIX}/chat/stream`
  },
  listChatSessions() {
    return request<ChatSession[]>(`${API_PREFIX}/chat/sessions`)
  },
  createChatSession(title?: string) {
    return request<ChatSession>(`${API_PREFIX}/chat/sessions`, {
      method: 'POST',
      body: JSON.stringify(title ? { title } : {}),
    })
  },
  renameChatSession(sessionId: number, title: string) {
    return request<ChatSession>(`${API_PREFIX}/chat/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify({ title }),
    })
  },
  deleteChatSession(sessionId: number) {
    return request<void>(`${API_PREFIX}/chat/sessions/${sessionId}`, {
      method: 'DELETE',
    })
  },
  getChatSessionMessages(sessionId: number, options: ListChatMessagesOptions = {}) {
    const params = new URLSearchParams()
    params.set('limit', String(options.limit ?? 50))
    if (typeof options.beforeId === 'number') {
      params.set('before_id', String(options.beforeId))
    }
    return request<ChatSessionMessagesPayload>(
      `${API_PREFIX}/chat/sessions/${sessionId}/messages?${params.toString()}`,
    )
  },
  getPersistentSession() {
    return request<PersistentSession>(`${API_PREFIX}/persistent-session`)
  },
  deletePersistentSession() {
    return request<void>(`${API_PREFIX}/persistent-session`, {
      method: 'DELETE',
    })
  },
  getPersistentSessionMessages(options: ListChatMessagesOptions = {}) {
    const params = new URLSearchParams()
    params.set('limit', String(options.limit ?? 50))
    if (typeof options.beforeId === 'number') {
      params.set('before_id', String(options.beforeId))
    }
    return request<PersistentSessionMessagesPayload>(
      `${API_PREFIX}/persistent-session/messages?${params.toString()}`,
    )
  },
  async uploadChatAttachment(file: File, sessionId?: number | null): Promise<ChatAttachment> {
    const formData = new FormData()
    formData.append('file', file)
    if (typeof sessionId === 'number') {
      formData.append('session_id', String(sessionId))
    }

    const response = await fetchWithTimeout(`${API_PREFIX}/chat/uploads`, {
      method: 'POST',
      body: formData,
      timeoutMs: 60 * 1000,
      setJsonContentType: false,
    })
    if (!response.ok) {
      throw new Error(await buildErrorMessage(response))
    }
    return (await response.json()) as ChatAttachment
  },
  async fetchChatAttachmentBlob(attachmentId: number): Promise<Blob> {
    const response = await fetchWithTimeout(`${API_PREFIX}/chat/uploads/${attachmentId}`, {
      method: 'GET',
      timeoutMs: 60 * 1000,
      setJsonContentType: false,
    })
    if (!response.ok) {
      throw new Error(await buildErrorMessage(response))
    }
    return response.blob()
  },
  chatAttachmentUrl(attachmentId: number) {
    return `${API_PREFIX}/chat/uploads/${attachmentId}`
  },
}
