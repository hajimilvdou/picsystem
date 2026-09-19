<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Delete, Plus } from '@element-plus/icons-vue'
import { api } from '../api/client'
import { useAuthStore } from '../stores/auth'
import { fmtSize, fmtTime } from '../utils/format'

const auth = useAuthStore()
const tab = ref('gen')
const form = ref({ prompt: '', model: 'gpt-image-2', n: 1, size: 'auto', quality: 'auto' })
const imageModels = ref(['gpt-image-2'])
const sizeOptions = ['auto', '1024x1024', '1536x1024', '1024x1536']
const qualityOptions = ['auto', 'high', 'medium', 'low']
const refFiles = ref([])
const generating = ref(false)
const results = ref([])

const history = ref([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyLoading = ref(false)

async function loadModels() {
  try {
    const data = await api.get('/api/models')
    const ids = (data.data || []).map((m) => m.id).filter(Boolean)
    const img = ids.filter((id) => /image|dall|flux|sd/i.test(id))
    if (img.length) imageModels.value = img
    if (!imageModels.value.includes(form.value.model)) form.value.model = imageModels.value[0] || 'gpt-image-2'
  } catch {
    /* 默认 */
  }
}

async function loadHistory() {
  historyLoading.value = true
  try {
    const data = await api.get(`/api/files?kind=image&page=${historyPage.value}&size=12`)
    history.value = data.items
    historyTotal.value = data.total
  } finally {
    historyLoading.value = false
  }
}

function addRefFiles(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  for (const f of files) {
    if (refFiles.value.length >= 4) {
      ElMessage.warning('参考图最多 4 张')
      break
    }
    if (!f.type.startsWith('image/')) {
      ElMessage.warning('仅支持图片文件')
      continue
    }
    refFiles.value.push(f)
  }
}

async function generate() {
  if (!form.value.prompt.trim()) {
    ElMessage.warning('请输入提示词')
    return
  }
  generating.value = true
  try {
    let data
    if (tab.value === 'gen') {
      data = await api.post('/api/images/generations', {
        ...form.value,
        size: form.value.size === 'auto' ? null : form.value.size,
      })
    } else {
      if (!refFiles.value.length) {
        ElMessage.warning('请上传至少一张参考图')
        generating.value = false
        return
      }
      const fd = new FormData()
      fd.append('prompt', form.value.prompt)
      fd.append('model', form.value.model)
      fd.append('n', String(form.value.n))
      if (form.value.size !== 'auto') fd.append('size', form.value.size)
      fd.append('quality', form.value.quality)
      for (const f of refFiles.value) fd.append('images', f)
      data = await api.postForm('/api/images/edits', fd)
    }
    results.value = data.files
    ElMessage.success(`已生成 ${data.files.length} 张图片`)
    auth.fetchMe(true)
    historyPage.value = 1
    loadHistory()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    generating.value = false
  }
}

async function removeFile(item, fromResults) {
  try {
    await ElMessageBox.confirm('确定删除这张图片吗？', '删除', { type: 'warning' })
  } catch {
    return
  }
  await api.del(`/api/files/${item.id}`)
  if (fromResults) results.value = results.value.filter((f) => f.id !== item.id)
  loadHistory()
}

onMounted(() => {
  loadModels()
  loadHistory()
})
</script>

<template>
  <div class="page">
    <h2 class="page-title">绘图</h2>
    <el-row :gutter="20">
      <el-col :xs="24" :md="9">
        <el-card>
          <el-tabs v-model="tab">
            <el-tab-pane label="文生图" name="gen" />
            <el-tab-pane label="图生图" name="edit" />
          </el-tabs>
          <el-form label-position="top">
            <el-form-item label="提示词">
              <el-input
                v-model="form.prompt"
                type="textarea"
                :rows="4"
                maxlength="4000"
                show-word-limit
                placeholder="描述你想要的画面，例如：一只漂浮在太空里的猫，电影感光影"
              />
            </el-form-item>
            <el-form-item v-if="tab === 'edit'" label="参考图（最多 4 张）">
              <div>
                <el-button :icon="Plus" @click="$refs.fileInput.click()">添加图片</el-button>
                <input ref="fileInput" type="file" accept="image/*" multiple hidden @change="addRefFiles" />
                <div style="display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px">
                  <div v-for="(f, i) in refFiles" :key="i" style="position: relative">
                    <img :src="URL.createObjectURL(f)" style="width: 64px; height: 64px; object-fit: cover; border-radius: 6px" />
                    <el-icon
                      style="position: absolute; top: -6px; right: -6px; background: #fff; border-radius: 50%; cursor: pointer"
                      @click="refFiles.splice(i, 1)"
                    >
                      <Delete />
                    </el-icon>
                  </div>
                </div>
              </div>
            </el-form-item>
            <el-form-item label="模型">
              <el-select v-model="form.model" style="width: 100%">
                <el-option v-for="m in imageModels" :key="m" :label="m" :value="m" />
              </el-select>
            </el-form-item>
            <el-form-item :label="`数量（${form.n} 张，消耗 ${form.n} 次）`">
              <el-slider v-model="form.n" :min="1" :max="4" show-stops />
            </el-form-item>
            <el-row :gutter="10">
              <el-col :span="12">
                <el-form-item label="尺寸">
                  <el-select v-model="form.size" style="width: 100%">
                    <el-option v-for="s in sizeOptions" :key="s" :label="s === 'auto' ? '自动' : s" :value="s" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="质量">
                  <el-select v-model="form.quality" style="width: 100%">
                    <el-option v-for="q in qualityOptions" :key="q" :label="q" :value="q" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-button type="primary" style="width: 100%" :loading="generating" @click="generate">
              {{ generating ? '生成中（可能需要 1-2 分钟）…' : '开始生成' }}
            </el-button>
            <div class="text-muted" style="margin-top: 8px">绘图剩余次数：{{ auth.quotaText('image') }}</div>
          </el-form>
        </el-card>
      </el-col>

      <el-col :xs="24" :md="15">
        <el-card v-if="results.length" style="margin-bottom: 16px">
          <template #header>本次生成</template>
          <div class="img-grid">
            <div v-for="f in results" :key="f.id" class="img-cell">
              <el-image :src="f.url" fit="cover" class="img-thumb" :preview-src-list="[f.url]" preview-teleported />
              <div class="img-actions">
                <a :href="f.url" download><el-icon title="下载"><Download /></el-icon></a>
                <el-icon title="删除" @click="removeFile(f, true)"><Delete /></el-icon>
              </div>
            </div>
          </div>
        </el-card>

        <el-card v-loading="historyLoading">
          <template #header>我的图库</template>
          <el-empty v-if="!history.length" description="还没有生成过图片" />
          <div v-else class="img-grid">
            <div v-for="f in history" :key="f.id" class="img-cell">
              <el-image :src="f.url" fit="cover" class="img-thumb" :preview-src-list="[f.url]" preview-teleported />
              <el-tooltip :content="`${f.prompt || '无提示词'} · ${fmtSize(f.size)} · ${fmtTime(f.created_at)}`">
                <div class="img-actions">
                  <a :href="f.url" download><el-icon><Download /></el-icon></a>
                  <el-icon @click="removeFile(f, false)"><Delete /></el-icon>
                </div>
              </el-tooltip>
            </div>
          </div>
          <el-pagination
            v-if="historyTotal > 12"
            layout="prev, pager, next"
            :total="historyTotal"
            :page-size="12"
            :current-page="historyPage"
            style="margin-top: 12px; justify-content: center"
            @current-change="(p) => { historyPage = p; loadHistory() }"
          />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.img-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.img-cell {
  position: relative;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--ps-border);
  transition: transform 0.22s ease, box-shadow 0.22s ease;
}
.img-cell:hover {
  transform: translateY(-3px);
  box-shadow: 0 8px 20px rgba(120, 96, 64, 0.14);
}
.img-thumb {
  width: 100%;
  aspect-ratio: 1;
  display: block;
  transition: transform 0.3s ease;
}
.img-cell:hover .img-thumb {
  transform: scale(1.03);
}
.img-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 6px 8px;
  background: var(--ps-surface);
  font-size: 16px;
}
.img-actions a {
  color: inherit;
}
</style>
