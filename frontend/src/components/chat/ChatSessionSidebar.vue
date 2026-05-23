<template>
  <aside class="chat-session-sidebar">
    <div class="chat-session-search-row">
      <button
        type="button"
        class="chat-new-button"
        :disabled="creating"
        title="新建对话"
        @click="handleNew"
      >
        <svg class="chat-new-button-icon" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 7v10M7 12h10" />
        </svg>
      </button>

      <div class="chat-session-search">
        <input
          v-model="query"
          type="search"
          placeholder="搜索对话"
          class="chat-session-search-input"
        />
      </div>
    </div>

    <div v-if="loading && !sessions.length && !persistentSession" class="chat-session-empty">加载中…</div>

    <div class="chat-session-groups">
      <section class="chat-session-group persistent-session-group">
        <h4 class="chat-session-group-title">持久会话</h4>
        <ul class="chat-session-list">
          <li
            class="chat-session-item persistent-session-item"
            :class="{ 'is-active': persistentSelected }"
            @click="$emit('selectPersistent')"
          >
            <div class="chat-session-item-body">
              <span class="chat-session-title">持久会话</span>
              <span class="chat-session-meta">{{ persistentMeta }}</span>
            </div>
            <div class="chat-session-actions" @click.stop>
              <button
                type="button"
                class="chat-session-action danger"
                title="删除持久会话"
                @click="handleDeletePersistent"
              >
                删除
              </button>
            </div>
          </li>
        </ul>
      </section>

      <div v-if="!groupedSessions.length" class="chat-session-empty">
        点击左侧 + 号新建对话
      </div>

      <section v-for="group in groupedSessions" :key="group.label" class="chat-session-group">
        <h4 class="chat-session-group-title">{{ group.label }}</h4>
        <ul class="chat-session-list">
          <li
            v-for="session in group.sessions"
            :key="session.id"
            class="chat-session-item"
            :class="{ 'is-active': session.id === currentSessionId }"
            @click="$emit('select', session.id)"
          >
            <div class="chat-session-item-body">
              <span class="chat-session-title">{{ session.title || '新对话' }}</span>
              <span class="chat-session-meta">{{ formatTime(session.last_message_at ?? session.updated_at) }}</span>
            </div>
            <div class="chat-session-actions" @click.stop>
              <button
                type="button"
                class="chat-session-action danger"
                title="删除"
                @click="handleDelete(session)"
              >
                删除
              </button>
            </div>
          </li>
        </ul>
      </section>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import type { ChatSession, PersistentSession } from '@/types'
import { formatChatSessionTime, getBeijingDayDifference } from '@/utils/formatters'

const props = defineProps<{
  sessions: ChatSession[]
  persistentSession: PersistentSession | null
  persistentSelected: boolean
  currentSessionId: number | null
  loading: boolean
}>()

const emit = defineEmits<{
  (e: 'select', sessionId: number): void
  (e: 'selectPersistent'): void
  (e: 'create'): void
  (e: 'delete', sessionId: number): void
  (e: 'deletePersistent'): void
}>()

const query = ref('')
const creating = ref(false)

function handleNew() {
  if (creating.value) return
  creating.value = true
  emit('create')
  window.setTimeout(() => {
    creating.value = false
  }, 500)
}

function handleDelete(session: ChatSession) {
  const confirmed = window.confirm(`确定删除对话“${session.title || '新对话'}”吗？`)
  if (!confirmed) return
  emit('delete', session.id)
}

function handleDeletePersistent() {
  const confirmed = window.confirm('确定删除持久会话中的全部上下文吗？')
  if (!confirmed) return
  emit('deletePersistent')
}

function formatTime(value: string | null): string {
  return formatChatSessionTime(value)
}

const persistentMeta = computed(() => {
  const session = props.persistentSession
  if (!session) return '查看自动化持续上下文'
  return formatTime(session.last_message_at ?? session.updated_at)
})

interface SessionGroup {
  label: string
  sessions: ChatSession[]
}

const filteredSessions = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return props.sessions
  return props.sessions.filter((item) => (item.title || '').toLowerCase().includes(q))
})

const groupedSessions = computed<SessionGroup[]>(() => {
  const list = filteredSessions.value
  if (!list.length) return []

  const today: ChatSession[] = []
  const yesterday: ChatSession[] = []
  const week: ChatSession[] = []
  const older: ChatSession[] = []

  for (const session of list) {
    const dayDifference = getBeijingDayDifference(session.last_message_at ?? session.updated_at)

    if (dayDifference === null) {
      older.push(session)
    } else if (dayDifference <= 0) {
      today.push(session)
    } else if (dayDifference === 1) {
      yesterday.push(session)
    } else if (dayDifference < 7) {
      week.push(session)
    } else {
      older.push(session)
    }
  }

  const groups: SessionGroup[] = []
  if (today.length) groups.push({ label: '今天', sessions: today })
  if (yesterday.length) groups.push({ label: '昨天', sessions: yesterday })
  if (week.length) groups.push({ label: '7 天内', sessions: week })
  if (older.length) groups.push({ label: '更早', sessions: older })
  return groups
})
</script>
