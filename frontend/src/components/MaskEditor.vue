<script setup>
/**
 * 局部编辑遮罩画布：在参考图上涂抹出「需要重绘」的区域。
 *
 * 遮罩导出为 PNG：白 = 重绘区域，黑 = 保持原样，与上游 /v1/images/edits 的 mask 口径一致。
 * 显示上用 mix-blend-mode: screen，让黑色底完全透出原图，只看到白色的涂抹笔迹。
 */
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  imageUrl: { type: String, default: '' },
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['change'])

const MAX_EDGE = 4096

const wrapRef = ref(null)
const canvasRef = ref(null)
const brush = ref(48)
const erasing = ref(false)
const ready = ref(false)
const painted = ref(false)
const loadError = ref('')

let painting = false
let lastPoint = null
let imageEl = null

function ctx2d() {
  return canvasRef.value?.getContext('2d') ?? null
}

function fillBase(color = '#000') {
  const ctx = ctx2d()
  const canvas = canvasRef.value
  if (!ctx || !canvas) return
  ctx.globalCompositeOperation = 'source-over'
  ctx.fillStyle = color
  ctx.fillRect(0, 0, canvas.width, canvas.height)
}

function reset() {
  fillBase('#000')
  painted.value = false
  emit('change', null)
}

function loadImage(url) {
  ready.value = false
  loadError.value = ''
  painted.value = false
  emit('change', null)
  if (imageEl) {
    imageEl.onload = null
    imageEl.onerror = null
    imageEl = null
  }
  if (!url) return

  const img = new Image()
  imageEl = img
  img.onload = () => {
    const canvas = canvasRef.value
    if (!canvas) return
    let { naturalWidth: w, naturalHeight: h } = img
    const scale = Math.min(1, MAX_EDGE / Math.max(w, h))
    canvas.width = Math.max(1, Math.round(w * scale))
    canvas.height = Math.max(1, Math.round(h * scale))
    reset()
    ready.value = true
  }
  img.onerror = () => {
    loadError.value = '参考图加载失败，无法涂抹遮罩'
  }
  img.src = url
}

function pointOf(event) {
  const canvas = canvasRef.value
  if (!canvas) return null
  const rect = canvas.getBoundingClientRect()
  if (!rect.width || !rect.height) return null
  return {
    x: (event.clientX - rect.left) * (canvas.width / rect.width),
    y: (event.clientY - rect.top) * (canvas.height / rect.height),
  }
}

function strokeBetween(from, to) {
  const ctx = ctx2d()
  if (!ctx) return
  ctx.globalCompositeOperation = 'source-over'
  ctx.strokeStyle = erasing.value ? '#000' : '#fff'
  ctx.lineWidth = brush.value
  ctx.lineCap = 'round'
  ctx.lineJoin = 'round'
  ctx.beginPath()
  ctx.moveTo(from.x, from.y)
  ctx.lineTo(to.x, to.y)
  ctx.stroke()
  painted.value = true
}

function onPointerDown(event) {
  if (props.disabled || !ready.value) return
  const point = pointOf(event)
  if (!point) return
  painting = true
  lastPoint = point
  canvasRef.value?.setPointerCapture?.(event.pointerId)
  // 单击也要留下一个点
  strokeBetween(point, { x: point.x + 0.01, y: point.y })
}

function onPointerMove(event) {
  if (!painting) return
  const point = pointOf(event)
  if (!point || !lastPoint) return
  strokeBetween(lastPoint, point)
  lastPoint = point
}

function onPointerUp(event) {
  if (!painting) return
  painting = false
  lastPoint = null
  canvasRef.value?.releasePointerCapture?.(event.pointerId)
  void syncMask()
}

async function syncMask() {
  const canvas = canvasRef.value
  if (!canvas || !painted.value) {
    emit('change', null)
    return
  }
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'))
  emit('change', blob ?? null)
}

async function getMaskFile() {
  const canvas = canvasRef.value
  if (!canvas || !painted.value) return null
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'))
  return blob ? new File([blob], 'mask.png', { type: 'image/png' }) : null
}

defineExpose({ getMaskFile, reset })

watch(() => props.imageUrl, loadImage, { immediate: true })
watch(() => props.disabled, (value) => {
  if (value) painting = false
})
onBeforeUnmount(() => {
  if (imageEl) {
    imageEl.onload = null
    imageEl.onerror = null
  }
})
</script>

<template>
  <div class="mask-editor">
    <div class="mask-editor__hint text-muted">
      在图上涂抹需要重绘的区域（白色笔迹）。未涂抹处保持原样；不涂 = 整图编辑。
    </div>
    <div ref="wrapRef" class="mask-editor__stage">
      <img v-if="imageUrl" :src="imageUrl" alt="参考图" class="mask-editor__image" />
      <canvas
        ref="canvasRef"
        class="mask-editor__canvas"
        :class="{ 'is-disabled': disabled || !ready }"
        @pointerdown="onPointerDown"
        @pointermove="onPointerMove"
        @pointerup="onPointerUp"
        @pointerleave="onPointerUp"
        @pointercancel="onPointerUp"
      />
      <div v-if="!imageUrl" class="mask-editor__empty text-muted">先选择参考图，再涂抹重绘区域</div>
    </div>
    <div v-if="loadError" class="mask-editor__error">{{ loadError }}</div>
    <div class="mask-editor__tools">
      <span class="text-muted" style="font-size: 12px">笔刷</span>
      <el-slider v-model="brush" :min="8" :max="200" :disabled="disabled || !ready" style="width: 140px" />
      <el-button size="small" :type="erasing ? 'primary' : 'default'" :disabled="disabled || !ready" @click="erasing = !erasing">
        {{ erasing ? '橡皮擦（开）' : '橡皮擦' }}
      </el-button>
      <el-button size="small" :disabled="disabled || !ready || !painted" @click="reset">清空遮罩</el-button>
      <el-tag v-if="painted" size="small" type="success">已选中重绘区域</el-tag>
    </div>
  </div>
</template>

<style scoped>
.mask-editor {
  width: 100%;
}

.mask-editor__hint {
  font-size: 12px;
  margin-bottom: 6px;
}

.mask-editor__stage {
  position: relative;
  width: 100%;
  border: 1px solid var(--el-border-color, #dcdfe6);
  border-radius: 6px;
  overflow: hidden;
  background: var(--el-fill-color-lighter, #fafafa);
  touch-action: none;
}

.mask-editor__image {
  display: block;
  width: 100%;
}

.mask-editor__canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  cursor: crosshair;
  mix-blend-mode: screen;
}

.mask-editor__canvas.is-disabled {
  cursor: not-allowed;
}

.mask-editor__empty {
  padding: 24px;
  text-align: center;
  font-size: 12px;
}

.mask-editor__error {
  color: var(--el-color-danger, #f56c6c);
  font-size: 12px;
  margin-top: 6px;
}

.mask-editor__tools {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 8px;
}
</style>
