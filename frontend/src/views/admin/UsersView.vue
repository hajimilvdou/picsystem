<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import { api } from '../../api/client'
import { fmtTime, fmtSize, copyText } from '../../utils/format'

const FEATURE_LABELS = { chat: '对话', image: '绘图', search: '搜索', ppt: 'PPT/PSD' }
const FEATURES = Object.keys(FEATURE_LABELS)

const items = ref([])
const total = ref(0)
const pendingCount = ref(0)
const page = ref(1)
const query = ref('')
const statusFilter = ref('')
const loading = ref(false)
const selected = ref([])

const editVisible = ref(false)
const editUser = ref(null)
const editForm = reactive({ role: 'user', status: 'active', quotas: {}, storage_limit_mb: 0, storage_limit_clear: true, storage_used_mb: 0, max_inflight: 2, max_inflight_clear: true, note: '' })

const createVisible = ref(false)
const createForm = reactive({ username: '', password: '', role: 'user', quotas: { chat: 0, image: 0, search: 0, ppt: 0 } })

const statsVisible = ref(false)
const statsData = ref(null)
const bulkVisible = ref(false)
const bulkForm = reactive({ feature: 'chat', pool: 'permanent', mode: 'add', amount: 10, valid_days: 1, valid_hours: 0, fixed_expires_at: null, target: 'selected' })

async function load() {
  loading.value = true
  try {
    const p = new URLSearchParams({ q: query.value, page: String(page.value), size: '15' })
    if (statusFilter.value) p.set('status', statusFilter.value)
    const data = await api.get(`/api/admin/users?${p}`)
    items.value = data.items
    total.value = data.total
    pendingCount.value = data.pending_count
  } finally {
    loading.value = false
  }
}

function openEdit(user) {
  editUser.value = user
  editForm.role = user.role
  editForm.status = user.status
  editForm.storage_limit_mb = user.storage_limit_mb ?? 0
  editForm.storage_limit_clear = user.storage_limit_mb == null
  editForm.storage_used_mb = Math.round((user.storage_bytes / 1024 / 1024) * 10) / 10
  editForm.max_inflight = user.max_inflight ?? 2
  editForm.max_inflight_clear = user.max_inflight == null
  editForm.note = user.note || ''
  editForm.quotas = {}
  for (const f of FEATURES) {
    editForm.quotas[f] = { total: user.quotas[f]?.total ?? 0, used: user.quotas[f]?.used ?? 0, reset: false }
  }
  editVisible.value = true
}

