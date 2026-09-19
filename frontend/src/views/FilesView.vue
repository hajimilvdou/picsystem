<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Delete, EditPen, PriceTag } from '@element-plus/icons-vue'
import { api } from '../api/client'
import { fmtSize, fmtTime } from '../utils/format'
import { useThumbFallback } from '../utils/thumbs'

const router = useRouter()
const { thumbSrc, onImageError } = useThumbFallback()

const tab = ref('image')
const items = ref([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)

// 图库标签
const tagFilter = ref('')
const tagStats = ref([])
const tagDialogVisible = ref(false)
const tagSaving = ref(false)
const tagTarget = ref(null)
const tagDraft = ref([])

function kindParam() {
  return tab.value === 'image' ? 'image' : tab.value
}

async function load() {
  loading.value = true
  try {
    const params = new URLSearchParams({ kind: kindParam(), page: String(page.value), size: '20' })
    if (tagFilter.value) params.set('tag', tagFilter.value)
    const data = await api.get(`/api/files?${params}`)
    items.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

async function loadTags() {
  try {
    const data = await api.get(`/api/files/tags?kind=${kindParam()}`)
    tagStats.value = data.items || []
  } catch {
    tagStats.value = []
  }
}

function switchTab() {
  page.value = 1
  tagFilter.value = ''
  load()
  loadTags()
}

function pickTag(tag) {
  tagFilter.value = tagFilter.value === tag ? '' : tag
  page.value = 1
  load()
}

function openTagDialog(item) {
  tagTarget.value = item
  tagDraft.value = [...(item.tags || [])]
  tagDialogVisible.value = true
}

async function saveTags() {
  if (!tagTarget.value) return
  tagSaving.value = true
  try {
    const data = await api.patch(`/api/files/${tagTarget.value.id}`, { tags: tagDraft.value })
    const updated = data.tags || []
    const row = items.value.find((f) => f.id === tagTarget.value.id)
    if (row) row.tags = updated
    tagDialogVisible.value = false
    ElMessage.success(updated.length ? `标签已保存：${updated.join('、')}` : '标签已清空')
    await loadTags()
    if (tagFilter.value && !updated.includes(tagFilter.value)) await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    tagSaving.value = false
  }
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

/** 把这张图带到绘图页做图生图 / 局部编辑（绘图页读取 ?edit=<fileId> 后自动带入）。 */
function editInDraw(item) {
  router.push({ path: '/draw', query: { edit: String(item.id) } })
}

onMounted(() => {
  load()
  loadTags()
})
</script>

<template>
  <div class="page">
    <h2 class="page-title">我的文件</h2>
    <el-tabs v-model="tab" @tab-change="switchTab">
      <el-tab-pane label="图片" name="image" />
      <el-tab-pane label="PPT" name="ppt" />
      <el-tab-pane label="PSD" name="psd" />
    </el-tabs>
    <div class="tag-bar">
      <span class="text-muted" style="font-size: 13px">标签筛选</span>
      <el-button v-if="tagStats.length" size="small" :type="tagFilter ? 'default' : 'primary'" @click="pickTag('')">
        全部
      </el-button>
      <el-button
        v-for="t in tagStats"
        :key="t.tag"
        size="small"
        :type="tagFilter === t.tag ? 'primary' : 'default'"
        @click="pickTag(t.tag)"
      >
        {{ t.tag }} ({{ t.count }})
      </el-button>
      <span v-if="!tagStats.length" class="text-muted" style="font-size: 12px">
        还没有标签，点图片右下角的标签图标即可添加
      </span>
    </div>
    <div v-loading="loading">
      <el-empty v-if="!items.length" :description="tagFilter ? `没有带「${tagFilter}」标签的文件` : '暂无文件'" />
      <template v-else>
        <div v-if="tab === 'image'" class="file-grid">
          <div v-for="f in items" :key="f.id" class="file-cell">
            <el-image
              :src="thumbSrc(f)"
              fit="cover"
              lazy
              style="width: 100%; aspect-ratio: 1"
              :preview-src-list="[f.url]"
              preview-teleported
              @error="onImageError(f)"
            />
            <div class="file-meta">
              <div v-if="f.tags && f.tags.length" class="file-tags">
                <el-tag
                  v-for="t in f.tags"
                  :key="t"
                  size="small"
                  :type="tagFilter === t ? 'primary' : 'info'"
                  effect="plain"
                  style="cursor: pointer"
                  @click="pickTag(t)"
                >
                  {{ t }}
                </el-tag>
              </div>
              <div class="text-muted" style="font-size: 12px">{{ fmtSize(f.size) }} · {{ fmtTime(f.created_at) }}</div>
              <div>
                <el-icon style="cursor: pointer; margin-right: 10px" title="设置标签" @click="openTagDialog(f)"><PriceTag /></el-icon>
                <el-icon style="cursor: pointer; margin-right: 10px" title="去绘图编辑" @click="editInDraw(f)"><EditPen /></el-icon>
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
          <el-table-column label="标签" min-width="150">
            <template #default="{ row }">
              <el-tag
                v-for="t in (row.tags || [])"
                :key="t"
                size="small"
                effect="plain"
                :type="tagFilter === t ? 'primary' : 'info'"
                style="cursor: pointer; margin-right: 4px"
                @click="pickTag(t)"
              >
                {{ t }}
              </el-tag>
              <span v-if="!row.tags || !row.tags.length" class="text-muted" style="font-size: 12px">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="220">
            <template #default="{ row }">
              <el-button size="small" type="primary" text :icon="PriceTag" @click="openTagDialog(row)">标签</el-button>
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

    <el-dialog v-model="tagDialogVisible" title="设置标签" width="min(460px, 94vw)">
      <div class="text-muted" style="font-size: 12px; margin-bottom: 8px">
        最多 10 个标签，每个不超过 24 字符；输入后回车即可新增，保存时整组覆盖。
      </div>
      <el-select
        v-model="tagDraft"
        multiple
        filterable
        allow-create
        default-first-option
        :reserve-keyword="false"
        placeholder="输入标签后回车"
        style="width: 100%"
      >
        <el-option v-for="t in tagStats" :key="t.tag" :label="`${t.tag}（${t.count}）`" :value="t.tag" />
      </el-select>
      <template #footer>
        <el-button @click="tagDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="tagSaving" @click="saveTags">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tag-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 14px;
}

.file-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  margin-bottom: 6px;
}

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
