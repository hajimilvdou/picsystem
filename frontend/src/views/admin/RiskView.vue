<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh, Search, Unlock } from '@element-plus/icons-vue'
import { api } from '../../api/client'
import { fmtTime } from '../../utils/format'

const KIND_LABELS = {
  login_fail: '登录失败',
  login_locked: '触发锁定',
  rate_limit: '触发限流',
  register: '注册',
  register_blocked: '注册拦截',
  agreement_accept: '同意协议',
  redeem: '兑换',
  content_blocked: '内容拦截',
}
const KIND_TYPES = {
  login_fail: 'warning',
  login_locked: 'danger',
  rate_limit: 'danger',
  register: 'success',
  register_blocked: 'info',
  agreement_accept: 'primary',
  redeem: 'success',
  content_blocked: 'danger',
}

const thresholds = reactive({
  user_rate_limit_per_minute: 20,
  login_max_failures: 5,
  login_lock_minutes: 15,
  register_max_per_ip_day: 10,
  register_max_per_fp_total: 2,
})
const savingThresholds = ref(false)

const locks = ref([])
const events = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const filters = reactive({ kind: '', username: '' })

async function loadSettings() {
  const data = await api.get('/api/admin/settings')
  thresholds.user_rate_limit_per_minute = data.user_rate_limit_per_minute
  thresholds.login_max_failures = data.login_max_failures
  thresholds.login_lock_minutes = data.login_lock_minutes
  thresholds.register_max_per_ip_day = data.register_max_per_ip_day
  thresholds.register_max_per_fp_total = data.register_max_per_fp_total
}

async function saveThresholds() {
  savingThresholds.value = true
  try {
    await api.patch('/api/admin/settings', { ...thresholds })
    ElMessage.success('风控阈值已保存')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    savingThresholds.value = false
  }
}

async function loadLocks() {
  const data = await api.get('/api/admin/risk/locks')
  locks.value = data.items
}

async function unlock(username, ip) {
  try {
    await api.post('/api/admin/risk/unlock', { username, ip })
    ElMessage.success(`已解除 ${username}@${ip} 的登录锁定`)
    loadLocks()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function loadEvents() {
  loading.value = true
  try {
    const p = new URLSearchParams({ page: String(page.value), size: '20' })
    if (filters.kind) p.set('kind', filters.kind)
    if (filters.username) p.set('username', filters.username)
    const data = await api.get(`/api/admin/risk/events?${p}`)
    events.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  loadSettings()
  loadLocks()
  loadEvents()
})
</script>

<template>
  <div class="page">
    <h2 class="page-title">风控中心</h2>

    <el-row :gutter="16">
      <el-col :span="12">
        <el-card style="margin-bottom: 16px">
          <template #header>风控阈值</template>
          <el-form label-width="220px">
            <el-form-item label="单用户每分钟调用上限">
              <el-input-number v-model="thresholds.user_rate_limit_per_minute" :min="1" :max="1000" />
            </el-form-item>
            <el-form-item label="登录连续失败锁定阈值（次）">
              <el-input-number v-model="thresholds.login_max_failures" :min="3" :max="50" />
            </el-form-item>
            <el-form-item label="登录锁定时长（分钟）">
              <el-input-number v-model="thresholds.login_lock_minutes" :min="1" :max="1440" />
            </el-form-item>
            <el-form-item label="单 IP 每日注册上限">
              <el-input-number v-model="thresholds.register_max_per_ip_day" :min="1" :max="1000" />
            </el-form-item>
            <el-form-item label="单设备指纹注册总数上限">
              <el-input-number v-model="thresholds.register_max_per_fp_total" :min="1" :max="100" />
            </el-form-item>
            <el-button type="primary" :loading="savingThresholds" @click="saveThresholds">保存</el-button>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card style="margin-bottom: 16px">
          <template #header>
            <div style="display: flex; justify-content: space-between; align-items: center">
              <span>当前登录锁定</span>
              <el-button size="small" text :icon="Refresh" @click="loadLocks">刷新</el-button>
            </div>
          </template>
          <el-empty v-if="!locks.length" description="暂无锁定" :image-size="60" />
          <el-table v-else :data="locks" size="small">
            <el-table-column prop="username" label="用户名" />
            <el-table-column prop="ip" label="IP" width="130" />
            <el-table-column label="剩余时间" width="110">
              <template #default="{ row }">{{ Math.ceil(row.remaining_seconds / 60) }} 分钟</template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button size="small" type="primary" text :icon="Unlock" @click="unlock(row.username, row.ip)">解锁</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="text-muted" style="margin-top: 8px">
            锁定保存在服务内存中，服务重启后自动清除。
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card>
      <template #header>风控事件</template>
      <div style="display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap">
        <el-select v-model="filters.kind" placeholder="事件类型" style="width: 150px" clearable>
          <el-option v-for="(l, k) in KIND_LABELS" :key="k" :label="l" :value="k" />
        </el-select>
        <el-input v-model="filters.username" placeholder="用户名" style="width: 160px" clearable @keyup.enter="page = 1; loadEvents()" />
        <el-button type="primary" :icon="Search" @click="page = 1; loadEvents()">筛选</el-button>
        <el-button :icon="Refresh" @click="loadEvents">刷新</el-button>
      </div>
      <el-table :data="events" v-loading="loading" size="small">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag :type="KIND_TYPES[row.kind] || 'info'" size="small">{{ row.kind_label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="username" label="用户名" width="140" show-overflow-tooltip />
        <el-table-column prop="ip" label="IP" width="130" show-overflow-tooltip />
        <el-table-column prop="detail" label="详情" min-width="220" show-overflow-tooltip />
      </el-table>
      <el-pagination
        v-if="total > 20"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="20"
        :current-page="page"
        style="margin-top: 12px; justify-content: center"
        @current-change="(p) => { page = p; loadEvents() }"
      />
    </el-card>
  </div>
</template>
