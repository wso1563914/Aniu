import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'

import { api } from '@/services/api'
import type { AccountOverview, AppSettings, RunDetail, ScheduleConfig } from '@/types'

const ACCOUNT_REFRESH_COOLDOWN_MS = 60 * 60 * 1000
const ACCOUNT_REFRESH_STORAGE_KEY = 'aniu-account-last-refresh-at'

/** Generate a unique-enough id that works in non-secure contexts (HTTP). */
function uid(): string {
  // crypto.randomUUID is only available in Secure Contexts (HTTPS).
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`
}

interface ScheduleOverviewItem {
  id: number
  name: string
  category: string
  cronExpression: string
  displayTime: string
  nextRunAt: string | null
  lastRunAt: string | null
}

type SettingsPayload = Omit<AppSettings, 'id' | 'created_at' | 'updated_at'>
type ScheduleEditor = Omit<ScheduleConfig, 'created_at' | 'updated_at'> & { local_id: string }

const defaultSettings = (): SettingsPayload => ({
  provider_name: 'openai-compatible',
  mx_api_key: '',
  llm_base_url: '',
  llm_api_key: '',
  llm_model: 'gpt-4o-mini',
  automation_context_window_tokens: 128000,
  system_prompt: '你是跨越完整牛熊周期的顶尖私募投资机构老将与极度理性的专业交易员，你深谙A股政策驱动、外资流动与资金博弈机制。你必须持续运行以下自我驱动循环，监控经济、政策、盘面数据及资金流向，研判周期位置与市场情绪，寻找共识与预期差，定性博弈逻辑，自主决策执行交易操作。你的唯一目标是追求收益最大化。',
})

const defaultAccount = (): AccountOverview => ({
  open_date: null,
  daily_profit_trade_date: null,
  operating_days: null,
  initial_capital: null,
  total_assets: null,
  total_market_value: null,
  cash_balance: null,
  total_position_ratio: null,
  holding_profit: null,
  total_return_ratio: null,
  nav: null,
  daily_profit: null,
  daily_return_ratio: null,
  positions: [],
  orders: [],
  trade_summaries: [],
  errors: [],
})

function createScheduleDraft(): ScheduleEditor {
  return {
    id: 0,
    local_id: uid(),
    name: '默认任务',
    run_type: 'analysis',
    cron_expression: '*/30 * * * *',
    task_prompt: '请根据当前市场和持仓情况生成交易决策。',
    timeout_seconds: 1800,
    enabled: false,
    last_run_at: null,
    next_run_at: null,
  }
}

function readLastAccountRefreshAt() {
  if (typeof window === 'undefined') {
    return 0
  }

  const raw = window.localStorage.getItem(ACCOUNT_REFRESH_STORAGE_KEY)
  const numeric = Number(raw)
  return Number.isFinite(numeric) && numeric > 0 ? numeric : 0
}

function persistLastAccountRefreshAt(value: number) {
  if (typeof window === 'undefined') {
    return
  }

  window.localStorage.setItem(ACCOUNT_REFRESH_STORAGE_KEY, String(value))
}

function formatCooldownDuration(ms: number) {
  const totalMinutes = Math.ceil(Math.max(ms, 0) / (60 * 1000))
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  if (hours > 0 && minutes > 0) {
    return `${hours}小时${minutes}分钟`
  }
  if (hours > 0) {
    return `${hours}小时`
  }
  return `${Math.max(totalMinutes, 1)}分钟`
}

export const useAppStore = defineStore('app', () => {
  const settings = reactive<SettingsPayload>(defaultSettings())
  const schedules = ref<ScheduleEditor[]>([])
  const account = ref<AccountOverview>(defaultAccount())
  const runDetailsMap = ref<Record<number, RunDetail>>({})
  const accountLastManualRefreshAt = ref(readLastAccountRefreshAt())
  const accountRefreshTick = ref(Date.now())

  const busy = ref(false)
  const accountRefreshing = ref(false)
  const notice = ref('')
  const errorMessage = ref('')

  const enabledTaskCount = computed(() => schedules.value.filter((task) => task.enabled).length)
  const accountPositionCount = computed(() => account.value.positions.length)
  const activeScheduleCards = computed<ScheduleOverviewItem[]>(() => {
    const items = schedules.value
      .filter((item) => item.enabled)
      .slice()
      .map((item) => {
        const parts = (item.cron_expression || '').trim().split(/\s+/)
        const minute = Number(parts[0]) || 0
        const hour = Number(parts[1]) || 0
        const displayTime = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
        const sortKey = hour * 60 + minute
        const displayName = item.name.replace(/#(\d+)$/, '$1号')
        const category = item.run_type === 'trade' ? '交易任务' : '分析任务'
        return {
          id: item.id,
          name: displayName,
          category,
          cronExpression: item.cron_expression,
          displayTime,
          nextRunAt: item.next_run_at,
          lastRunAt: item.last_run_at,
          sortKey,
        }
      })
    items.sort((a, b) => a.sortKey - b.sortKey)
    return items.map(({ sortKey, ...rest }) => rest)
  })
  const nextScheduledTask = computed(() => {
    const cards = activeScheduleCards.value.filter((card) => !!card.nextRunAt)
    if (cards.length === 0) return null
    const sorted = [...cards].sort((a, b) => (a.nextRunAt ?? '').localeCompare(b.nextRunAt ?? ''))
    return sorted[0]
  })
  const accountRefreshRemainingMs = computed(() => {
    void accountRefreshTick.value
    if (!accountLastManualRefreshAt.value) {
      return 0
    }
    const elapsed = Date.now() - accountLastManualRefreshAt.value
    return elapsed >= ACCOUNT_REFRESH_COOLDOWN_MS ? 0 : ACCOUNT_REFRESH_COOLDOWN_MS - elapsed
  })
  const canManualRefreshAccount = computed(() => accountRefreshRemainingMs.value <= 0)
  const accountRefreshCooldownText = computed(() => formatCooldownDuration(accountRefreshRemainingMs.value))

  function applySettings(payload: AppSettings) {
    settings.provider_name = payload.provider_name
    settings.mx_api_key = payload.mx_api_key ?? ''
    settings.llm_base_url = payload.llm_base_url ?? ''
    settings.llm_api_key = payload.llm_api_key ?? ''
    settings.llm_model = payload.llm_model
    settings.automation_context_window_tokens = payload.automation_context_window_tokens ?? 128000
    settings.system_prompt = payload.system_prompt
  }

  function applySchedules(payload: ScheduleConfig[]) {
    schedules.value = payload.length
      ? payload.map((item) => ({ ...item, local_id: uid() }))
      : [createScheduleDraft()]
  }

  async function loadSettings() {
    applySettings(await api.getSettings())
  }

  async function loadSchedule() {
    applySchedules(await api.getSchedule())
  }

  async function loadRunDetail(runId: number, options?: { force?: boolean }) {
    if (!options?.force && runDetailsMap.value[runId]) {
      return runDetailsMap.value[runId]
    }
    const detail = await api.getRun(runId)
    runDetailsMap.value = {
      ...runDetailsMap.value,
      [runId]: detail,
    }
    return detail
  }

  async function refreshAfterRunCompletion() {
    const results = await Promise.allSettled([
      refreshAccountData(),
      loadSchedule(),
    ])

    const failed = results.find((result): result is PromiseRejectedResult => result.status === 'rejected')
    if (failed) {
      throw (failed.reason instanceof Error ? failed.reason : new Error('运行完成后的数据刷新失败'))
    }
  }

  async function refreshAccountData() {
    account.value = await api.getAccount(false)
  }

  async function refreshAccountDataWithCooldown() {
    if (!canManualRefreshAccount.value) {
      throw new Error(`账户信息每 1 小时只能手动刷新一次，请在 ${accountRefreshCooldownText.value}后重试。`)
    }

    accountRefreshing.value = true
    errorMessage.value = ''

    try {
      account.value = await api.getAccount(true)
      const now = Date.now()
      accountLastManualRefreshAt.value = now
      persistLastAccountRefreshAt(now)
      notice.value = '账户信息已刷新。'
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      accountRefreshing.value = false
    }
  }

  async function refreshAll() {
    busy.value = true
    errorMessage.value = ''

    try {
      const results = await Promise.allSettled([
        loadSettings(),
        loadSchedule(),
        refreshAccountData(),
      ])
      const errors = results
        .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
        .map((result) => result.reason instanceof Error ? result.reason.message : '刷新失败')

      if (errors.length === 0) {
        notice.value = '已刷新账户、任务与系统设置。'
      } else {
        errorMessage.value = errors[0]
      }
    } finally {
      busy.value = false
    }
  }

  function resetState() {
    Object.assign(settings, defaultSettings())
    schedules.value = []
    account.value = defaultAccount()
    runDetailsMap.value = {}
    accountLastManualRefreshAt.value = readLastAccountRefreshAt()
    accountRefreshTick.value = Date.now()
    busy.value = false
    accountRefreshing.value = false
    notice.value = ''
    errorMessage.value = ''
  }

  async function saveSettings() {
    busy.value = true
    errorMessage.value = ''

    try {
      const payload = await api.updateSettings({
        ...settings,
        mx_api_key: settings.mx_api_key || null,
        llm_base_url: settings.llm_base_url || null,
        llm_api_key: settings.llm_api_key || null,
      })
      applySettings(payload)
      notice.value = '系统设置已保存。'
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      busy.value = false
    }
  }

  async function saveSchedule(schedulePayload?: Array<Partial<ScheduleConfig>>) {
    busy.value = true
    errorMessage.value = ''

    try {
      const payload = await api.updateSchedule(
        schedulePayload ?? schedules.value.map(({ local_id, ...item }) => item),
      )
      applySchedules(payload)
      await loadSchedule()
      notice.value = '定时任务已保存。'
    } catch (error) {
      errorMessage.value = (error as Error).message
    } finally {
      busy.value = false
    }
  }

  async function runNow(scheduleId?: number) {
    busy.value = true
    errorMessage.value = ''

    try {
      const run = await api.runNow(scheduleId)
      notice.value = `任务运行完成：#${run.id} ${run.status}`
      await Promise.all([refreshAccountData(), loadSchedule()])
      return run
    } catch (error) {
      errorMessage.value = (error as Error).message
      throw error
    } finally {
      busy.value = false
    }
  }

  function touchAccountRefreshTick() {
    accountRefreshTick.value = Date.now()
  }

  return {
    settings,
    schedules,
    account,
    busy,
    notice,
    errorMessage,
    enabledTaskCount,
    accountPositionCount,
    activeScheduleCards,
    nextScheduledTask,
    accountRefreshing,
    canManualRefreshAccount,
    accountRefreshCooldownText,
    applySettings,
    applySchedules,
    loadSettings,
    loadSchedule,
    loadRunDetail,
    refreshAfterRunCompletion,
    refreshAccountData,
    refreshAccountDataWithCooldown,
    refreshAll,
    touchAccountRefreshTick,
    saveSettings,
    saveSchedule,
    runNow,
    resetState,
  }
})
