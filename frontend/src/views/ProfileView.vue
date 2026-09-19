<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api/client'
import { useAuthStore, FEATURE_LABELS } from '../stores/auth'
import { fmtTime } from '../utils/format'

const auth = useAuthStore()

const storageUsedMb = computed(() => Math.round((auth.storage.used_bytes / 1024 / 1024) * 10) / 10)
const storagePercent = computed(() => {
  if (!auth.storage.limit_mb) return 0
  return Math.min(Math.round((auth.storage.used_bytes / (auth.storage.limit_mb * 1024 * 1024)) * 100), 100)
})
const pwd = reactive({ old_password: '', new_password: '', confirm: '' })
const saving = ref(false)
const logs = ref([])
const logsTotal = ref(0)
const logsPage = ref(1)
const sessions = ref([])
const checkinLoading = ref(false)
const checkinResult = ref('')
const redeemCode = ref('')
const redeemLoading = ref(false)
const redeemResult = ref('')

async function doRedeem() {
  if (!redeemCode.value.trim()) {
    ElMessage.warning('请输入兑换码')
    return
  }
  redeemLoading.value = true
  try {
    const data = await api.post('/api/auth/redeem', { code: redeemCode.value.trim() })
    const parts = Object.entries(data.granted || {})
      .filter(([, v]) => v > 0)
      .map(([k, v]) => `${FEATURE_LABELS[k] || k} +${v}`)
    redeemResult.value = parts.length ? `兑换成功：${parts.join('，')}` : '兑换成功'
    redeemCode.value = ''
    await auth.fetchMe(true)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    redeemLoading.value = false
  }
}

async function doCheckin() {
  checkinLoading.value = true
  try {
    const data = await api.post('/api/auth/checkin')
    const parts = Object.entries(data.granted || {})
      .filter(([, v]) => v > 0)
      .map(([k, v]) => `${FEATURE_LABELS[k] || k} +${v}`)
    checkinResult.value = parts.length ? `获得：${parts.join('，')}` : data.message || '签到成功'
    await auth.fetchMe(true)
  } catch (e) {
    ElMessage.warning(e.message)
  } finally {
    checkinLoading.value = false
  }
}

function fmtTempExpiry(iso) {
  if (!iso) return ''
  return new Date(iso).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric' })
}

