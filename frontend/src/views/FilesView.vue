<script setup>
import { onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { Download, Delete } from '@element-plus/icons-vue'
import { api } from '../api/client'
import { fmtSize, fmtTime } from '../utils/format'

const tab = ref('image')
const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const kind = tab.value === 'image' ? 'image' : tab.value
    const data = await api.get(`/api/files?kind=${kind}&page=${page.value}&size=20`)
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function switchTab() {
  page.value = 1
  load()
}

async function remove(item) {
  try {
    await ElMessageBox.confirm(`确定删除「${item.filename}」吗？`, '删除文件', { type: 'warning' })
  } catch {
    return
  }
  await api.del(`/api/files/${item.id}`)
  load()
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h2 class="page-title">我的文件</h2>
    <el-tabs v-model="tab" @tab-change="switchTab">
      <el-tab-pane label="图片" name="image" />
      <el-tab-pane label="PPT" name="ppt" />
      <el-tab-pane label="PSD" name="psd" />
    </el-tabs>
    <div v-loading="loading">
      <el-empty v-if="!items.length" description="暂无文件" />
      <template v-else>
        <div v-if="tab === 'image'" class="file-grid">
          <div v-for="f in items" :key="f.id" class="file-cell">
            <el-image :src="f.url" fit="cover" style="width: 100%; aspect-ratio: 1" :preview-src-list="[f.url]" preview-teleported />
            <div class="file-meta">
              <div class="text-muted" style="font-size: 12px">{{ fmtSize(f.size) }} · {{ fmtTime(f.created_at) }}</div>
              <div>
                <a :href="f.url" download style="margin-right: 10px"><el-icon><Download /></el-icon></a>
                <el-icon style="cursor: pointer" @click="remove(f)"><Delete /></el-icon>
              </div>
            </div>
          </div>
        </div>
        <el-table v-else :data="items">
          <el-table-column prop="filename" label="文件名" min-width="200" show-overflow-tooltip />
          <el-table-column label="大小" width="110">
            <template #default="{ row }">{{ fmtSize(row.size) }}</template>
          </el-table-column>
          <el-table-column label="创建时间" width="160">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="140">
            <template #default="{ row }">
              <el-button size="small" type="primary" text :icon="Download" tag="a" :href="row.url" download>下载</el-button>
              <el-button size="small" type="danger" text :icon="Delete" @click="remove(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination
          v-if="total > 20"
          layout="total, prev, pager, next"
          :total="total"
          :page-size="20"
          :current-page="page"
          style="margin-top: 16px; justify-content: center"
          @current-change="(p) => { page = p; load() }"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.file-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 14px;
}
.file-cell {
  border: 1px solid var(--ps-border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--ps-surface);
  transition: transform 0.22s ease, box-shadow 0.22s ease;
}
.file-cell:hover {
  transform: translateY(-3px);
  box-shadow: 0 8px 20px rgba(120, 96, 64, 0.14);
}
.file-cell :deep(.el-image__inner) {
  transition: transform 0.3s ease;
}
.file-cell:hover :deep(.el-image__inner) {
  transform: scale(1.03);
}
.file-meta {
  padding: 8px;
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.file-meta a {
  color: inherit;
}
</style>
