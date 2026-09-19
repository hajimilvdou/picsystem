<script setup>
/**
 * 已选图片的缩略图条：每张一个小缩略图 + 右上角删除键，末尾是添加入口。
 *
 * 约定：父组件通过 `v-model` 传入 File[]，并且**每次都用新数组赋值**
 * （本组件靠数组引用变化刷新预览 URL），卸载时自动回收所有 objectURL。
 */
import { onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Delete, Plus } from '@element-plus/icons-vue'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  max: { type: Number, default: 4 },
  buttonText: { type: String, default: '添加图片' },
  thumbSize: { type: Number, default: 64 },
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const inputRef = ref(null)
const previews = ref([])

function syncPreviews(files) {
  previews.value.forEach((item) => URL.revokeObjectURL(item.url))
  previews.value = (files || []).map((file) => ({ file, url: URL.createObjectURL(file) }))
}

watch(() => props.modelValue, syncPreviews, { immediate: true })
onBeforeUnmount(() => previews.value.forEach((item) => URL.revokeObjectURL(item.url)))

function pick() {
  if (props.disabled) return
  inputRef.value?.click()
}

function onFiles(event) {
  const incoming = Array.from(event.target.files || [])
  event.target.value = ''
  const next = [...props.modelValue]
  for (const file of incoming) {
    if (!file.type.startsWith('image/')) {
      ElMessage.warning(`已跳过非图片文件：${file.name}`)
      continue
    }
    if (next.length >= props.max) {
      ElMessage.warning(`最多 ${props.max} 张图片`)
      break
    }
    next.push(file)
  }
  emit('update:modelValue', next)
}

function remove(index) {
  const next = [...props.modelValue]
  next.splice(index, 1)
  emit('update:modelValue', next)
}

defineExpose({ clear: () => emit('update:modelValue', []) })
</script>

<template>
  <div class="picker">
    <div class="picker__row">
      <div
        v-for="(item, index) in previews"
        :key="item.url"
        class="picker__thumb"
        :style="{ width: `${thumbSize}px`, height: `${thumbSize}px` }"
      >
        <img :src="item.url" :alt="item.file.name" :title="item.file.name" />
        <button
          class="picker__remove"
          type="button"
          title="移除这张图片"
          :disabled="disabled"
          @click="remove(index)"
        >
          <el-icon><Delete /></el-icon>
        </button>
      </div>
      <button
        v-if="modelValue.length < max"
        class="picker__add"
        type="button"
        :style="{ width: `${thumbSize}px`, height: `${thumbSize}px` }"
        :disabled="disabled"
        @click="pick"
      >
        <el-icon><Plus /></el-icon>
        <span>{{ buttonText }}</span>
      </button>
    </div>
    <input ref="inputRef" type="file" accept="image/*" multiple hidden @change="onFiles" />
    <div class="picker__meta text-muted">{{ modelValue.length }} / {{ max }}</div>
  </div>
</template>

<style scoped>
.picker__row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.picker__thumb {
  position: relative;
  border: 1px solid var(--el-border-color, #dcdfe6);
  border-radius: 8px;
  overflow: hidden;
  background: var(--el-fill-color-lighter, #fafafa);
}

.picker__thumb img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.picker__remove {
  position: absolute;
  top: 2px;
  right: 2px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 11px;
  cursor: pointer;
  transition: background 0.15s;
}

.picker__remove:hover:not(:disabled) {
  background: var(--el-color-danger, #f56c6c);
}

.picker__remove:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.picker__add {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  border: 1px dashed var(--el-border-color, #dcdfe6);
  border-radius: 8px;
  background: transparent;
  color: var(--el-text-color-secondary, #909399);
  font-size: 11px;
  line-height: 1.2;
  cursor: pointer;
}

.picker__add:hover:not(:disabled) {
  border-color: var(--el-color-primary, #409eff);
  color: var(--el-color-primary, #409eff);
}

.picker__add:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.picker__meta {
  font-size: 12px;
  margin-top: 4px;
}
</style>
