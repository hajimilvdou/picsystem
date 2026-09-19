<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, Search, Upload } from '@element-plus/icons-vue'
import { api } from '../../api/client'
import {
  parseSessionJsonPayload,
  parseTokenLines,
  readAccountFiles,
} from '../../utils/accountImport'

/* ------------------------------------------------------------------ 常量 */

// 与上游 chatgpt2api v3.2.3 的导入模式一一对应（见 docs/UPSTREAM-ADMIN-API.md）
const IMPORT_MODES = [
  { value: 'access_token', label: '导入 Access Token' },
  { value: 'session_json', label: '导入 Session JSON' },
  { value: 'backup_json', label: '导入完整备份文件' },
  { value: 'cpa_json', label: '导入 CPA JSON 文件' },
  { value: 'sub2api_json', label: '导入 Sub2API JSON 文件' },
  { value: 'oauth_login', label: 'OAuth 登录已有账号' },
  { value: 'remote_cpa', label: '从远程 CPA 导入' },
  { value: 'sub2api', label: '从 Sub2API 导入' },
]

const STATUS_OPTIONS = [
  { label: '全部', value: 'all' },
  { label: '正常', value: 'normal' },
  { label: '限流', value: 'limited' },
  { label: '异常', value: 'abnormal' },
  { label: '禁用', value: 'disabled' },
]

const UNGROUPED = '__ungrouped__'
const PRESERVE_GROUP = '__preserve__'

/* ------------------------------------------------------------------ 列表 */

const items = ref([])
const total = ref(0)
const page = ref(1)
const keyword = ref('')
const statusFilter = ref('all')
const groupFilter = ref('all')
const loading = ref(false)
const loadError = ref('')
const selectedRows = ref([])
const accountGroups = ref([])
const syncing = ref(false)

const selectedIds = computed(() => selectedRows.value.map((row) => row.id).filter(Boolean))

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: '50',
      keyword: keyword.value,
      status: statusFilter.value,
      group_id: groupFilter.value,
    })
    const data = await api.get(`/api/admin/upstream-accounts?${params}`)
    items.value = data.items || []
    total.value = data.total || 0
    selectedRows.value = []
  } catch (e) {
    loadError.value = e.message
  } finally {
    loading.value = false
  }
}

async function loadGroups() {
  try {
    const data = await api.get('/api/admin/upstream-accounts/groups')
    accountGroups.value = data.groups || []
  } catch {
    accountGroups.value = []
  }
}

/* ------------------------------------------------------------------ 进度面板 */

const run = reactive({
  visible: false,
  title: '',
  stage: '',
  percent: 0,
  indeterminate: false,
  error: '',
  result: null,
  events: [],
})

const progressStatus = computed(() => {
  if (run.error) return 'exception'
  return run.percent === 100 ? 'success' : ''
})

let pollTimer = null

function clearPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

function openRun(title) {
  run.visible = true
  run.title = title
  run.stage = '读取凭据'
  run.percent = 0
  run.indeterminate = true
  run.error = ''
  run.result = null
  run.events = []
}

function setStage(stage) {
  run.stage = stage
}

function finishRun(payload, fallback = {}) {
  const result = {
    added: Number(payload?.added ?? fallback.added ?? 0),
    skipped: Number(payload?.skipped ?? fallback.skipped ?? 0),
    synced: Number(payload?.synced ?? payload?.updated ?? fallback.synced ?? 0),
    failed: Number(payload?.failed ?? fallback.failed ?? 0),
    removed: Number(payload?.removed ?? 0),
  }
  if (!result.failed && Array.isArray(payload?.errors)) result.failed = payload.errors.length
  run.result = result
  run.events = Array.isArray(payload?.events) ? payload.events.slice(0, 200) : []
  run.stage = payload?.message || '完成'
  run.percent = 100
  run.indeterminate = false
}

function failRun(message) {
  run.error = message
  run.stage = '失败'
  run.indeterminate = false
}

function closeRun() {
  clearPoll()
  run.visible = false
}

/* ------------------------------------------------------------------ 导入：通用 */

const importVisible = ref(false)
const importMode = ref('access_token')
const importBusy = ref(false)
const importTargetGroup = ref(PRESERVE_GROUP)

const targetGroupId = computed(() =>
  importTargetGroup.value === PRESERVE_GROUP ? null : importTargetGroup.value
)

// 远程来源按需加载，避免上游版本较旧时一开弹窗就报错
function ensureSources(mode) {
  if (mode === 'remote_cpa' && !cpa.pools.length) loadCpaPools()
  if (mode === 'sub2api' && !sub.servers.length) loadSub2Servers()
}

function openImport(mode = 'access_token') {
  importVisible.value = true
  importMode.value = mode
  importTargetGroup.value = PRESERVE_GROUP
  loadGroups()
  ensureSources(mode)
}

function closeImport() {
  if (importBusy.value) return
  importVisible.value = false
}

function setImportMode(mode) {
  if (importBusy.value) return
  importMode.value = mode
  ensureSources(mode)
}

async function confirmImport(title, message) {
  try {
    await ElMessageBox.confirm(message, title, { type: 'warning', confirmButtonText: '确认导入' })
    return true
  } catch {
    return false
  }
}

/** 本地凭据批量导入：tokens 与 accounts 二选一或同时使用。 */
async function submitLocalImport({ tokens = [], accounts = [], restore = false, title, syncAfterImport = true }) {
  importBusy.value = true
  importVisible.value = false
  openRun(title)
  try {
    setStage('保存账号')
    const payload = await api.post('/api/admin/upstream-accounts', {
      tokens,
      accounts,
      restore,
      sync_after_import: syncAfterImport,
      return_items: false,
      target_group_id: targetGroupId.value,
    })
    setStage(syncAfterImport ? '同步账号与额度' : '完成')
    finishRun(payload)
    if (payload?.errors?.length) {
      await promptCleanup(payload.updated_ids || [], payload.errors.length)
    }
    await Promise.all([load(), loadGroups()])
  } catch (e) {
    failRun(e.message)
  } finally {
    importBusy.value = false
  }
}

/** 导入后若同步失败，提示是否移除「本次确认失效」的账号（等价上游 import-cleanup）。 */
async function promptCleanup(accountIds, errorCount) {
  if (!errorCount || !accountIds.length) return
  let preview
  try {
    preview = await api.post('/api/admin/upstream-accounts/import-cleanup', { account_ids: accountIds })
  } catch (e) {
    ElMessage.warning(`检查本次确认失效账号失败，已先保留：${e.message}`)
    return
  }
  const abnormal = Number(preview.abnormal || 0)
  if (!abnormal) {
    ElMessage.info('本次导入有同步失败，但没有确认失效账号；暂时检测失败的账号会保留')
    return
  }
  try {
    await ElMessageBox.confirm(
      `本次导入同步失败 ${errorCount} 个，其中 ${abnormal} 个账号鉴权已确认失效，是否直接删除？\n\n只会删除本次导入且已确认失效的账号。`,
      '移除本次确认失效账号？',
      { type: 'warning', confirmButtonText: `删除 ${abnormal} 个`, cancelButtonText: '先保留' }
    )
  } catch {
    return
  }
  try {
    const result = await api.post('/api/admin/upstream-accounts/import-cleanup', {
      account_ids: accountIds,
      remove: true,
    })
    ElMessage.success(`已移除 ${result.removed ?? abnormal} 个确认失效账号`)
    await load()
  } catch (e) {
    ElMessage.error(`移除失败：${e.message}`)
  }
}

