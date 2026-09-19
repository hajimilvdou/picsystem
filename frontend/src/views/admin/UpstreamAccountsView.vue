<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import { api } from '../../api/client'

const items = ref([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const statusFilter = ref('all')
const loading = ref(false)
const loadError = ref('')
const addVisible = ref(false)
const addText = ref('')
const addLoading = ref(false)
const syncing = ref(false)
let pollTimer = null

const STATUS_OPTIONS = [
  { label: '全部', value: 'all' },
  { label: '正常', value: 'normal' },
  { label: '限流', value: 'limited' },
  { label: '异常', value: 'abnormal' },
  { label: '禁用', value: 'disabled' },
]

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const data = await api.get(
      `/api/admin/upstream-accounts?page=${page.value}&page_size=50&keyword=${encodeURIComponent(keyword.value)}&status=${statusFilter.value}`
    )
    items.value = data.items || []
    total.value = data.total || 0
  } catch (e) {
    loadError.value = e.message
  } finally {
    loading.value = false
  }
}

async function addAccounts() {
  const tokens = addText.value.split('\n').map((s) => s.trim()).filter(Boolean)
  if (!tokens.length) {
    ElMessage.warning('请粘贴至少一个 Access Token（每行一个）')
    return
  }
  addLoading.value = true
  try {
    const data = await api.post('/api/admin/upstream-accounts', { tokens, sync_after_import: true })
    const errs = (data.errors || []).length
    ElMessage.success(`添加完成：成功 ${data.added ?? 0}，跳过 ${data.skipped ?? 0}${errs ? `，失败 ${errs}` : ''}`)
    addVisible.value = false
    addText.value = ''
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    addLoading.value = false
  }
}