async function changePassword() {
  if (pwd.new_password.length < 8) {
    ElMessage.warning('新密码长度至少 8 位')
    return
  }
  if (pwd.new_password !== pwd.confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  saving.value = true
  try {
    await api.post('/api/auth/change-password', { old_password: pwd.old_password, new_password: pwd.new_password })
    ElMessage.success('密码已修改')
    pwd.old_password = pwd.new_password = pwd.confirm = ''
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function loadLogs() {
  const data = await api.get(`/api/me/logs?page=${logsPage.value}&size=10`)
  logs.value = data.items
  logsTotal.value = data.total
}

function quotaPercent(q) {
  if (!q || q.total < 0) return 0
  return Math.min(Math.round((q.used / Math.max(q.total, 1)) * 100), 100)
}

async function loadSessions() {
  const data = await api.get('/api/auth/sessions')
  sessions.value = data.items
}

async function revokeSession(row) {
  await api.del(`/api/auth/sessions/${row.id}`)
  ElMessage.success('已注销该设备')
  loadSessions()
}

onMounted(() => {
  loadLogs()
  loadSessions()
})
</script>

<template>
  <div class="page" style="max-width: 900px">
    <h2 class="page-title">个人中心</h2>
    <el-row :gutter="16">
      <el-col :xs="24" :md="10">
        <el-card style="margin-bottom: 16px">
          <template #header>账号信息</template>
          <p><span class="text-muted">用户名：</span>{{ auth.user?.username }}</p>
          <p><span class="text-muted">角色：</span>{{ auth.user?.role === 'admin' ? '管理员' : '普通用户' }}</p>
        </el-card>

        <el-card style="margin-bottom: 16px" v-if="auth.checkinEnabled">
          <template #header>每日签到</template>
          <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
            <el-button type="primary" :disabled="auth.checkedInToday" :loading="checkinLoading" @click="doCheckin">
              {{ auth.checkedInToday ? '今日已签到' : '立即签到' }}
            </el-button>
            <span v-if="checkinResult" class="text-muted">{{ checkinResult }}</span>
          </div>
        </el-card>

        <el-card style="margin-bottom: 16px">
          <template #header>兑换码</template>
          <div style="display: flex; gap: 10px; flex-wrap: wrap">
            <el-input v-model="redeemCode" placeholder="输入兑换码" style="max-width: 240px" @keyup.enter="doRedeem" />
            <el-button type="primary" plain :loading="redeemLoading" @click="doRedeem">兑换</el-button>
          </div>
          <div v-if="redeemResult" class="text-muted" style="margin-top: 8px">{{ redeemResult }}</div>
        </el-card>

        <el-card style="margin-bottom: 16px">
          <template #header>我的额度（次数）</template>
          <div v-for="(label, f) in FEATURE_LABELS" :key="f" style="margin-bottom: 14px">
            <div style="display: flex; justify-content: space-between; margin-bottom: 4px">
              <span>{{ label }}</span>
              <span class="text-muted">
                <template v-if="auth.quotas[f]?.total < 0">不限量</template>
                <template v-else>永久：已用 {{ auth.quotas[f]?.used ?? 0 }} / 共 {{ auth.quotas[f]?.total ?? 0 }}</template>
              </span>
            </div>
            <el-progress
              :percentage="auth.quotas[f]?.total < 0 ? 0 : quotaPercent(auth.quotas[f])"
              :status="quotaPercent(auth.quotas[f]) >= 90 ? 'exception' : undefined"
            />
            <div v-if="auth.quotas[f]?.temp > 0" class="temp-line">
              限时额度 +{{ auth.quotas[f].temp }}（{{ fmtTempExpiry(auth.quotas[f].temp_expires_at) }} 到期）
            </div>
          </div>
          <div style="margin-bottom: 14px">
            <div style="display: flex; justify-content: space-between; margin-bottom: 4px">
              <span>存储空间</span>
              <span class="text-muted">
                <template v-if="!auth.storage.limit_mb">不限量</template>
                <template v-else>已用 {{ storageUsedMb }}MB / 共 {{ auth.storage.limit_mb }}MB</template>
              </span>
            </div>
            <el-progress :percentage="storagePercent" :status="storagePercent >= 90 ? 'exception' : undefined" />
          </div>
          <div class="text-muted">额度用尽后请联系管理员调整</div>
        </el-card>

        <el-card>
          <template #header>登录设备</template>
          <el-table :data="sessions" size="small">
            <el-table-column label="设备" min-width="140">
              <template #default="{ row }">
                {{ row.device_label }}
                <el-tag v-if="row.current" type="success" size="small" style="margin-left: 6px">本机</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="ip" label="IP" width="120" />
            <el-table-column label="首次登录" width="145">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="最近活跃" width="145">
              <template #default="{ row }">{{ fmtTime(row.last_seen_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-popconfirm v-if="!row.current" title="注销该设备的登录？" @confirm="revokeSession(row)">
                  <template #reference>
                    <el-button size="small" text type="danger">注销</el-button>
                  </template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <div class="text-muted" style="margin-top: 8px">如发现陌生设备，请立即注销并修改密码</div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>修改密码</template>
          <el-form label-position="top">
            <el-form-item label="原密码">
              <el-input v-model="pwd.old_password" type="password" show-password autocomplete="current-password" />
            </el-form-item>
            <el-form-item label="新密码">
              <el-input v-model="pwd.new_password" type="password" show-password autocomplete="new-password" />
            </el-form-item>
            <el-form-item label="确认新密码">
              <el-input v-model="pwd.confirm" type="password" show-password autocomplete="new-password" />
            </el-form-item>
            <el-button type="primary" :loading="saving" @click="changePassword">保存</el-button>
          </el-form>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="14">
        <el-card>
          <template #header>最近调用记录</template>
          <el-table :data="logs" size="small">
            <el-table-column label="功能" width="90">
              <template #default="{ row }">{{ FEATURE_LABELS[row.feature] || row.feature }}</template>
            </el-table-column>
            <el-table-column prop="model" label="模型" min-width="110" show-overflow-tooltip />
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.status === 'success' ? 'success' : 'danger'" size="small">
                  {{ row.status === 'success' ? '成功' : '失败' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="90">
              <template #default="{ row }">{{ (row.latency_ms / 1000).toFixed(1) }}s</template>
            </el-table-column>
            <el-table-column label="时间" width="150">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
          </el-table>
          <el-pagination
            v-if="logsTotal > 10"
            layout="prev, pager, next"
            :total="logsTotal"
            :page-size="10"
            :current-page="logsPage"
            style="margin-top: 12px; justify-content: center"
            @current-change="(p) => { logsPage = p; loadLogs() }"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.temp-line {
  font-size: 12px;
  color: #b88230;
  margin-top: 2px;
}
</style>