/* ------------------------------------------------------------------ 导入：Access Token */

const tokenText = ref('')
const tokenFileInput = ref(null)

function pickTokenFile() {
  tokenFileInput.value?.click()
}

async function onTokenFileChange(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  try {
    tokenText.value = await file.text()
  } catch {
    ElMessage.error('读取文件失败')
    return
  }
  await importTokenText()
}

async function importTokenText() {
  const tokens = parseTokenLines(tokenText.value)
  if (!tokens.length) {
    ElMessage.warning('请粘贴至少一个 Access Token（每行一个）')
    return
  }
  if (!(await confirmImport('导入 Access Token', `即将导入 ${tokens.length} 个 Access Token，导入后同步账号与额度。是否继续？`))) return
  tokenText.value = ''
  await submitLocalImport({ tokens, title: '导入 Access Token' })
}

/* ------------------------------------------------------------------ 导入：Session JSON */

const sessionText = ref('')

async function importSessionJson() {
  let payload
  try {
    payload = parseSessionJsonPayload(sessionText.value)
  } catch (e) {
    ElMessage.error(e.message)
    return
  }
  if (!(await confirmImport('导入 Session JSON', '即将导入 1 个账号（含 AT/RT/ID Token），导入后同步账号与额度。是否继续？'))) return
  sessionText.value = ''
  await submitLocalImport({ accounts: [payload], title: '导入 Session JSON' })
}

/* ------------------------------------------------------------------ 导入：本地 JSON 文件 */

const jsonFileInput = ref(null)

function pickJsonFiles() {
  jsonFileInput.value?.click()
}

async function onJsonFileChange(event) {
  const files = Array.from(event.target.files || [])
  event.target.value = ''
  if (!files.length) return

  const mode = importMode.value
  const label = {
    backup_json: '完整备份文件',
    cpa_json: 'CPA JSON 文件',
    sub2api_json: 'Sub2API JSON 文件',
  }[mode] || 'JSON 文件'

  const { accounts, errors } = await readAccountFiles(files)
  if (errors.length) {
    ElMessage.error(errors.slice(0, 3).join('；') + (errors.length > 3 ? ` 等 ${errors.length} 条` : ''))
  }
  if (!accounts.length) return

  const restore = mode === 'backup_json'
  const message = restore
    ? `即将读取 ${files.length} 个备份文件，恢复其中 ${accounts.length} 个账号的凭据与配置（不请求远程验证）。是否继续？`
    : `即将读取 ${files.length} 个 ${label}（共 ${accounts.length} 个账号），保存后同步账号与额度。是否继续？`
  if (!(await confirmImport(`导入${label}`, message))) return

  await submitLocalImport({ accounts, restore, title: `导入${label}` })
}

/* ------------------------------------------------------------------ 导入：OAuth 登录 */

const oauth = reactive({
  emailHint: '',
  sessionId: '',
  authorizeUrl: '',
  redirectPrefix: '',
  callback: '',
  busy: false,
})

async function startOAuth() {
  oauth.busy = true
  try {
    const data = await api.post('/api/admin/upstream-accounts/oauth/start', { email_hint: oauth.emailHint })
    oauth.sessionId = data.session_id || ''
    oauth.authorizeUrl = data.authorize_url || ''
    oauth.redirectPrefix = data.redirect_uri_prefix || ''
    oauth.callback = ''
    if (!oauth.sessionId || !oauth.authorizeUrl) throw new Error('上游没有返回完整的 OAuth 授权会话')
    window.open(oauth.authorizeUrl, '_blank', 'noopener,noreferrer')
    ElMessage.success('OAuth 授权链接已生成')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    oauth.busy = false
  }
}

function openAuthorizeUrl() {
  if (oauth.authorizeUrl) window.open(oauth.authorizeUrl, '_blank', 'noopener,noreferrer')
}

async function copyAuthorizeUrl() {
  if (!oauth.authorizeUrl) return
  try {
    await navigator.clipboard.writeText(oauth.authorizeUrl)
    ElMessage.success('授权链接已复制')
  } catch {
    ElMessage.error('复制失败，请手动选中链接')
  }
}

async function finishOAuth() {
  if (!oauth.sessionId) {
    ElMessage.warning('请先生成授权链接')
    return
  }
  if (!oauth.callback.trim()) {
    ElMessage.warning('请粘贴 callback URL 或 code')
    return
  }
  importBusy.value = true
  importVisible.value = false
  openRun('OAuth 登录导入')
  try {
    setStage('保存账号')
    const payload = await api.post('/api/admin/upstream-accounts/oauth/finish', {
      session_id: oauth.sessionId,
      callback: oauth.callback.trim(),
      target_group_id: targetGroupId.value,
    })
    await Promise.all([load(), loadGroups()])
    const accountIds = payload.updated_ids || []
    if (accountIds.length) {
      setStage('同步账号与额度')
      const sync = await api.post('/api/admin/upstream-accounts/sync', { account_ids: accountIds })
      payload.synced = sync.synced ?? payload.synced
      if (Array.isArray(sync.errors)) payload.errors = [...(payload.errors || []), ...sync.errors]
    }
    finishRun(payload)
    oauth.emailHint = ''
    oauth.callback = ''
    oauth.sessionId = ''
    oauth.authorizeUrl = ''
    oauth.redirectPrefix = ''
  } catch (e) {
    failRun(e.message)
  } finally {
    importBusy.value = false
  }
}

/* ------------------------------------------------------------------ 远程 CPA */

const cpa = reactive({
  pools: [],
  poolId: '',
  files: [],
  selected: [],
  busy: false,
  connectVisible: false,
  form: { name: '', base_url: '', secret_key: '' },
})

async function loadCpaPools() {
  try {
    const data = await api.get('/api/admin/upstream-accounts/cpa/pools')
    cpa.pools = data.pools || []
    if (!cpa.pools.some((p) => p.id === cpa.poolId)) cpa.poolId = cpa.pools[0]?.id || ''
    resumeActiveJobs()
  } catch (e) {
    cpa.pools = []
    ElMessage.error(`加载 CPA 连接失败：${e.message}`)
  }
}

async function loadCpaFiles() {
  if (!cpa.poolId) {
    ElMessage.warning('请先选择 CPA 连接')
    return
  }
  cpa.busy = true
  try {
    const data = await api.get(`/api/admin/upstream-accounts/cpa/pools/${cpa.poolId}/files`)
    cpa.files = data.files || []
    cpa.selected = []
  } catch (e) {
    ElMessage.error(`加载远程文件失败：${e.message}`)
  } finally {
    cpa.busy = false
  }
}

