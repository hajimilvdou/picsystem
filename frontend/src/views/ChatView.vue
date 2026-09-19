<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Delete, Edit, Promotion, VideoPause, Notebook } from '@element-plus/icons-vue'
import { api, streamChat } from '../api/client'
import { useAuthStore } from '../stores/auth'
import MarkdownView from '../components/MarkdownView.vue'

const auth = useAuthStore()
const conversations = ref([])
const activeId = ref(null)
const messages = ref([])
const input = ref('')
const model = ref('auto')
const chatModels = ref(['auto'])
const sending = ref(false)
const streamingText = ref('')
const listRef = ref(null)
const convDrawer = ref(false)
const isMobile = ref(false)
let abortController = null

function checkMobile() {
  isMobile.value = window.innerWidth <= 768
}
onMounted(() => {
  checkMobile()
  window.addEventListener('resize', checkMobile)
})
onUnmounted(() => window.removeEventListener('resize', checkMobile))

const activeConv = computed(() => conversations.value.find((c) => c.id === activeId.value))

async function loadConversations() {
  const data = await api.get('/api/chat/conversations')
  conversations.value = data.items
}

async function openConversation(id) {
  if (sending.value) return
  activeId.value = id
  const data = await api.get(`/api/chat/conversations/${id}`)
  messages.value = data.messages.map((m) => ({ role: m.role, content: m.content }))
  if (data.model) model.value = data.model
  scrollBottom()
}

function newChat() {
  if (sending.value) return
  activeId.value = null
  messages.value = []
}

async function renameConversation(conv) {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的对话标题', '重命名', {
      inputValue: conv.title,
      inputValidator: (v) => (v && v.trim() ? true : '标题不能为空'),
    })
    await api.patch(`/api/chat/conversations/${conv.id}`, { title: value.trim() })
    conv.title = value.trim()
  } catch {
    /* 取消 */
  }
}

async function removeConversation(conv) {
  try {
    await ElMessageBox.confirm(`确定删除对话「${conv.title}」吗？`, '删除对话', { type: 'warning' })
  } catch {
    return
  }
  await api.del(`/api/chat/conversations/${conv.id}`)
  conversations.value = conversations.value.filter((c) => c.id !== conv.id)
  if (activeId.value === conv.id) newChat()
}

async function loadModels() {
  try {
    const data = await api.get('/api/models')
    const ids = (data.data || []).map((m) => m.id).filter(Boolean)
    const chat = ids.filter((id) => !/image|dall|flux|sd/i.test(id))
    if (chat.length) chatModels.value = chat
    if (!chatModels.value.includes(model.value)) model.value = chatModels.value[0] || 'auto'
  } catch {
    /* 使用默认模型 */
  }
}

function scrollBottom() {
  nextTick(() => {
    if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
  })
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  sending.value = true
  streamingText.value = ''
  messages.value.push({ role: 'user', content: text })
  messages.value.push({ role: 'assistant', content: '', streaming: true })
  input.value = ''
  scrollBottom()

  abortController = new AbortController()
  let failed = false
  try {
    await streamChat({
      body: { conversation_id: activeId.value, model: model.value, message: text },
      signal: abortController.signal,
      onMeta(data) {
        if (!activeId.value) {
          activeId.value = data.conversation_id
          loadConversations()
        }
      },
      onDelta(chunk) {
        const last = messages.value[messages.value.length - 1]
        last.content += chunk
        scrollBottom()
      },
      onError(detail) {
        failed = true
        messages.value.pop()
        ElMessage.error(detail || '生成失败，请稍后再试')
      },
      onDone() {},
    })
  } catch (e) {
    if (e.name !== 'AbortError') {
      failed = true
      ElMessage.error(e.message)
    } else {
      failed = true
      ElMessage.info('已停止生成')
    }
  } finally {
    const last = messages.value[messages.value.length - 1]
    if (last?.streaming && (!last.content || failed)) messages.value.pop()
    else if (last) delete last.streaming
    streamingText.value = ''
    sending.value = false
    abortController = null
    auth.fetchMe(true) // 刷新额度显示
    loadConversations()
  }
}

function stop() {
  abortController?.abort()
}

onMounted(() => {
  loadConversations()
  loadModels()
})
</script>

