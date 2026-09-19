<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { api } from '../api/client'
import { fmtTime, copyText } from '../utils/format'

const items = ref([])
const loading = ref(false)
const createVisible = ref(false)
const newName = ref('')
const createdKey = ref('')
const baseUrl = location.origin

async function load() {
  loading.value = true
  try {
    const data = await api.get('/api/keys')
    items.value = data.items
  } finally {
    loading.value = false
  }
}

async function create() {
  const data = await api.post('/api/keys', { name: newName.value })
  createdKey.value = data.key
  newName.value = ''
  load()
}

async function copyKey() {
  await copyText(createdKey.value)
  ElMessage.success('已复制')
}

async function toggle(item) {
  await api.patch(`/api/keys/${item.id}`)
  load()
}

async function remove(item) {
  try {
    await ElMessageBox.confirm(`确定删除密钥「${item.name}」吗？删除后使用该密钥的调用将立即失败。`, '删除密钥', { type: 'warning' })
  } catch {
    return
  }
  await api.del(`/api/keys/${item.id}`)
  load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h2 class="page-title">API 密钥</h2>
    <el-alert type="info" :closable="false" style="margin-bottom: 16px">
      <p>通过密钥以 OpenAI 兼容方式调用本站接口，调用按你的账号额度按次计费。</p>
      <p style="margin: 4px 0 0">接口地址（Base URL）：<code>{{ baseUrl }}/v1</code>，鉴权方式：<code>Authorization: Bearer sk-...</code></p>
    </el-alert>

    <el-card>
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>我的密钥</span>
          <el-button type="primary" size="small" :icon="Plus" @click="createVisible = true">创建密钥</el-button>
        </div>
      </template>
      <el-table :data="items" v-loading="loading">
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column prop="prefix" label="密钥" min-width="120">
          <template #default="{ row }"><code>{{ row.prefix }}...</code></template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最近使用" width="160">
          <template #default="{ row }">{{ fmtTime(row.last_used_at) }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="toggle(row)">{{ row.enabled ? '停用' : '启用' }}</el-button>
            <el-button size="small" text type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card style="margin-top: 16px">
      <template #header>调用示例</template>
      <pre class="code-block">curl {{ baseUrl }}/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-你的密钥" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}],"stream":true}'</pre>
      <div class="text-muted">
        支持接口：/v1/chat/completions（对话）、/v1/images/generations 与 /v1/images/edits（绘图）、
        /v1/search（搜索）、/v1/ppt/generations 与 /v1/editable-file-tasks（PPT/PSD）、/v1/models（模型列表）。
      </div>
    </el-card>

    <el-dialog v-model="createVisible" title="创建密钥" width="min(420px, 94vw)" :close-on-click-modal="false">
      <template v-if="!createdKey">
        <el-input v-model="newName" placeholder="密钥名称（便于区分用途）" maxlength="100" />
        <div style="text-align: right; margin-top: 16px">
          <el-button @click="createVisible = false">取消</el-button>
          <el-button type="primary" @click="create">创建</el-button>
        </div>
      </template>
      <template v-else>
        <el-alert type="warning" :closable="false" title="密钥仅显示一次，请立即复制保存" />
        <div class="key-display">
          <code>{{ createdKey }}</code>
        </div>
        <div style="text-align: right; margin-top: 16px">
          <el-button type="primary" @click="copyKey">复制</el-button>
          <el-button @click="createVisible = false; createdKey = ''">完成</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.code-block {
  background: #faf6ef;
  border: 1px solid var(--ps-border);
  border-radius: 8px;
  padding: 12px;
  overflow-x: auto;
  font-size: 13px;
}
.key-display {
  margin-top: 12px;
  padding: 12px;
  background: #faf6ef;
  border-radius: 8px;
  word-break: break-all;
}
</style>
