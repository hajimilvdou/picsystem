/**
 * 缩略图取用与失败回退。
 *
 * 后端只给图片类返回 `thumb_url`；若缩略图不可用（文件损坏、超过解码上限等）接口会 404，
 * 此时自动回退到原图，避免图库里出现破图。
 */
import { ref } from 'vue'

export function useThumbFallback() {
  const failed = ref({})

  function thumbSrc(item) {
    if (!item) return ''
    if (item.thumb_url && !failed.value[item.id]) return item.thumb_url
    return item.url || ''
  }

  function onImageError(item) {
    if (item?.thumb_url && !failed.value[item.id]) {
      failed.value = { ...failed.value, [item.id]: true }
    }
  }

  return { thumbSrc, onImageError }
}