async function saveEdit() {
  const quotas = {}
  for (const f of FEATURES) {
    quotas[f] = { total: editForm.quotas[f].total, reset_used: editForm.quotas[f].reset }
  }
  try {
    await api.patch(`/api/admin/users/${editUser.value.id}`, {
      role: editForm.role,
      status: editForm.status,
      quotas,
      storage_limit_mb: editForm.storage_limit_mb,
      storage_limit_clear: editForm.storage_limit_clear,
      max_inflight: editForm.max_inflight,
      max_inflight_clear: editForm.max_inflight_clear,
      note: editForm.note,
    })
    ElMessage.success('已保存')
    editVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function createUser() {
  try {
    await api.post('/api/admin/users', createForm)
    ElMessage.success('用户已创建')
    createVisible.value = false
    createForm.username = createForm.password = ''
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function approve(user) {
  await api.post(`/api/admin/users/${user.id}/approve`)
  ElMessage.success(`已通过 ${user.username} 的注册审核`)
  load()
}

async function reject(user) {
  try {
    await ElMessageBox.confirm(`确定拒绝并删除「${user.username}」的注册申请吗？邀请码次数将退回。`, '拒绝申请', { type: 'warning' })
  } catch { return }
  await api.post(`/api/admin/users/${user.id}/reject`)
  ElMessage.success('已拒绝')
  load()
}

async function openStats(user) {
  statsData.value = null
  statsVisible.value = true
  statsData.value = await api.get(`/api/admin/users/${user.id}/stats`)
}

async function doBulk() {
  const ids = bulkForm.target === 'all' ? 'all' : selected.value.map((u) => u.id)
  if (bulkForm.target === 'selected' && !ids.length) {
    ElMessage.warning('请先勾选用户，或选择全部用户')
    return
  }
  try {
    const data = await api.post('/api/admin/users/bulk-quota', {
      user_ids: ids,
      feature: bulkForm.feature,
      pool: bulkForm.pool,
      mode: bulkForm.mode,
      amount: bulkForm.amount,
      valid_days: bulkForm.valid_days,
      valid_hours: bulkForm.valid_hours,
      fixed_expires_at: bulkForm.fixed_expires_at || null,
    })
    ElMessage.success(`已调整 ${data.affected} 个用户`)
    bulkVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function resetPassword(user) {
  try {
    await ElMessageBox.confirm(`确定重置「${user.username}」的密码吗？其所有会话将失效。`, '重置密码', { type: 'warning' })
  } catch { return }
  const data = await api.post(`/api/admin/users/${user.id}/reset-password`, {})
  await ElMessageBox.alert(`新密码（请立即转达用户）：\n${data.password}`, '重置成功', { confirmButtonText: '复制' })
  copyText(data.password)
}

async function removeUser(user) {
  try {
    await ElMessageBox.confirm(
      `确定删除用户「${user.username}」吗？其对话、文件与密钥将一并删除，且不可恢复。`,
      '删除用户',
      { type: 'error', confirmButtonText: '删除' }
    )
  } catch { return }
  await api.del(`/api/admin/users/${user.id}`)
  ElMessage.success('已删除')
  load()
}

function quotaSummary(quotas) {
  return FEATURES.map((f) => {
    const q = quotas[f]
    if (!q) return `${FEATURE_LABELS[f]} 0`
    const perm = q.total < 0 ? '不限' : Math.max(q.total - q.used, 0)
    const temp = q.temp > 0 ? `(+${q.temp}限时)` : ''
    return `${FEATURE_LABELS[f]} ${perm}${temp}`
  }).join(' · ')
}

const statusTag = computed(() => ({
  active: { label: '正常', type: 'success' },
  disabled: { label: '已禁用', type: 'danger' },
  pending: { label: '待审核', type: 'warning' },
}))

onMounted(load)
</script>

<template>
  <div class="page">
    <h2 class="page-title">用户管理</h2>
    <div style="display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap">
      <el-input v-model="query" placeholder="搜索用户名 / ID" style="width: 200px" clearable @keyup.enter="page = 1; load()" />
      <el-select v-model="statusFilter" placeholder="状态" style="width: 130px" clearable @change="page = 1; load()">
        <el-option label="全部" value="" />
        <el-option label="正常" value="active" />
        <el-option label="已禁用" value="disabled" />
        <el-option :label="`待审核 (${pendingCount})`" value="pending" />
      </el-select>
      <el-button type="primary" :icon="Search" @click="page = 1; load()">搜索</el-button>
      <el-button type="primary" plain :icon="Plus" @click="createVisible = true">新建用户</el-button>
      <el-button type="warning" plain @click="bulkVisible = true">批量调额</el-button>
    </div>

    <el-card>
      <el-table :data="items" v-loading="loading" @selection-change="(rows) => (selected = rows)">
        <el-table-column type="selection" width="40" />
        <el-table-column prop="id" label="ID" width="60" />
        <el-table-column prop="username" label="用户名" min-width="110">
          <template #default="{ row }">
            <div>{{ row.username }}</div>
            <el-tooltip v-if="row.note || row.reg_note" :content="row.note || `申请备注：${row.reg_note}`">
              <span class="text-muted" style="font-size: 12px">备注：{{ (row.note || row.reg_note).slice(0, 12) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="角色" width="80">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'warning' : 'info'" size="small">
              {{ row.role === 'admin' ? '管理员' : '用户' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTag[row.status]?.type" size="small">{{ statusTag[row.status]?.label || row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="剩余额度（含限时）" min-width="230">
          <template #default="{ row }">
            <span class="text-muted" style="font-size: 12px">{{ quotaSummary(row.quotas) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="注册时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="290" fixed="right">
          <template #default="{ row }">
            <template v-if="row.status === 'pending'">
              <el-button size="small" text type="success" @click="approve(row)">通过</el-button>
              <el-button size="small" text type="danger" @click="reject(row)">拒绝</el-button>
            </template>
            <template v-else>
              <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
              <el-button size="small" text type="primary" @click="openStats(row)">统计</el-button>
              <el-button size="small" text type="warning" @click="resetPassword(row)">重置密码</el-button>
              <el-button size="small" text type="danger" @click="removeUser(row)">删除</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>
      <el-pagination
        v-if="total > 15"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="15"
        :current-page="page"
        style="margin-top: 12px; justify-content: center"
        @current-change="(p) => { page = p; load() }"
      />
    </el-card>

    <!-- 编辑用户 -->
    <el-dialog v-model="editVisible" :title="`编辑用户：${editUser?.username}`" width="min(480px, 94vw)">
      <el-form label-width="90px">
        <el-form-item label="角色">
          <el-select v-model="editForm.role" style="width: 160px">
            <el-option label="普通用户" value="user" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-switch v-model="editForm.status" active-value="active" inactive-value="disabled" active-text="正常" inactive-text="禁用" />
        </el-form-item>
        <el-form-item label="并发上限">
          <el-input-number v-model="editForm.max_inflight" :min="1" :max="100" :disabled="editForm.max_inflight_clear" />
          <el-checkbox v-model="editForm.max_inflight_clear" style="margin-left: 10px">跟随全局</el-checkbox>
        </el-form-item>
        <el-form-item label="存储上限">
          <el-input-number v-model="editForm.storage_limit_mb" :min="0" :max="1048576" :disabled="editForm.storage_limit_clear" />
          <span class="text-muted" style="margin: 0 10px">MB（已用 {{ editForm.storage_used_mb }}MB）</span>
          <el-checkbox v-model="editForm.storage_limit_clear">跟随全局默认</el-checkbox>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="editForm.note" type="textarea" :rows="2" maxlength="500" show-word-limit placeholder="仅管理员可见；用户注册时填写的申请备注会自动带入" />
        </el-form-item>
        <el-divider content-position="left">永久组额度（-1 不限）</el-divider>
        <el-form-item v-for="f in FEATURES" :key="f" :label="FEATURE_LABELS[f]">
          <el-input-number v-model="editForm.quotas[f].total" :min="-1" :max="999999" />
          <span class="text-muted" style="margin: 0 10px">已用 {{ editForm.quotas[f].used }}</span>
          <el-checkbox v-model="editForm.quotas[f].reset">重置已用</el-checkbox>
        </el-form-item>
        <el-form-item>
          <span class="text-muted">限时组发放请使用「批量调额」</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新建用户 -->
    <el-dialog v-model="createVisible" title="新建用户" width="min(480px, 94vw)">
      <el-form label-width="90px">
        <el-form-item label="用户名">
          <el-input v-model="createForm.username" placeholder="3-32 位字母数字_-" maxlength="32" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="createForm.password" placeholder="至少 8 位" show-password type="password" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="createForm.role" style="width: 160px">
            <el-option label="普通用户" value="user" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
        <el-divider content-position="left">初始永久额度（-1 不限）</el-divider>
        <el-form-item v-for="f in FEATURES" :key="f" :label="FEATURE_LABELS[f]">
          <el-input-number v-model="createForm.quotas[f]" :min="-1" :max="999999" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="createUser">创建</el-button>
      </template>
    </el-dialog>

    <!-- 批量调额 -->
    <el-dialog v-model="bulkVisible" title="批量调整额度" width="min(520px, 94vw)">
      <el-form label-width="110px">
        <el-form-item label="目标用户">
          <el-radio-group v-model="bulkForm.target">
            <el-radio value="selected">勾选的 {{ selected.length }} 个用户</el-radio>
            <el-radio value="all">全部用户</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="功能">
          <el-select v-model="bulkForm.feature" style="width: 160px">
            <el-option v-for="(l, f) in FEATURE_LABELS" :key="f" :label="l" :value="f" />
          </el-select>
        </el-form-item>
        <el-form-item label="额度组">
          <el-radio-group v-model="bulkForm.pool">
            <el-radio value="permanent">永久组</el-radio>
            <el-radio value="temporary">限时组</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="操作">
          <el-radio-group v-model="bulkForm.mode">
            <el-radio value="add">增加</el-radio>
            <el-radio value="subtract">减少</el-radio>
            <el-radio value="set">统一设为</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="数量（次）">
          <el-input-number v-model="bulkForm.amount" :min="0" :max="1000000" />
        </el-form-item>
        <template v-if="bulkForm.pool === 'temporary' && bulkForm.mode !== 'subtract'">
          <el-form-item label="固定到期时间">
            <el-date-picker v-model="bulkForm.fixed_expires_at" type="datetime" placeholder="可选，优先于天数+小时" style="width: 100%" />
          </el-form-item>
          <el-form-item label="有效天数">
            <el-input-number v-model="bulkForm.valid_days" :min="1" :max="3650" />
            <span class="text-muted" style="margin-left: 10px">1 = 当天 24 点（按签到时区）</span>
          </el-form-item>
          <el-form-item label="附加小时">
            <el-input-number v-model="bulkForm.valid_hours" :min="0" :max="8784" />
          </el-form-item>
        </template>
      </el-form>
      <template #footer>
        <el-button @click="bulkVisible = false">取消</el-button>
        <el-button type="primary" @click="doBulk">执行</el-button>
      </template>
    </el-dialog>

    <!-- 用户统计 -->
    <el-dialog v-model="statsVisible" :title="`操作统计：${statsData?.username || ''}`" width="min(560px, 94vw)">
      <div v-if="statsData" style="display: grid; gap: 14px">
        <el-row :gutter="12">
          <el-col :span="8"><el-statistic title="近 7 天调用" :value="statsData.week_count" /></el-col>
          <el-col :span="8"><el-statistic title="活跃设备" :value="statsData.active_sessions" /></el-col>
          <el-col :span="8"><el-statistic title="存储占用" :value="Math.round(statsData.storage_bytes / 1024 / 1024 * 10) / 10" suffix="MB" /></el-col>
        </el-row>
        <el-table :data="Object.entries(statsData.totals).map(([f, v]) => ({ f, ...v }))" size="small">
          <el-table-column label="功能" width="110">
            <template #default="{ row }">{{ FEATURE_LABELS[row.f] || row.f }}</template>
          </el-table-column>
          <el-table-column prop="count" label="累计调用" width="110" />
          <el-table-column prop="success" label="成功次数" />
        </el-table>
        <div class="text-muted">最近活跃：{{ fmtTime(statsData.last_active) }}</div>
        <div v-if="statsData.daily.length">
          <div style="margin-bottom: 6px">近 7 天每日调用</div>
          <div style="display: flex; gap: 8px; flex-wrap: wrap">
            <el-tag v-for="d in statsData.daily" :key="d.day" size="small" effect="plain">{{ d.day.slice(5) }}: {{ d.count }}</el-tag>
          </div>
        </div>
      </div>
      <el-skeleton v-else :rows="5" animated />
    </el-dialog>
  </div>
</template>
