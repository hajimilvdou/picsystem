<script setup>
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
import MarkdownView from './MarkdownView.vue'

const auth = useAuthStore()
const visible = ref(false)
const text = ref('')
let checked = false

watch(
  () => auth.user,
  async (user) => {
    if (!user || checked) return
    checked = true
    try {
      const data = await api.get('/api/auth/notice')
      if (data.show) {
        text.value = data.text
        visible.value = true
      }
    } catch {
      /* 忽略 */
    }
  },
  { immediate: true }
)

async function ack() {
  try {
    await api.post('/api/auth/notice/ack')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    visible.value = false
  }
}
</script>

<template>
  <el-dialog v-model="visible" title="站内公告" width="min(560px, 92vw)" :close-on-click-modal="false">
    <div class="notice-body">
      <MarkdownView :content="text" />
    </div>
    <template #footer>
      <el-button type="primary" @click="ack">我知道了</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.notice-body {
  max-height: 55vh;
  overflow-y: auto;
}
</style>
