<script setup>
import { nextTick, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
import MarkdownView from './MarkdownView.vue'

const auth = useAuthStore()
const visible = ref(false)
const loading = ref(false)
const loadError = ref('')
const accepting = ref(false)
const canAccept = ref(false)
const text = ref('')
const version = ref(1)
const scrollRef = ref(null)

watch(
  () => auth.needAgreement && auth.user?.status === 'active',
  async (need) => {
    if (need) {
      visible.value = true
      await loadAgreement()
    } else {
      visible.value = false
    }
  },
  { immediate: true }
)

async function loadAgreement() {
  canAccept.value = false
  loadError.value = ''
  loading.value = true
  try {
    const data = await api.get('/api/auth/agreement')
    text.value = data.text
    version.value = data.version
    await nextTick()
    checkScroll() // 内容不足一屏时直接可点
  } catch (e) {
    loadError.value = e.message
  } finally {
    loading.value = false
  }
}

function checkScroll() {
  const el = scrollRef.value
  if (!el) return
  if (el.scrollHeight - el.scrollTop - el.clientHeight <= 12) {
    canAccept.value = true
  }
}

async function accept() {
  accepting.value = true
  try {
    await api.post('/api/auth/agreement/accept')
    ElMessage.success('感谢阅读，已记录你的同意')
    await auth.fetchMe(true)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    accepting.value = false
  }
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="用户使用协议与免责声明"
    width="min(640px, 94vw)"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    :show-close="false"
    align-center
  >
    <div class="agreement-tip">请完整阅读以下协议（滚动到底部后按钮可用）</div>
    <div v-if="loadError" style="text-align: center; padding: 40px 0">
      <el-alert type="error" :title="`加载协议失败：${loadError}`" :closable="false" style="margin-bottom: 12px" />
      <el-button type="primary" @click="loadAgreement">重试</el-button>
    </div>
    <div v-show="!loadError" ref="scrollRef" class="agreement-body" v-loading="loading" @scroll="checkScroll">
      <MarkdownView :content="text" />
    </div>
    <template #footer>
      <el-button type="primary" :disabled="!canAccept" :loading="accepting" @click="accept">
        {{ canAccept ? '我已阅读并同意' : '请先滚动阅读到底部' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.agreement-tip {
  color: var(--ps-text-2);
  font-size: 13px;
  margin-bottom: 8px;
}
.agreement-body {
  height: 52vh;
  overflow-y: auto;
  border: 1px solid var(--ps-border);
  border-radius: 10px;
  padding: 16px;
  background: #faf6ef;
}
</style>
