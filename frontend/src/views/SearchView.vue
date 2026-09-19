<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
import MarkdownView from '../components/MarkdownView.vue'

const auth = useAuthStore()
const prompt = ref('')
const loading = ref(false)
const records = ref([]) // {prompt, answer, sources, time}

async function doSearch() {
  const q = prompt.value.trim()
  if (!q || loading.value) return
  loading.value = true
  try {
    const data = await api.post('/api/search', { prompt: q })
    const sources = Array.isArray(data.sources) ? data.sources : []
    records.value.unshift({
      prompt: q,
      answer: data.answer || data.content || data.text || JSON.stringify(data, null, 2),
      sources,
      time: new Date().toLocaleTimeString(),
    })
    auth.fetchMe(true)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function sourceUrl(s) {
  const u = typeof s === 'string' ? s : s.url || s.link || ''
  // 仅放行 http(s) 链接，防 javascript: 等伪协议
  return /^https?:\/\//i.test(u) ? u : ''
}
function sourceTitle(s, i) {
  return typeof s === 'string' ? s : s.title || s.name || s.url || `来源 ${i + 1}`
}
</script>

<template>
  <div class="page" style="max-width: 860px">
    <h2 class="page-title">联网搜索</h2>
    <div style="display: flex; gap: 10px; margin-bottom: 20px">
      <el-input
        v-model="prompt"
        size="large"
        placeholder="输入你想了解的问题，回车搜索"
        clearable
        @keyup.enter="doSearch"
      />
      <el-button type="primary" size="large" :icon="Search" :loading="loading" @click="doSearch">搜索</el-button>
    </div>
    <div class="text-muted" style="margin-bottom: 16px">搜索剩余次数：{{ auth.quotaText('search') }}</div>

    <el-empty v-if="!records.length" description="搜索结果将在这里展示" />
    <el-card v-for="(r, i) in records" :key="i" style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between">
          <strong>{{ r.prompt }}</strong>
          <span class="text-muted">{{ r.time }}</span>
        </div>
      </template>
      <MarkdownView :content="r.answer" />
      <template v-if="r.sources.length">
        <el-divider content-position="left">引用来源</el-divider>
        <ol style="margin: 0; padding-left: 20px">
          <li v-for="(s, j) in r.sources" :key="j" style="margin-bottom: 4px">
            <a v-if="sourceUrl(s)" :href="sourceUrl(s)" target="_blank" rel="noopener noreferrer">{{ sourceTitle(s, j) }}</a>
            <span v-else>{{ sourceTitle(s, j) }}</span>
          </li>
        </ol>
      </template>
    </el-card>
  </div>
</template>
