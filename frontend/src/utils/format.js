import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'

dayjs.extend(utc)

export function fmtTime(v) {
  if (!v) return '-'
  const s = String(v)
  // 无时区标记的时间串（SQLite server_default 产物）按 UTC 解析再转本地；
  // 带时区（PostgreSQL timestamptz / ISO Z）直接解析。
  const hasTz = /([zZ]|[+-]\d{2}:?\d{2})$/.test(s)
  return (hasTz ? dayjs(s) : dayjs.utc(s).local()).format('YYYY-MM-DD HH:mm')
}

export function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

// GB 级容量友好版（磁盘 / 系统存储卡片用）
export function fmtBytes(n) {
  if (n === null || n === undefined) return '—'
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' GB'
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB'
  if (n >= 1024) return (n / 1024).toFixed(1) + ' KB'
  return n + ' B'
}

export function quotaText(q) {
  if (!q) return '-'
  if (q.total < 0) return '不限'
  return `${Math.max(q.total - q.used, 0)} / ${q.total}`
}

export function copyText(text) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
  const ta = document.createElement('textarea')
  ta.value = text
  document.body.appendChild(ta)
  ta.select()
  document.execCommand('copy')
  ta.remove()
  return Promise.resolve()
}