<template>
  <div class="chat-wrap">
    <div v-if="!isMobile" class="conv-aside">
      <el-button type="primary" :icon="Plus" style="width: 100%" @click="newChat">新对话</el-button>
      <div class="conv-list">
        <div
          v-for="c in conversations"
          :key="c.id"
          class="conv-item"
          :class="{ active: c.id === activeId }"
          @click="openConversation(c.id)"
        >
          <span class="conv-title">{{ c.title }}</span>
          <span class="conv-actions" @click.stop>
            <el-icon title="重命名" @click="renameConversation(c)"><Edit /></el-icon>
            <el-icon title="删除" @click="removeConversation(c)"><Delete /></el-icon>
          </span>
        </div>
        <el-empty v-if="!conversations.length" description="暂无对话" :image-size="60" />
      </div>
    </div>

    <el-drawer v-model="convDrawer" direction="ltr" size="260px" title="会话列表">
      <el-button type="primary" :icon="Plus" style="width: 100%; margin-bottom: 10px" @click="newChat(); convDrawer = false">新对话</el-button>
      <div
        v-for="c in conversations"
        :key="c.id"
        class="conv-item"
        :class="{ active: c.id === activeId }"
        @click="openConversation(c.id); convDrawer = false"
      >
        <span class="conv-title">{{ c.title }}</span>
        <span class="conv-actions" @click.stop>
          <el-icon title="重命名" @click="renameConversation(c)"><Edit /></el-icon>
          <el-icon title="删除" @click="removeConversation(c)"><Delete /></el-icon>
        </span>
      </div>
      <el-empty v-if="!conversations.length" description="暂无对话" :image-size="60" />
    </el-drawer>

    <div class="chat-main">
      <div ref="listRef" class="msg-list">
        <el-empty v-if="!messages.length" description="开始新的对话吧" />
        <div v-for="(m, i) in messages" :key="i" class="msg-row" :class="m.role">
          <div class="msg-bubble">
            <div class="msg-role">{{ m.role === 'user' ? '我' : 'AI' }}</div>
            <MarkdownView v-if="m.role !== 'user'" :content="m.content || (m.streaming ? '…' : '')" />
            <div v-else class="msg-text">{{ m.content }}</div>
          </div>
        </div>
      </div>

      <div class="input-area">
        <el-button v-if="isMobile" text :icon="Notebook" @click="convDrawer = true" />
        <el-select v-model="model" size="default" :style="isMobile ? 'width: 110px' : 'width: 180px'" :disabled="sending">
          <el-option v-for="m in chatModels" :key="m" :label="m" :value="m" />
        </el-select>
        <el-input
          v-model="input"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 6 }"
          placeholder="输入消息，Enter 发送，Shift+Enter 换行"
          @keydown.enter.exact.prevent="send"
        />
        <el-button v-if="sending" type="danger" :icon="VideoPause" circle @click="stop" />
        <el-button v-else type="primary" :icon="Promotion" circle :disabled="!input.trim()" @click="send" />
      </div>
      <div class="text-muted" style="text-align: center; padding-bottom: 8px">
        对话剩余次数：{{ auth.quotaText('chat') }} · 内容由 AI 生成，请注意甄别
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat-wrap {
  display: flex;
  height: calc(100vh - 0px);
}
.conv-aside {
  width: 240px;
  border-right: 1px solid var(--ps-border);
  background: var(--ps-surface);
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.conv-list {
  flex: 1;
  overflow-y: auto;
}
.conv-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 10px;
  border-radius: 10px;
  cursor: pointer;
  margin-bottom: 4px;
  font-size: 14px;
  color: #6b5d4c;
  transition: background 0.18s ease, color 0.18s ease;
}
.conv-item:hover {
  background: rgba(176, 124, 79, 0.08);
}
.conv-item.active {
  background: linear-gradient(120deg, rgba(176, 124, 79, 0.16), rgba(176, 124, 79, 0.08));
  color: var(--ps-primary-deep);
  font-weight: 600;
}
.conv-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.conv-actions {
  display: none;
  gap: 6px;
  margin-left: 6px;
}
.conv-item:hover .conv-actions {
  display: inline-flex;
}
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.msg-list {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
}
.msg-row {
  display: flex;
  margin-bottom: 18px;
}
.msg-row.user {
  justify-content: flex-end;
}
.msg-bubble {
  max-width: 78%;
  background: var(--ps-surface);
  border: 1px solid var(--ps-border);
  border-radius: 14px;
  padding: 12px 16px;
  box-shadow: 0 1px 3px rgba(120, 96, 64, 0.06);
}
.msg-row.user .msg-bubble {
  background: linear-gradient(135deg, #f0dfc8, #ead3b8);
  border-color: #e2cbae;
}
.msg-role {
  font-size: 12px;
  color: var(--ps-text-2);
  margin-bottom: 6px;
}
.msg-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.7;
}
.input-area {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  padding: 12px 24px 8px;
  border-top: 1px solid var(--ps-border);
  background: var(--ps-surface);
}
.input-area :deep(.el-textarea__inner) {
  border-radius: 10px;
}
</style>
