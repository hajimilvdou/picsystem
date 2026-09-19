<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Download, Refresh } from '@element-plus/icons-vue'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { fmtTime } from '../utils/format'

const auth = useAuthStore()
const kind = ref('ppt')
const prompt = ref('')
const creating = ref(false)
const tasks = ref([])
let pollTimer = null

const STATUS = {
  queued: { label: '排队中', type: 'info' },
  running: { label: '生成中', type: 'primary' },
  success: { label: '已完成', type: 'success' },
  error: { label: '失败', type: 'danger' },
}

async function loadTasks() {
  const data = await api.get('/api/ppt/tasks')
  tasks.value = data.items
  schedulePoll()
}

function schedulePoll() {
  clearTimeout(pollTimer)
  if (tasks.value.some((t) => t.status === 'queued' || t.status === 'running')) {
    pollTimer = setTimeout(refreshActive, 4000)
  }
}

async function refreshActive() {
  const active = tasks.value.filter((t) => t.status === 'queued' || t.status === 'running')
  for (const t of active) {
    try {
      const data = await api.get(`/api/ppt/tasks/${t.id}`)
      Object.assign(t, data)
    } catch {
      /* 下次再试 */
    }
  }
  auth.fetchMe(true)
  schedulePoll()
}

async function create() {
  if (!prompt.value.trim()) {
    ElMessage.warning('请描述需要生成的内容')
    return
  }
  creating.value = true
  try {
    await api.post('/api/ppt/generations', { prompt: prompt.value.trim(), kind: kind.value })
    ElMessage.success('任务已创建，生成通常需要几分钟')
    prompt.value = ''
    auth.fetchMe(true)
    await loadTasks()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    creating.value = false
  }
}

onMounted(loadTasks)
onUnmounted(() => clearTimeout(pollTimer))
</script>

<template>
  <div class="page" style="max-width: 900px">
    <h2 class="page-title">PPT / PSD 生成</h2>
    <el-card style="margin-bottom: 16px">
      <el-tabs v-model="kind">
        <el-tab-pane label="PPT 演示文稿" name="ppt" />
        <el-tab-pane label="PSD 设计稿" name="psd" />
      </el-tabs>
      <el-input
        v-model="prompt"
        type="textarea"
        :rows="3"
        maxlength="4000"
        show-word-limit
        :placeholder="kind === 'ppt' ? '描述 PPT 的主题、大纲与风格，例如：面向大学生的时间管理讲座，8 页，简洁商务风' : '描述设计稿内容'"
      />
      <div style="display: flex; align-items: center; gap: 12px; margin-top: 12px">
        <el-button type="primary" :loading="creating" @click="create">创建任务</el-button>
        <span class="text-muted">PPT/PSD 剩余次数：{{ auth.quotaText('ppt') }}</span>
      </div>
    </el-card>

    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>我的任务</span>
          <el-button size="small" text :icon="Refresh" @click="loadTasks">刷新</el-button>
        </div>
      </template>
      <el-empty v-if="!tasks.length" description="暂无任务" />
      <el-table v-else :data="tasks">
        <el-table-column label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.kind.toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="内容" min-width="240">
          <template #default="{ row }">
            <el-tooltip :content="row.prompt">
              <span style="display: inline-block; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">
                {{ row.prompt }}
              </span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="STATUS[row.status]?.type || 'info'" size="small">
              {{ STATUS[row.status]?.label || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'success' && row.download_url"
              size="small"
              type="primary"
              :icon="Download"
              tag="a"
              :href="row.download_url"
              download
            >
              下载
            </el-button>
            <el-tooltip v-else-if="row.status === 'error'" :content="row.error">
              <span class="text-muted">查看原因</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