function toggleCpaFile(name, checked) {
  const next = new Set(cpa.selected)
  if (checked) next.add(name)
  else next.delete(name)
  cpa.selected = Array.from(next)
}

function selectAllCpaFiles() {
  cpa.selected = cpa.files.map((f) => f.name)
}

function clearCpaSelection() {
  cpa.selected = []
}

async function createCpaPool() {
  if (!cpa.form.base_url.trim() || !cpa.form.secret_key.trim()) {
    ElMessage.warning('请填写 CPA 服务地址与密钥')
    return
  }
  cpa.busy = true
  try {
    const data = await api.post('/api/admin/upstream-accounts/cpa/pools', { ...cpa.form })
    cpa.pools = data.pools || []
    cpa.poolId = data.pool?.id || cpa.poolId
    cpa.connectVisible = false
    cpa.form = { name: '', base_url: '', secret_key: '' }
    ElMessage.success('CPA 连接已保存')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    cpa.busy = false
  }
}

async function deleteCpaPool() {
  const pool = cpa.pools.find((p) => p.id === cpa.poolId)
  if (!pool) return
  try {
    await ElMessageBox.confirm(`确定删除连接「${pool.name || pool.base_url}」吗？`, '删除 CPA 连接', { type: 'warning' })
  } catch {
    return
  }
  try {
    const data = await api.del(`/api/admin/upstream-accounts/cpa/pools/${pool.id}`)
    cpa.pools = data.pools || []
    cpa.poolId = cpa.pools[0]?.id || ''
    cpa.files = []
    cpa.selected = []
    ElMessage.success('连接已删除')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function importFromCpa() {
  if (!cpa.poolId || !cpa.selected.length) return
  if (!(await confirmImport('确认远程 CPA 导入', `即将从该连接导入 ${cpa.selected.length} 个文件里的账号，写入上游号池。是否继续？`))) return
  importBusy.value = true
  importVisible.value = false
  openRun('导入远程 CPA')
  try {
    const start = await api.post(`/api/admin/upstream-accounts/cpa/pools/${cpa.poolId}/import`, {
      names: cpa.selected,
      target_group_id: targetGroupId.value,
    })
    await trackJob(start.import_job, () =>
      api.get(`/api/admin/upstream-accounts/cpa/pools/${cpa.poolId}/import`)
    )
    cpa.selected = []
  } catch (e) {
    failRun(e.message)
  } finally {
    importBusy.value = false
  }
}

/* ------------------------------------------------------------------ 远程 Sub2API */

const sub = reactive({
  servers: [],
  serverId: '',
  groups: [],
  groupId: '',
  accounts: [],
  selected: [],
  byGroups: true,
  collapsed: {},
  busy: false,
  connectVisible: false,
  form: { name: '', base_url: '', email: '', password: '', api_key: '', group_id: '' },
})

const subAccountGroups = computed(() => {
  const map = new Map()
  for (const account of sub.accounts) {
    const groupId = String(account.remote_group_id || '').trim()
    const groupName = String(account.remote_group_name || '').trim()
    const key = groupId || (groupName ? `name:${groupName}` : UNGROUPED)
    const name = groupName || (groupId ? groupId : '未分组账号')
    if (!map.has(key)) map.set(key, { key, groupId, name, accounts: [] })
    map.get(key).accounts.push(account)
  }
  const order = new Map(sub.groups.map((g, index) => [String(g.id), index]))
  return Array.from(map.values()).sort((a, b) => {
    const left = a.groupId ? order.get(a.groupId) ?? 9999 : 10000
    const right = b.groupId ? order.get(b.groupId) ?? 9999 : 10000
    return left !== right ? left - right : a.name.localeCompare(b.name)
  })
})

async function loadSub2Servers() {
  try {
    const data = await api.get('/api/admin/upstream-accounts/sub2api/servers')
    sub.servers = data.servers || []
    if (!sub.servers.some((s) => s.id === sub.serverId)) sub.serverId = sub.servers[0]?.id || ''
    resumeActiveJobs()
  } catch (e) {
    sub.servers = []
    ElMessage.error(`加载 Sub2API 连接失败：${e.message}`)
  }
}

async function loadSub2Groups() {
  if (!sub.serverId) return
  try {
    const data = await api.get(`/api/admin/upstream-accounts/sub2api/servers/${sub.serverId}/groups`)
    sub.groups = data.groups || []
  } catch (e) {
    sub.groups = []
    ElMessage.error(`加载远端分组失败：${e.message}`)
  }
}

function isCollapsed(key) {
  return Boolean(sub.collapsed[key])
}

function toggleCollapsed(key) {
  sub.collapsed = { ...sub.collapsed, [key]: !sub.collapsed[key] }
}

function selectedInGroup(group) {
  const set = new Set(sub.selected)
  return group.accounts.filter((a) => set.has(a.id)).length
}

function toggleSubAccount(accountId, checked) {
  const next = new Set(sub.selected)
  if (checked) next.add(accountId)
  else next.delete(accountId)
  sub.selected = Array.from(next)
}

function selectSubGroup(group) {
  const next = new Set(sub.selected)
  for (const account of group.accounts) next.add(account.id)
  sub.selected = Array.from(next)
}

function clearSubGroup(group) {
  const ids = new Set(group.accounts.map((a) => a.id))
  sub.selected = sub.selected.filter((id) => !ids.has(id))
}

async function loadSub2Accounts() {
  if (!sub.serverId) {
    ElMessage.warning('请先选择 Sub2API 连接')
    return
  }
  sub.busy = true
  try {
    const serverId = sub.serverId
    if (!sub.groups.length) await loadSub2Groups()
    let accounts = []
    const groupParam = sub.groupId ? sub.groupId : ''
    const data = await api.get(
      `/api/admin/upstream-accounts/sub2api/servers/${serverId}/accounts${groupParam ? `?group_id=${encodeURIComponent(groupParam)}` : ''}`
    )
    accounts = data.accounts || []
    // 「全部账号」时按远端分组逐个拉取，保证每行都带得上分组信息
    if (!groupParam && sub.groups.length) {
      const results = await Promise.all(
        sub.groups.map(async (group) => {
          try {
            const r = await api.get(
              `/api/admin/upstream-accounts/sub2api/servers/${serverId}/accounts?group_id=${encodeURIComponent(group.id)}`
            )
            return (r.accounts || []).map((a) => ({
              ...a,
              remote_group_id: a.remote_group_id || String(group.id),
              remote_group_name: a.remote_group_name || String(group.name || group.id),
            }))
          } catch {
            return []
          }
        })
      )
      if (results.some((list) => list.length)) accounts = [...results.flat(), ...accounts]
    }
    const seen = new Set()
    sub.accounts = accounts.filter((a) => {
      const id = String(a.id || '')
      if (!id || seen.has(id)) return false
      seen.add(id)
      return true
    })
    sub.selected = []
    sub.collapsed = Object.fromEntries(subAccountGroups.value.map((g) => [g.key, true]))
  } catch (e) {
    ElMessage.error(`加载远端账号失败：${e.message}`)
  } finally {
    sub.busy = false
  }
}

async function createSub2Server() {
  const f = sub.form
  if (!f.base_url.trim()) {
    ElMessage.warning('请填写 Sub2API 服务地址')
    return
  }
  if (!(f.email.trim() && f.password.trim()) && !f.api_key.trim()) {
    ElMessage.warning('请填写邮箱+密码，或 API Key 之一')
    return
  }
  sub.busy = true
  try {
    const data = await api.post('/api/admin/upstream-accounts/sub2api/servers', { ...f })
    sub.servers = data.servers || []
    sub.serverId = data.server?.id || sub.serverId
    sub.connectVisible = false
    sub.form = { name: '', base_url: '', email: '', password: '', api_key: '', group_id: '' }
    ElMessage.success('Sub2API 连接已保存')
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    sub.busy = false
  }
}

async function deleteSub2Server() {
  const server = sub.servers.find((s) => s.id === sub.serverId)
  if (!server) return
  try {
    await ElMessageBox.confirm(`确定删除连接「${server.name || server.base_url}」吗？`, '删除 Sub2API 连接', { type: 'warning' })
  } catch {
    return
  }
  try {
    const data = await api.del(`/api/admin/upstream-accounts/sub2api/servers/${server.id}`)
    sub.servers = data.servers || []
    sub.serverId = sub.servers[0]?.id || ''
    sub.accounts = []
    sub.groups = []
    sub.selected = []
    ElMessage.success('连接已删除')
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function buildGroupBindings(accountIds) {
  if (targetGroupId.value !== null || !sub.byGroups) return []
  const selected = new Set(accountIds)
  const bindings = []
  for (const group of subAccountGroups.value) {
    const ids = group.accounts.map((a) => a.id).filter((id) => selected.has(id))
    if (!ids.length) continue
    if (!group.groupId && group.name === '未分组账号') continue
    bindings.push({ remote_group_id: group.groupId, name: group.name, account_ids: ids })
  }
  return bindings
}

async function importFromSub2() {
  if (!sub.serverId || !sub.selected.length) return
  const bindings = buildGroupBindings(sub.selected)
  const groupText = bindings.length ? `，并按 ${bindings.length} 个远端分组创建或复用同名分组` : ''
  if (!(await confirmImport('确认 Sub2API 导入', `即将导入 ${sub.selected.length} 个账号${groupText}。是否继续？`))) return
  importBusy.value = true
  importVisible.value = false
  openRun('导入 Sub2API 账号')
  try {
    const start = await api.post(`/api/admin/upstream-accounts/sub2api/servers/${sub.serverId}/import`, {
      account_ids: sub.selected,
      group_bindings: bindings,
      create_account_groups: targetGroupId.value === null && sub.byGroups,
      target_group_id: targetGroupId.value,
    })
    await trackJob(start.import_job, () =>
      api.get(`/api/admin/upstream-accounts/sub2api/servers/${sub.serverId}/import`)
    )
    sub.selected = []
  } catch (e) {
    failRun(e.message)
  } finally {
    importBusy.value = false
  }
}

/* ------------------------------------------------------------------ 远程导入任务跟踪 */

async function trackJob(job, fetchProgress) {
  if (!job) throw new Error('上游没有返回导入任务')
  run.indeterminate = false
  run.stage = job.stage_label || '读取凭据'
  run.percent = progressPercent(job)
  if (job.terminal) {
    finishRemoteJob(job)
    return
  }
  for (let i = 0; i < 300; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500))
    const data = await fetchProgress()
    const current = data.import_job
    if (!current) continue
    run.stage = current.stage_label || run.stage
    run.percent = progressPercent(current)
    run.events = Array.isArray(current.events) ? current.events.slice(0, 200) : []
    if (current.terminal) {
      finishRemoteJob(current)
      return
    }
  }
  throw new Error('导入进度超时')
}

function progressPercent(job) {
  const totalCount = Number(job?.progress_total || job?.total || 0)
  const done = Number(job?.progress_completed || job?.completed || 0)
  if (!totalCount) return job?.terminal ? 100 : 0
  return Math.min(100, Math.round((done / totalCount) * 100))
}

function finishRemoteJob(job) {
  if (job.status === 'failed') {
    failRun(job.error || job.result_message || '导入失败')
    return
  }
  finishRun(job)
  run.stage = job.result_message || '完成'
  if (job.result_tone === 'warning') ElMessage.warning(job.result_message)
  else ElMessage.success(job.result_message || '导入完成')
  load()
  loadGroups()
}

/** 打开导入弹窗时，若上游仍有进行中的远程导入任务，则恢复跟踪。 */
function resumeActiveJobs() {
  const active = [...cpa.pools, ...sub.servers].find((source) =>
    ['pending', 'running'].includes(String(source.import_job?.status || ''))
  )
  if (!active || run.visible) return
  const isCpa = cpa.pools.some((p) => p.id === active.id)
  importVisible.value = false
  openRun(isCpa ? '导入远程 CPA（恢复跟踪）' : '导入 Sub2API 账号（恢复跟踪）')
  trackJob(active.import_job, () =>
    isCpa
      ? api.get(`/api/admin/upstream-accounts/cpa/pools/${active.id}/import`)
      : api.get(`/api/admin/upstream-accounts/sub2api/servers/${active.id}/import`)
  ).catch((e) => failRun(e.message))
}

/* ------------------------------------------------------------------ 账号操作 */

async function pollOperation(progressId, doneText) {
  clearPoll()
  const tick = async () => {
    try {
      const data = await api.get(`/api/admin/upstream-accounts/operations/${encodeURIComponent(progressId)}`)
      if (data.done) {
        const failed = data.result?.errors?.length || 0
        const message = data.message || data.status_label || doneText
        if (data.error || data.tone === 'danger') ElMessage.error(data.error || message)
        else if (failed) ElMessage.warning(`${message}（${failed} 个失败）`)
        else if (data.tone === 'warning') ElMessage.warning(message)
        else ElMessage.success(message)
        await load()
        return
      }
      pollTimer = setTimeout(tick, 2000)
    } catch {
      pollTimer = setTimeout(tick, 4000)
    }
  }
  pollTimer = setTimeout(tick, 1500)
}

async function batch(accountIds, operation, label) {
  try {
    const data = await api.post('/api/admin/upstream-accounts/batch', { account_ids: accountIds, operation })
    if (data.progress_id) pollOperation(data.progress_id, `${label}完成`)
    else {
      ElMessage.success(`${label}已提交`)
      await load()
    }
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function removeAccounts(accountIds, label) {
  try {
    const data = await api.post('/api/admin/upstream-accounts/delete', { account_ids: accountIds })
    // 上游删除是异步任务：有 progress_id 就轮询到真正完成，否则立刻刷新列表
    if (data.progress_id) pollOperation(data.progress_id, `${label}完成`)
    else {
      ElMessage.success(`${label}已提交`)
      await load()
    }
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function syncAccounts(accountIds, label) {
  try {
    const data = await api.post('/api/admin/upstream-accounts/sync', { account_ids: accountIds })
    if (data.progress_id) pollOperation(data.progress_id, `${label}完成`)
    else {
      ElMessage.success(`${label}已触发`)
      await load()
    }
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function onSelectionChange(rows) {
  selectedRows.value = rows
}

async function syncAll() {
  syncing.value = true
  try {
    await syncAccounts([], '同步全部额度')
  } finally {
    syncing.value = false
  }
}

async function removeOne(row) {
  try {
    await ElMessageBox.confirm(`确定删除账号「${row.display_name || row.email || row.id}」吗？`, '删除账号', { type: 'warning' })
  } catch {
    return
  }
  await removeAccounts([row.id], '删除账号')
}

async function batchDelete() {
  if (!selectedIds.value.length) return
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${selectedIds.value.length} 个账号吗？`, '批量删除账号', { type: 'warning' })
  } catch {
    return
  }
  await removeAccounts(selectedIds.value, '批量删除')
}

/* ------------------------------------------------------------------ 分组管理 */

const groupDialogVisible = ref(false)
const groupForm = reactive({ id: '', name: '', notes: '', enabled: true })
const groupSaving = ref(false)
const bindGroupId = ref('')

async function saveGroup() {
  if (!groupForm.name.trim() && !groupForm.id.trim()) {
    ElMessage.warning('请填写账号组名称')
    return
  }
  groupSaving.value = true
  try {
    await api.post('/api/admin/upstream-accounts/groups', { ...groupForm })
    ElMessage.success('账号组已保存')
    groupForm.id = ''
    groupForm.name = ''
    groupForm.notes = ''
    await loadGroups()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    groupSaving.value = false
  }
}

async function removeGroup(group) {
  try {
    await ElMessageBox.confirm(
      `确定删除账号组「${group.name}」吗？组内 ${group.account_count || 0} 个账号会变为未分组。`,
      '删除账号组',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await api.del(`/api/admin/upstream-accounts/groups/${encodeURIComponent(group.id)}`)
    ElMessage.success('账号组已删除')
    await Promise.all([loadGroups(), load()])
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function bindSelectedToGroup() {
  if (!selectedIds.value.length) {
    ElMessage.warning('请先选择账号')
    return
  }
  // 空值不落库：避免误把账号移出分组（移出分组请显式选择「移出分组」）
  if (bindGroupId.value === '') {
    ElMessage.warning('请先选择目标分组')
    return
  }
  try {
    await api.post('/api/admin/upstream-accounts/bind-group', {
      account_ids: selectedIds.value,
      group_id: bindGroupId.value,
    })
    ElMessage.success('账号组已更新')
    await Promise.all([loadGroups(), load()])
  } catch (e) {
    ElMessage.error(e.message)
  }
}

/* ------------------------------------------------------------------ 展示辅助 */

function fmtTs(v) {
  if (!v) return '-'
  const d = new Date(Number(v) * 1000)
  return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString('zh-CN', { hour12: false })
}

function tagType(tone) {
  if (tone === 'success') return 'success'
  if (tone === 'warning') return 'warning'
  if (tone === 'error' || tone === 'danger') return 'danger'
  return 'info'
}

function eventType(status) {
  if (status === 'success') return 'success'
  if (status === 'failed') return 'danger'
  if (status === 'skipped') return 'warning'
  return 'info'
}

onMounted(() => {
  load()
  loadGroups()
})

onUnmounted(clearPoll)
</script>

<template>
  <div class="page">
    <h2 class="page-title">上游账号（chatgpt2api）</h2>
    <el-alert type="info" :closable="false" style="margin-bottom: 16px">
      在此直接管理上游 ChatGPT 账号，上游密钥不出服务端、上游服务无需暴露端口。
      导入支持 Access Token / Session JSON / 完整备份 / CPA / Sub2API JSON 及远程 CPA、Sub2API 服务器，
      与上游 chatgpt2api 控制台口径一致。若页面报"上游版本不兼容"类错误，请按 docs/UPSTREAM-ADMIN-API.md 的预案处理。
    </el-alert>

    <div class="account-toolbar">
      <el-input
        v-model="keyword"
        placeholder="搜索邮箱 / 用户 ID"
        style="width: 220px"
        clearable
        @keyup.enter="page = 1; load()"
      />
      <el-select v-model="statusFilter" style="width: 110px" @change="page = 1; load()">
        <el-option v-for="o in STATUS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
      </el-select>
      <el-select v-model="groupFilter" style="width: 150px" @change="page = 1; load()">
        <el-option label="全部分组" value="all" />
        <el-option label="未分组" :value="UNGROUPED" />
        <el-option
          v-for="g in accountGroups"
          :key="g.id"
          :label="`${g.name}（${g.account_count}）`"
          :value="g.id"
        />
      </el-select>
      <el-button type="primary" :icon="Search" @click="page = 1; load()">搜索</el-button>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
      <el-button type="primary" plain :icon="Upload" @click="openImport()">导入账号</el-button>
      <el-button plain @click="groupDialogVisible = true">分组管理</el-button>
      <el-button type="warning" plain :loading="syncing" @click="syncAll">同步全部额度</el-button>
    </div>

    <el-alert v-if="loadError" type="error" :title="`加载失败：${loadError}`" :closable="false" style="margin-bottom: 16px" />

    <el-card>
      <div v-if="selectedIds.length" class="batch-bar">
        <span>已选 {{ selectedIds.length }} 个账号</span>
        <el-button size="small" @click="batch(selectedIds, 'enable', '批量启用')">批量启用</el-button>
        <el-button size="small" @click="batch(selectedIds, 'disable', '批量禁用')">批量禁用</el-button>
        <el-button size="small" @click="syncAccounts(selectedIds, '批量同步')">批量同步</el-button>
        <el-select v-model="bindGroupId" size="small" style="width: 150px" placeholder="绑定到分组">
          <el-option label="移出分组" :value="UNGROUPED" />
          <el-option v-for="g in accountGroups" :key="g.id" :label="g.name" :value="g.id" />
        </el-select>
        <el-button size="small" type="primary" plain @click="bindSelectedToGroup">应用分组</el-button>
        <el-button size="small" type="danger" plain @click="batchDelete">批量删除</el-button>
      </div>

      <el-table :data="items" v-loading="loading" size="small" row-key="id" @selection-change="onSelectionChange">
        <el-table-column type="selection" width="42" />
        <el-table-column label="账号" min-width="200">
          <template #default="{ row }">
            <div>{{ row.email || row.user_id || row.id }}</div>
            <div class="text-muted" style="font-size: 12px">
              {{ row.plan_label }} · {{ row.source_label }}{{ row.group_name ? ` · ${row.group_name}` : '' }}
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tooltip :disabled="!row.status_reason" :content="row.status_reason">
              <el-tag :type="tagType(row.status_tone)" size="small">{{ row.status_label }}</el-tag>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="绘图额度" width="120">
          <template #default="{ row }">
            <div>{{ row.quota_label }}</div>
            <div v-if="row.image_inflight || row.quota_reset_at" class="text-muted" style="font-size: 12px">
              <span v-if="row.image_inflight">在途 {{ row.image_inflight }}</span>
              <span v-if="row.quota_reset_at">{{ row.image_inflight ? ' · ' : '' }}重置 {{ fmtTs(row.quota_reset_at) }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="AT / RT" width="140">
          <template #default="{ row }">
            <div style="font-size: 12px">
              <div>AT：<span :style="{ color: row.access_token_tone === 'success' ? 'green' : row.access_token_tone ? 'orange' : '' }">{{ row.access_token_label }}</span></div>
              <div>RT：{{ row.refresh_token_label }}</div>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="成/败" width="90">
          <template #default="{ row }">{{ row.success_count }} / {{ row.failure_count }}</template>
        </el-table-column>
        <el-table-column label="最近使用" width="150">
          <template #default="{ row }">{{ fmtTs(row.last_used_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="syncAccounts([row.id], '检测')">检测</el-button>
            <el-button size="small" text :type="row.enabled ? 'warning' : 'success'" @click="batch([row.id], row.enabled ? 'disable' : 'enable', row.enabled ? '禁用' : '启用')">
              {{ row.enabled ? '禁用' : '启用' }}
            </el-button>
            <el-button size="small" text type="danger" @click="removeOne(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-if="total > 50"
        layout="total, prev, pager, next"
        :total="total"
        :page-size="50"
        :current-page="page"
        style="margin-top: 12px; justify-content: center"
        @current-change="(p) => { page = p; load() }"
      />
      <el-empty v-if="!loading && !items.length && !loadError" description="上游暂无账号，点击「导入账号」开始导入" />
    </el-card>

    <!-- 导入弹窗 -->
    <el-dialog
      v-model="importVisible"
      title="导入账号"
      width="min(920px, 96vw)"
      :close-on-click-modal="false"
      @close="closeImport"
    >
      <div class="import-layout">
        <aside class="import-modes">
          <button
            v-for="mode in IMPORT_MODES"
            :key="mode.value"
            type="button"
            class="import-mode"
            :class="{ 'is-active': importMode === mode.value }"
            :disabled="importBusy"
            @click="setImportMode(mode.value)"
          >
            {{ mode.label }}
          </button>

          <el-divider style="margin: 12px 0" />
          <div class="import-target">
            <div class="text-muted" style="font-size: 12px; margin-bottom: 6px">导入目标分组</div>
            <!-- 注意：导入接口用空串表示「移出分组」，与绑定接口的 __ungrouped__ 不同 -->
            <el-select v-model="importTargetGroup" size="small" style="width: 100%">
              <el-option label="保持原有分组" :value="PRESERVE_GROUP" />
              <el-option label="移出分组" :value="''" />
              <el-option v-for="g in accountGroups" :key="g.id" :label="g.name" :value="g.id" />
            </el-select>
          </div>
        </aside>

        <section class="import-body">
          <!-- Access Token -->
          <div v-if="importMode === 'access_token'" class="import-section">
            <h4>导入 Access Token</h4>
            <p class="text-muted">
              支持直接粘贴，一行一个（空行与 <code>#</code> 开头的注释行会被忽略，重复项自动去重）；
              也支持从 TXT 文件读取。
            </p>
            <el-input v-model="tokenText" type="textarea" :rows="10" placeholder="一行一个 access token" />
            <div class="import-actions">
              <el-button :disabled="importBusy" @click="pickTokenFile">读取 TXT 文件</el-button>
              <el-button type="primary" :disabled="importBusy || !tokenText.trim()" @click="importTokenText">开始导入</el-button>
            </div>
          </div>

          <!-- Session JSON -->
          <div v-else-if="importMode === 'session_json'" class="import-section">
            <h4>导入 Session JSON</h4>
            <p class="text-muted">
              打开
              <a href="https://chatgpt.com/api/auth/session" target="_blank" rel="noopener noreferrer">https://chatgpt.com/api/auth/session</a>
              ，复制完整 JSON，系统会保留其中的 AT、RT 和 ID Token（该接口通常不包含长期 RT）。
            </p>
            <el-input v-model="sessionText" type="textarea" :rows="12" placeholder="粘贴完整 session JSON" />
            <div class="import-actions">
              <el-button type="primary" :disabled="importBusy || !sessionText.trim()" @click="importSessionJson">开始导入</el-button>
            </div>
          </div>

          <!-- 本地 JSON 文件（备份 / CPA / Sub2API） -->
          <div v-else-if="['backup_json', 'cpa_json', 'sub2api_json'].includes(importMode)" class="import-section">
            <h4>
              {{ importMode === 'backup_json' ? '导入完整备份文件' : importMode === 'cpa_json' ? '导入 CPA JSON 文件' : '导入 Sub2API JSON 文件' }}
            </h4>
            <p class="text-muted">
              <template v-if="importMode === 'backup_json'">
                读取本系统或上游导出的完整账号 JSON，直接恢复已保存的凭据与账号配置，不请求远程接口验证。
              </template>
              <template v-else>
                从一个或多个 {{ importMode === 'cpa_json' ? 'CPA' : 'Sub2API' }} JSON 文件提取账号凭据，保存后同步账号信息与额度。
              </template>
              支持 <code>accounts / items / results / data</code> 等常见嵌套结构。
            </p>
            <el-button :icon="Upload" :disabled="importBusy" @click="pickJsonFiles">
              选择 {{ importMode === 'backup_json' ? '备份' : importMode === 'cpa_json' ? 'CPA' : 'Sub2API' }} JSON 文件（可多选）
            </el-button>
          </div>

          <!-- OAuth 登录 -->
          <div v-else-if="importMode === 'oauth_login'" class="import-section">
            <h4>OAuth 登录已有账号</h4>
            <p class="text-muted">
              用浏览器登录 ChatGPT，回填 callback URL 后保存 RT，用于 AT 临期自动续期。
            </p>
            <el-form label-width="110px">
              <el-form-item label="账号邮箱（可选）">
                <el-input v-model="oauth.emailHint" placeholder="name@example.com" />
              </el-form-item>
            </el-form>
            <div class="import-actions" style="justify-content: flex-start">
              <el-button type="primary" :loading="oauth.busy" @click="startOAuth">
                {{ oauth.authorizeUrl ? '重新生成授权链接' : '生成并打开授权页面' }}
              </el-button>
              <el-button v-if="oauth.authorizeUrl" @click="openAuthorizeUrl">打开授权页面</el-button>
              <el-button v-if="oauth.authorizeUrl" @click="copyAuthorizeUrl">复制授权链接</el-button>
            </div>
            <el-alert
              v-if="oauth.authorizeUrl"
              type="info"
              :closable="false"
              style="margin: 12px 0"
              :title="`授权链接已生成。登录完成后把浏览器最终跳转到的 callback URL 粘贴到下方。${oauth.redirectPrefix ? `目标地址：${oauth.redirectPrefix}` : ''}`"
            />
            <el-input
              v-model="oauth.callback"
              type="textarea"
              :rows="4"
              :disabled="!oauth.sessionId"
              placeholder="粘贴完整 callback URL，或只粘贴 code"
            />
            <div class="import-actions">
              <el-button type="primary" :disabled="!oauth.sessionId || !oauth.callback.trim()" @click="finishOAuth">完成导入</el-button>
            </div>
          </div>

          <!-- 远程 CPA -->
          <div v-else-if="importMode === 'remote_cpa'" class="import-section">
            <h4>从远程 CPA 导入</h4>
            <p class="text-muted">读取已保存的 CPA 连接里的账号文件，勾选后导入上游号池。</p>
            <div class="import-inline">
              <el-select v-model="cpa.poolId" placeholder="选择 CPA 连接" style="flex: 1">
                <el-option v-for="p in cpa.pools" :key="p.id" :label="p.name || p.base_url || p.id" :value="p.id" />
              </el-select>
              <el-button :loading="cpa.busy" @click="loadCpaPools">刷新来源</el-button>
              <el-button :disabled="!cpa.poolId" @click="loadCpaFiles">加载文件</el-button>
              <el-button plain @click="cpa.connectVisible = true">新增连接</el-button>
              <el-button plain type="danger" :disabled="!cpa.poolId" @click="deleteCpaPool">删除连接</el-button>
            </div>

            <div v-if="cpa.files.length" class="import-list-bar">
              <span>已加载 {{ cpa.files.length }} 个文件，已选择 {{ cpa.selected.length }} 个</span>
              <span>
                <el-button size="small" @click="selectAllCpaFiles">全选</el-button>
                <el-button size="small" :disabled="!cpa.selected.length" @click="clearCpaSelection">清空</el-button>
              </span>
            </div>
            <el-empty v-if="!cpa.files.length" :description="cpa.poolId ? '暂无可导入文件，点击「加载文件」获取' : '请选择 CPA 连接'" :image-size="60" />
            <div v-else class="import-scroll">
              <label v-for="file in cpa.files" :key="file.name" class="import-row">
                <span class="import-row-text">
                  <span>{{ file.email || file.name }}</span>
                  <span class="text-muted">{{ file.name }}</span>
                </span>
                <el-checkbox
                  :model-value="cpa.selected.includes(file.name)"
                  @update:model-value="(v) => toggleCpaFile(file.name, v)"
                />
              </label>
            </div>
            <div class="import-actions">
              <el-button type="primary" :disabled="importBusy || !cpa.selected.length" @click="importFromCpa">导入选中</el-button>
            </div>
          </div>

          <!-- 远程 Sub2API -->
          <div v-else-if="importMode === 'sub2api'" class="import-section">
            <h4>从 Sub2API 导入</h4>
            <p class="text-muted">
              读取已保存 Sub2API 连接里的账号；选择「全部账号」时会按远端分组折叠加载，导入时可自动创建或复用同名上游分组。
            </p>
            <div class="import-inline">
              <el-select v-model="sub.serverId" placeholder="选择 Sub2API 连接" style="flex: 1" @change="loadSub2Groups">
                <el-option v-for="s in sub.servers" :key="s.id" :label="s.name || s.base_url || s.id" :value="s.id" />
              </el-select>
              <el-select v-model="sub.groupId" placeholder="全部分组" style="width: 200px" @change="sub.accounts = []; sub.selected = []">
                <el-option label="全部账号（按远端分组）" value="" />
                <el-option
                  v-for="g in sub.groups"
                  :key="g.id"
                  :label="`${g.name || g.id} · ${g.active_account_count}/${g.account_count}`"
                  :value="g.id"
                />
              </el-select>
              <el-button :loading="sub.busy" @click="loadSub2Servers">刷新来源</el-button>
              <el-button :disabled="!sub.serverId" @click="loadSub2Accounts">加载账号</el-button>
              <el-button plain @click="sub.connectVisible = true">新增连接</el-button>
              <el-button plain type="danger" :disabled="!sub.serverId" @click="deleteSub2Server">删除连接</el-button>
            </div>

            <div v-if="sub.accounts.length" class="import-list-bar">
              <span>已加载 {{ sub.accounts.length }} 个账号，{{ subAccountGroups.length }} 个分组，已选择 {{ sub.selected.length }} 个</span>
              <span>
                <el-checkbox v-model="sub.byGroups" :disabled="importTargetGroup !== PRESERVE_GROUP">按远端分组导入</el-checkbox>
                <el-button size="small" @click="sub.selected = sub.accounts.map((a) => a.id)">全选账号</el-button>
                <el-button size="small" :disabled="!sub.selected.length" @click="sub.selected = []">清空</el-button>
              </span>
            </div>
            <el-empty v-if="!sub.accounts.length" :description="sub.serverId ? '暂无可导入账号，点击「加载账号」获取' : '请选择 Sub2API 连接'" :image-size="60" />
            <div v-else class="import-scroll">
              <div v-for="group in subAccountGroups" :key="group.key" class="import-group">
                <div class="import-group-head">
                  <el-button link @click="toggleCollapsed(group.key)">
                    {{ isCollapsed(group.key) ? '▸' : '▾' }} {{ group.name }}
                    <span class="text-muted">（{{ selectedInGroup(group) }}/{{ group.accounts.length }}）</span>
                  </el-button>
                  <span>
                    <el-button size="small" @click="selectSubGroup(group)">全选本组</el-button>
                    <el-button size="small" :disabled="!selectedInGroup(group)" @click="clearSubGroup(group)">清空本组</el-button>
                  </span>
                </div>
                <div v-if="!isCollapsed(group.key)">
                  <label v-for="account in group.accounts" :key="account.id" class="import-row">
                    <span class="import-row-text">
                      <span>{{ account.email || account.name || account.id }}</span>
                      <span class="text-muted">
                        {{ account.plan_type || '未知' }} · {{ account.status || '-' }} ·
                        {{ account.has_access_token ? '有 access token' : '需导出 token' }}
                      </span>
                    </span>
                    <el-checkbox
                      :model-value="sub.selected.includes(account.id)"
                      @update:model-value="(v) => toggleSubAccount(account.id, v)"
                    />
                  </label>
                </div>
              </div>
            </div>
            <div class="import-actions">
              <el-button type="primary" :disabled="importBusy || !sub.selected.length" @click="importFromSub2">导入选中</el-button>
            </div>
          </div>
        </section>
      </div>
    </el-dialog>

    <!-- 新增 CPA 连接 -->
    <el-dialog v-model="cpa.connectVisible" title="新增 CPA 连接" width="min(520px, 94vw)">
      <el-form label-width="100px">
        <el-form-item label="名称"><el-input v-model="cpa.form.name" placeholder="可留空，默认显示服务地址" /></el-form-item>
        <el-form-item label="服务地址"><el-input v-model="cpa.form.base_url" placeholder="https://cpa.example.com" /></el-form-item>
        <el-form-item label="管理密钥"><el-input v-model="cpa.form.secret_key" type="password" show-password placeholder="仅保存在服务端" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cpa.connectVisible = false">取消</el-button>
        <el-button type="primary" :loading="cpa.busy" @click="createCpaPool">保存</el-button>
      </template>
    </el-dialog>

    <!-- 新增 Sub2API 连接 -->
    <el-dialog v-model="sub.connectVisible" title="新增 Sub2API 连接" width="min(560px, 94vw)">
      <el-form label-width="110px">
        <el-form-item label="名称"><el-input v-model="sub.form.name" placeholder="可留空，默认显示服务地址" /></el-form-item>
        <el-form-item label="服务地址"><el-input v-model="sub.form.base_url" placeholder="https://sub2api.example.com" /></el-form-item>
        <el-form-item label="登录邮箱"><el-input v-model="sub.form.email" /></el-form-item>
        <el-form-item label="登录密码"><el-input v-model="sub.form.password" type="password" show-password placeholder="仅保存在服务端" /></el-form-item>
        <el-form-item label="API Key"><el-input v-model="sub.form.api_key" type="password" show-password placeholder="与账号密码二选一，仅保存在服务端" /></el-form-item>
        <el-form-item label="默认分组"><el-input v-model="sub.form.group_id" placeholder="可留空" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="sub.connectVisible = false">取消</el-button>
        <el-button type="primary" :loading="sub.busy" @click="createSub2Server">保存</el-button>
      </template>
    </el-dialog>

    <!-- 分组管理 -->
    <el-dialog v-model="groupDialogVisible" title="账号组管理" width="min(620px, 94vw)">
      <el-table :data="accountGroups" size="small" max-height="320">
        <el-table-column label="名称" min-width="140">
          <template #default="{ row }">{{ row.name }}</template>
        </el-table-column>
        <el-table-column label="账号数" width="90">
          <template #default="{ row }">{{ row.account_count }}</template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="140">
          <template #default="{ row }">{{ row.notes || '-' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button size="small" text type="danger" @click="removeGroup(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-divider content-position="left">新建 / 更新账号组</el-divider>
      <el-form label-width="90px">
        <el-form-item label="组 ID"><el-input v-model="groupForm.id" placeholder="留空则按名称自动生成；填已有 ID 表示更新" /></el-form-item>
        <el-form-item label="名称"><el-input v-model="groupForm.name" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="groupForm.notes" /></el-form-item>
        <el-form-item label="启用"><el-switch v-model="groupForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="groupDialogVisible = false">关闭</el-button>
        <el-button type="primary" :loading="groupSaving" @click="saveGroup">保存</el-button>
      </template>
    </el-dialog>

    <!-- 导入进度 -->
    <el-drawer v-model="run.visible" :title="run.title || '导入进度'" size="min(520px, 94vw)" :close-on-click-modal="false">
      <el-alert v-if="run.error" type="error" :title="run.error" :closable="false" style="margin-bottom: 12px" />
      <div style="margin-bottom: 8px">{{ run.stage }}</div>
      <el-progress :percentage="run.percent" :indeterminate="run.indeterminate" :status="progressStatus" />
      <div v-if="run.result" class="run-summary">
        <el-tag type="success">新增 {{ run.result.added }}</el-tag>
        <el-tag type="info">更新 / 跳过 {{ run.result.skipped }}</el-tag>
        <el-tag type="primary">同步 {{ run.result.synced }}</el-tag>
        <el-tag v-if="run.result.failed" type="danger">失败 {{ run.result.failed }}</el-tag>
        <el-tag v-if="run.result.removed" type="warning">移除 {{ run.result.removed }}</el-tag>
      </div>
      <div v-if="run.events.length" class="run-events">
        <div class="text-muted" style="margin: 12px 0 6px">明细（最近 {{ run.events.length }} 条）</div>
        <div v-for="(event, index) in run.events" :key="`${event.sequence}-${index}`" class="run-event">
          <el-tag size="small" :type="eventType(event.status)">{{ event.status }}</el-tag>
          <span class="run-event-text">{{ event.account_label || event.account_id }}</span>
          <span class="text-muted">{{ event.message }}</span>
        </div>
      </div>
      <template #footer>
        <el-button @click="closeRun">关闭</el-button>
      </template>
    </el-drawer>

    <input ref="tokenFileInput" type="file" accept=".txt,text/plain" style="display: none" @change="onTokenFileChange" />
    <input ref="jsonFileInput" type="file" accept=".json,application/json" multiple style="display: none" @change="onJsonFileChange" />
  </div>
</template>

<style scoped>
.account-toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.batch-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  background: var(--el-fill-color-light, #f5f7fa);
}

.import-layout {
  display: grid;
  grid-template-columns: 190px minmax(0, 1fr);
  gap: 0;
  min-height: 420px;
}

.import-modes {
  border-right: 1px solid var(--el-border-color-lighter, #ebeef5);
  padding-right: 12px;
  display: flex;
  flex-direction: column;
}

.import-mode {
  border: 0;
  background: transparent;
  text-align: left;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 13px;
  color: var(--el-text-color-regular, #606266);
  cursor: pointer;
}

.import-mode:hover:not(:disabled) {
  background: var(--el-fill-color-light, #f5f7fa);
}

.import-mode.is-active {
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--el-color-primary, #409eff);
}

.import-mode:disabled {
  cursor: not-allowed;
  opacity: 0.6;
}

.import-body {
  padding-left: 16px;
  min-width: 0;
}

.import-section h4 {
  margin: 0 0 6px;
  font-size: 15px;
}

.import-section p {
  margin: 0 0 12px;
  font-size: 12px;
  line-height: 1.7;
}

.import-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.import-inline {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.import-list-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--el-fill-color-light, #f5f7fa);
  font-size: 12px;
  margin-bottom: 8px;
}

.import-scroll {
  max-height: 320px;
  overflow: auto;
  border: 1px solid var(--el-border-color-lighter, #ebeef5);
  border-radius: 6px;
}

.import-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--el-border-color-lighter, #ebeef5);
  cursor: pointer;
}

.import-row:last-child {
  border-bottom: 0;
}

.import-row-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
  font-size: 12px;
  gap: 2px;
  word-break: break-all;
}

.import-group + .import-group {
  border-top: 1px solid var(--el-border-color-lighter, #ebeef5);
}

.import-group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  flex-wrap: wrap;
  padding: 6px 10px;
  background: var(--el-fill-color-lighter, #fafafa);
  font-size: 12px;
}

.run-summary {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 12px;
}

.run-events {
  margin-top: 8px;
}

.run-event {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 12px;
  padding: 4px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter, #ebeef5);
}

.run-event-text {
  min-width: 0;
  word-break: break-all;
}

@media (max-width: 720px) {
  .import-layout {
    grid-template-columns: minmax(0, 1fr);
  }

  .import-modes {
    border-right: 0;
    border-bottom: 1px solid var(--el-border-color-lighter, #ebeef5);
    padding: 0 0 12px;
    margin-bottom: 12px;
  }

  .import-body {
    padding-left: 0;
  }
}
</style>
