<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { Search, Refresh } from '@element-plus/icons-vue'
import { api } from '../../api/client'
import { fmtTime } from '../../utils/format'

const FEATURE_LABELS = { chat: '对话', image: '绘图', search: '搜索', ppt: 'PPT/PSD' }

const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const filters = ref({ username: '', feature: '', status: '' })
const autoRefresh = ref(false)
let timer = null

async function load() {
  loading.value = true
  try {
    const p = new URLSearchParams({ page: String(page.value), size: '20' })
    for (const [k, v] of Object.entries(filters.value)) if (v) p.set(k, v)
    const data = await api.get(`/api/admin/logs?${p}`)
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function toggleAuto(v) {
  clearInterval(timer)
  if (v) timer = setInterval(load, 10000)
}

onMounted(load)
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page">
    <h2 class="page-title">调用日志</h2>
    <div style="display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap">
      <el-input v-model="filters.username" placeholder="用户名" style="width: 160px" clearable @keyup.enter="page = 1; load()" />
      <el-select v-model="filters.feature" placeholder="功能" style="width: 130px" clearable>
        <el-option v-for="(l, f) in FEATURE_LABELS" :key="f" :label="l" :value="f" />
      </el-select>
      <el-select v-model="filters.status" placeholder="状态" style="width: 110px" clearable>
        <el-option label="成功" value="success" />
        <el-option label="失败" value="failed" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="page = 1; load()">筛选</el-button>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
      <el-checkbox v-model="autoRefresh" @change="toggleAuto" style="margin-left: auto">每 10 秒自动刷新</el-checkbox>
    </div>

    <el-card>
      <el-table :data="items" v-loading="loading" size="small">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="username" label="用户" width="110" show-overflow-tooltip />
        <el-table-column label="功能" width="90">
          <template #default="{ row }">{{ row.feature_label }}</template>
        </el-table-column>
        <el-table-column prop="model" label="模型/类型" min-width="110" show-overflow-tooltip />
        <el-table-column prop="endpoint" label="接口" min-width="180" show-overflow-tooltip />
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
              {{ row.status === 'success' ? '成功' : '失败' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="次数" width="70">
          <template #default="{ row }">{{ row.units }}</template>
        </el-table-column>
        <el-table-column label="Token (入/出)" width="110">
          <template #default="{ row }">
            <span v-if="row.prompt_tokens != null">{{ row.prompt_tokens }} / {{ row.completion_tokens ?? '-' }}</span>
            <span v-else class="text-muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="90">
          <template #default="{ row }">{{ (row.latency_ms / 1000).toFixed(1) }}s</template>
        </el-table-column>
        <el-table-column prop="ip" label="IP" width="120" show-overflow-tooltip />
        <el-table-column label="错误" min-width="140">
          <template #default="{ row }">
            <el-tooltip v-if="row.error" :content="row.error">
              <span style="color: var(--el-color-danger); font-size: 12px">{{ row.error.slice(0, 40) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-if="total > 20"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="20"
        :current-page="page"
        style="margin-top: 12px; justify-content: center"
        @current-change="(p) => { page = p; load() }"
      />
    </el-card>
  </div>
</template>