async function batch(row, operation) {
  const label = operation === 'enable' ? '启用' : '禁用'
  try {
    const data = await api.post('/api/admin/upstream-accounts/batch', {
      account_ids: [row.id],
      operation,
    })
    if (data.progress_id) pollProgress(data.progress_id, `${label}完成`)
    else load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(`确定删除账号「${row.display_name || row.email || row.id}」吗？`, '删除账号', { type: 'warning' })
  } catch {
    return
  }
  try {
    const data = await api.post('/api/admin/upstream-accounts/delete', { account_ids: [row.id] })
    if (data.progress_id) pollProgress(data.progress_id, '删除完成')
    else load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function syncAll() {
  syncing.value = true
  try {
    const data = await api.post('/api/admin/upstream-accounts/sync', { account_ids: [] })
    if (data.progress_id) pollProgress(data.progress_id, '同步完成')
    else {
      ElMessage.success('同步已触发')
      load()
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    syncing.value = false
  }
}

// 单账号检测：重新拉取该账号的套餐 / 额度 / Token 状态（等价老面板的"检测"）
async function syncOne(row) {
  row._syncing = true
  try {
    const data = await api.post('/api/admin/upstream-accounts/sync', { account_ids: [row.id] })
    if (data.progress_id) pollProgress(data.progress_id, '检测完成')
    else {
      ElMessage.success('检测已触发')
      load()
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    row._syncing = false
  }
}

function pollProgress(progressId, doneText) {
  clearTimeout(pollTimer)
  const tick = async () => {
    try {
      const data = await api.get(`/api/admin/upstream-accounts/operations/${progressId}`)
      if (data.done) {
        ElMessage.success(doneText)
        load()
        return
      }
      pollTimer = setTimeout(tick, 2000)
    } catch {
      pollTimer = setTimeout(tick, 4000)
    }
  }
  pollTimer = setTimeout(tick, 1500)
}

function fmtTs(v) {
  if (!v) return '-'
  const d = new Date(Number(v) * 1000)
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString('zh-CN', { hour12: false })
}

onMounted(load)
onUnmounted(() => clearTimeout(pollTimer))
</script>

<template>
  <div class="page">
    <h2 class="page-title">上游账号（chatgpt2api）</h2>
    <el-alert type="info" :closable="false" style="margin-bottom: 16px">
      在此直接管理上游 ChatGPT 账号，上游密钥不出服务端、上游服务无需暴露端口。
      若页面报"上游版本不兼容"类错误，请按 docs/UPSTREAM-ADMIN-API.md 的预案处理。
    </el-alert>

    <div style="display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap">
      <el-input v-model="keyword" placeholder="搜索邮箱 / 用户 ID" style="width: 220px" clearable @keyup.enter="page = 1; load()" />
      <el-select v-model="statusFilter" style="width: 120px" @change="page = 1; load()">
        <el-option v-for="o in STATUS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="page = 1; load()">搜索</el-button>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
      <el-button type="primary" plain :icon="Plus" @click="addVisible = true">添加账号</el-button>
      <el-button type="warning" plain :loading="syncing" @click="syncAll">同步全部额度</el-button>
    </div>

    <el-alert v-if="loadError" type="error" :title="`加载失败：${loadError}`" :closable="false" style="margin-bottom: 16px" />

    <el-card>
      <el-table :data="items" v-loading="loading" size="small">
        <el-table-column label="账号" min-width="200">
          <template #default="{ row }">
            <div>{{ row.email || row.user_id || row.id }}</div>
            <div class="text-muted" style="font-size: 12px">
              {{ row.plan_label }} · {{ row.source_label }}{{ row.group_name ? ` · ${row.group_name}` : '' }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tooltip :disabled="!row.status_reason" :content="row.status_reason">
              <el-tag :type="row.status_tone === 'success' ? 'success' : row.status_tone === 'warning' ? 'warning' : row.status_tone === 'error' ? 'danger' : 'info'" size="small">
                {{ row.status_label }}
              </el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="绘图额度" width="110">
          <template #default="{ row }">
            <div>{{ row.quota_label }}</div>
            <div v-if="row.image_inflight || row.quota_reset_at" class="text-muted" style="font-size: 12px">
              <span v-if="row.image_inflight">在途 {{ row.image_inflight }}</span>
              <span v-if="row.quota_reset_at">{{ row.image_inflight ? ' · ' : '' }}重置 {{ fmtTs(row.quota_reset_at) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="AT / RT" width="140">
          <template #default="{ row }">
            <div style="font-size: 12px">
              <div>AT：<span :style="{ color: row.access_token_tone === 'success' ? 'green' : row.access_token_tone ? 'orange' : '' }">{{ row.access_token_label }}</span></div>
              <div>RT：{{ row.refresh_token_label }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="成/败" width="90">
          <template #default="{ row }">{{ row.success_count }} / {{ row.failure_count }}</template>
        </el-table-column>
        <el-table-column label="最近使用" width="150">
          <template #default="{ row }">{{ fmtTs(row.last_used_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" :loading="row._syncing" @click="syncOne(row)">检测</el-button>
            <el-button size="small" text :type="row.enabled ? 'warning' : 'success'" @click="batch(row, row.enabled ? 'disable' : 'enable')">
              {{ row.enabled ? '禁用' : '启用' }}
            </el-button>
            <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-if="total > 50"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="50"
        :current-page="page"
        style="margin-top: 12px; justify-content: center"
        @current-change="(p) => { page = p; load() }"
      />
      <el-empty v-if="!loading && !items.length && !loadError" description="上游暂无账号，点击「添加账号」导入" />
    </el-card>

    <el-dialog v-model="addVisible" title="添加上游账号" width="min(560px, 94vw)">
      <el-alert type="info" :closable="false" style="margin-bottom: 12px"
        title="粘贴 ChatGPT 账号的 Access Token，每行一个。导入后将自动同步套餐与额度。" />
      <el-input v-model="addText" type="textarea" :rows="8" placeholder="eyJhbGciOi...&#10;eyJhbGciOi..." />
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="addLoading" @click="addAccounts">导入并同步</el-button>
      </template>
    </el-dialog>
  </div>
</template>
