<script setup>
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { api } from '../../api/client'
import { fmtTime, copyText } from '../../utils/format'

const FEATURE_LABELS = { chat: '对话', image: '绘图', search: '搜索', ppt: 'PPT/PSD' }
const FEATURES = Object.keys(FEATURE_LABELS)

const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const dialogVisible = ref(false)
const editing = ref(null) // null = 新建
const form = reactive({ code: '', note: '', max_uses: 1, quotas: { chat: 0, image: 0, search: 0, ppt: 0 }, pool_type: 'permanent', valid_days: 1, valid_hours: 0, fixed_expires_at: null, expires_at: null, enabled: true })

async function load() {
  loading.value = true
  try {
    const data = await api.get(`/api/admin/invites?page=${page.value}&size=15`)
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editing.value = null
  Object.assign(form, { code: '', note: '', max_uses: 1, quotas: { chat: 0, image: 0, search: 0, ppt: 0 }, pool_type: 'permanent', valid_days: 1, valid_hours: 0, fixed_expires_at: null, expires_at: null, enabled: true })
  dialogVisible.value = true
}

function openEdit(row) {
  editing.value = row
  Object.assign(form, {
    code: row.code,
    note: row.note,
    max_uses: row.max_uses,
    quotas: { ...row.quotas },
    pool_type: row.pool_type || 'permanent',
    valid_days: row.valid_days ?? 1,
    valid_hours: row.valid_hours ?? 0,
    fixed_expires_at: row.fixed_expires_at,
    expires_at: row.expires_at,
    enabled: row.enabled,
  })
  dialogVisible.value = true
}

async function save() {
  try {
    if (editing.value) {
      await api.patch(`/api/admin/invites/${editing.value.id}`, {
        note: form.note,
        max_uses: form.max_uses,
        enabled: form.enabled,
        expires_at: form.expires_at || null,
        chat_quota: form.quotas.chat,
        image_quota: form.quotas.image,
        search_quota: form.quotas.search,
        ppt_quota: form.quotas.ppt,
        pool_type: form.pool_type,
        valid_days: form.valid_days,
        valid_hours: form.valid_hours,
        fixed_expires_at: form.fixed_expires_at || null,
      })
      ElMessage.success('已保存')
    } else {
      await api.post('/api/admin/invites', {
        code: form.code || null,
        note: form.note,
        max_uses: form.max_uses,
        expires_at: form.expires_at || null,
        chat_quota: form.quotas.chat,
        image_quota: form.quotas.image,
        search_quota: form.quotas.search,
        ppt_quota: form.quotas.ppt,
        pool_type: form.pool_type,
        valid_days: form.valid_days,
        valid_hours: form.valid_hours,
        fixed_expires_at: form.fixed_expires_at || null,
      })
      ElMessage.success('邀请码已创建')
    }
    dialogVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function toggle(row) {
  await api.patch(`/api/admin/invites/${row.id}`, { enabled: !row.enabled })
  load()
}

async function copy(row) {
  await copyText(row.code)
  ElMessage.success('已复制邀请码')
}

async function remove(row) {
  await api.del(`/api/admin/invites/${row.id}`)
  ElMessage.success('已删除')
  load()
}

function quotaSummary(q) {
  return FEATURES.map((f) => `${FEATURE_LABELS[f]} ${q[f] < 0 ? '不限' : q[f]}`).join(' · ')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h2 class="page-title">邀请码管理</h2>
    <div style="margin-bottom: 16px">
      <el-button type="primary" :icon="Plus" @click="openCreate">创建邀请码</el-button>
    </div>
    <el-card>
      <el-table :data="items" v-loading="loading">
        <el-table-column prop="code" label="邀请码" min-width="140">
          <template #default="{ row }">
            <code>{{ row.code }}</code>
            <el-button size="small" text type="primary" @click="copy(row)">复制</el-button>
          </template>
        </el-table-column>
        <el-table-column label="注册额度" min-width="230">
          <template #default="{ row }">
            <span class="text-muted" style="font-size: 12px">{{ quotaSummary(row.quotas) }}</span>
            <div v-if="row.pool_type === 'temporary'" style="font-size: 12px; color: #b88230">
              限时组 · {{ row.fixed_expires_at ? `${fmtTime(row.fixed_expires_at)} 固定到期` : `${row.valid_days}天+${row.valid_hours}时` }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="使用" width="90">
          <template #default="{ row }">{{ row.used_count }} / {{ row.max_uses }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="有效期至" width="150">
          <template #default="{ row }">{{ row.expires_at ? fmtTime(row.expires_at) : '永久' }}</template>
        </el-table-column>
        <el-table-column prop="note" label="备注" min-width="120" show-overflow-tooltip />
        <el-table-column label="操作" width="170" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openEdit(row)">编辑</el-button>
            <el-button size="small" text :type="row.enabled ? 'warning' : 'success'" @click="toggle(row)">
              {{ row.enabled ? '停用' : '启用' }}
            </el-button>
            <el-popconfirm title="确定删除该邀请码？" @confirm="remove(row)">
              <template #reference>
                <el-button size="small" text type="danger">删除</el-button>
              </template>
            </el-popconfirm>
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

    <el-dialog v-model="dialogVisible" :title="editing ? '编辑邀请码' : '创建邀请码'" width="min(480px, 94vw)">
      <el-form label-width="110px">
        <el-form-item v-if="!editing" label="邀请码">
          <el-input v-model="form.code" placeholder="留空则自动生成" maxlength="64" />
        </el-form-item>
        <el-form-item label="可用次数">
          <el-input-number v-model="form.max_uses" :min="1" :max="100000" />
        </el-form-item>
        <el-form-item label="有效期至">
          <el-date-picker v-model="form.expires_at" type="datetime" placeholder="留空为永久有效" style="width: 100%" />
        </el-form-item>
        <el-form-item v-if="editing" label="状态">
          <el-switch v-model="form.enabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-divider content-position="left">注册即赠额度（-1 不限）</el-divider>
        <el-form-item v-for="f in FEATURES" :key="f" :label="FEATURE_LABELS[f]">
          <el-input-number v-model="form.quotas[f]" :min="-1" :max="999999" />
        </el-form-item>
        <el-form-item label="额度组">
          <el-radio-group v-model="form.pool_type">
            <el-radio value="permanent">永久组</el-radio>
            <el-radio value="temporary">限时组</el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.pool_type === 'temporary'">
          <el-form-item label="固定到期时间">
            <el-date-picker v-model="form.fixed_expires_at" type="datetime" placeholder="可选，优先于天数+小时" style="width: 100%" />
          </el-form-item>
          <el-form-item label="有效天数">
            <el-input-number v-model="form.valid_days" :min="1" :max="3650" />
            <span class="text-muted" style="margin-left: 10px">1 = 当天 24 点（按签到时区）</span>
          </el-form-item>
          <el-form-item label="附加小时">
            <el-input-number v-model="form.valid_hours" :min="0" :max="8784" />
          </el-form-item>
        </template>
        <el-form-item label="备注">
          <el-input v-model="form.note" maxlength="200" placeholder="内部备注，用户不可见" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
