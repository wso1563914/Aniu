<template>
  <div class="tab-content">
    <section class="panel chat-panel">
      <div class="panel-head">
        <div class="head-main">
          <h2>AI 聊天</h2>
          <p class="section-kicker">AI Chat</p>
        </div>
      </div>

      <div class="chat-workspace">
        <ChatSessionSidebar
          :sessions="sessions"
          :persistent-session="persistentSession"
          :persistent-selected="persistentSelected"
          :current-session-id="currentSessionId"
          :loading="sessionsLoading"
          @select="handleSelect"
          @select-persistent="handleSelectPersistent"
          @create="handleCreate"
          @delete="handleDelete"
          @delete-persistent="handleDeletePersistent"
        />

        <ChatConversation
          :session="persistentSelected ? persistentSession : currentSession"
          :messages="persistentSelected ? persistentMessages : messages"
          v-model="input"
          :pending-attachments="pendingAttachments"
          :sending="sending"
          :loading="persistentSelected ? persistentLoading : loading"
          :loading-older-messages="persistentSelected ? persistentLoadingOlder : loadingOlderMessages"
          :has-more-messages="persistentSelected ? persistentHasMoreMessages : hasMoreMessages"
          :can-send="persistentSelected ? false : canSend"
          :error-message="persistentSelected ? persistentErrorMessage : errorMessage"
          :read-only="persistentSelected"
          :ensure-session-ready="ensureSessionReady"
          :load-older-messages="persistentSelected ? loadOlderPersistentMessages : loadOlderMessages"
          @submit="handleSubmit"
          @attach="addAttachment"
          @remove-attachment="removeAttachment"
          @upload-error="handleUploadError"
        />
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import ChatConversation from '@/components/chat/ChatConversation.vue'
import ChatSessionSidebar from '@/components/chat/ChatSessionSidebar.vue'
import { useChatSession } from '@/composables/useChatSession'
import { useChatSessions } from '@/composables/useChatSessions'
import { usePersistentSession } from '@/composables/usePersistentSession'
import { useRunStream } from '@/composables/useRunStream'

const {
  sessions,
  currentSession,
  currentSessionId,
  loading: sessionsLoading,
  loadSessions,
  createSession,
  deleteSession,
  selectSession,
  touchSession,
} = useChatSessions()

const {
  messages,
  input,
  pendingAttachments,
  sending,
  loading,
  loadingOlderMessages,
  errorMessage,
  canSend,
  activeSessionId,
  hasMoreMessages,
  loadSession,
  loadOlderMessages,
  sendMessage,
  addAttachment,
  removeAttachment,
} = useChatSession()

const {
  session: persistentSession,
  messages: persistentMessages,
  loading: persistentLoading,
  loadingOlderMessages: persistentLoadingOlder,
  errorMessage: persistentErrorMessage,
  hasMoreMessages: persistentHasMoreMessages,
  loadSession: loadPersistentSession,
  loadOlderMessages: loadOlderPersistentMessages,
  refreshSummaryOnly: refreshPersistentSummaryOnly,
  deleteSession: deletePersistentSession,
  appendSystemMessage: appendPersistentSystemMessage,
  clear: clearPersistentSession,
} = usePersistentSession()

const runStream = useRunStream()

const skipNextSessionLoad = ref(false)
const persistentSelected = ref(false)
const DEFAULT_SESSION_TITLE = '\u65b0\u5bf9\u8bdd'
const DEFAULT_SESSION_TITLES = new Set([DEFAULT_SESSION_TITLE, '\u65b0\u4f1a\u8bdd'])

function deriveSessionTitle(currentTitle: string | undefined, content: string): string {
  const normalizedTitle = currentTitle?.trim() ?? ''
  if (normalizedTitle && !DEFAULT_SESSION_TITLES.has(normalizedTitle)) {
    return normalizedTitle
  }

  const firstLine = content.trim().split(/\r?\n/u)[0]?.trim() ?? ''
  return firstLine.slice(0, 30) || DEFAULT_SESSION_TITLE
}

async function restoreCurrentSession(forceReload = false) {
  await loadSessions()

  const currentId = currentSessionId.value
  const hasCurrentSession = currentId !== null && sessions.value.some((item) => item.id === currentId)
  const nextSessionId = hasCurrentSession ? currentId : (sessions.value[0]?.id ?? null)

  if (nextSessionId !== currentSessionId.value) {
    selectSession(nextSessionId)
    return
  }

  activeSessionId.value = nextSessionId
  if (forceReload || nextSessionId === null) {
    await loadSession(nextSessionId)
  }
}

onMounted(async () => {
  await Promise.all([
    restoreCurrentSession(true),
    refreshPersistentSummaryOnly(),
  ])
})

const disposeRunStreamListener = runStream.onEvent((event) => {
  if (event.type !== 'context_compacted') {
    return
  }
  const content = String(event.content || '').trim()
  if (!content) {
    return
  }
  appendPersistentSystemMessage(content, new Date().toISOString())
})

watch(currentSessionId, async (sessionId) => {
  if (persistentSelected.value) {
    return
  }
  activeSessionId.value = sessionId
  if (skipNextSessionLoad.value && sessionId !== null) {
    skipNextSessionLoad.value = false
    return
  }
  await loadSession(sessionId)
})

function handleSelect(sessionId: number) {
  persistentSelected.value = false
  selectSession(sessionId)
}

async function handleSelectPersistent() {
  persistentSelected.value = true
  activeSessionId.value = null
  await loadPersistentSession()
}

async function ensureSessionReady(): Promise<number | null> {
  if (currentSessionId.value !== null) {
    return currentSessionId.value
  }

  try {
    skipNextSessionLoad.value = true
    const created = await createSession()
    activeSessionId.value = created.id
    return created.id
  } catch (error) {
    skipNextSessionLoad.value = false
    errorMessage.value = (error as Error).message
    return null
  }
}

async function handleCreate() {
  persistentSelected.value = false
  try {
    const created = await createSession()
    activeSessionId.value = created.id
  } catch (error) {
    errorMessage.value = (error as Error).message
  }
}

async function handleDelete(sessionId: number) {
  try {
    await deleteSession(sessionId)
    if (currentSessionId.value === null) {
      await loadSession(null)
    }
  } catch (error) {
    errorMessage.value = (error as Error).message
  }
}

async function handleDeletePersistent() {
  try {
    await deletePersistentSession()
  } catch (error) {
    persistentErrorMessage.value = (error as Error).message
  }
}

async function handleSubmit() {
  const sessionId = await ensureSessionReady()
  if (sessionId === null) {
    return
  }

  const submittedContent = input.value.trim()
  const currentTitle = currentSession.value?.title
  const currentMessageCount = currentSession.value?.message_count ?? 0
  const result = await sendMessage()
  if (result) {
    touchSession(result.sessionId, {
      title: deriveSessionTitle(currentTitle, submittedContent),
      message_count: currentMessageCount + 2,
    })
  }
}

function handleUploadError(message: string) {
  errorMessage.value = message
}

watch(persistentSelected, (selected) => {
  if (!selected) {
    clearPersistentSession()
  }
})

onBeforeUnmount(() => {
  disposeRunStreamListener()
})
</script>
