<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Refresh } from '@element-plus/icons-vue'
import { api } from '../../api/client'
import { useAuthStore } from '../../stores/auth'
import { fmtBytes, fmtTime } from '../../utils/format'

const auth = useAuthStore()
const form = ref({
  feature_chat_enabled: true,
  feature_image_enabled: true,
  feature_search_enabled: true,
  feature_ppt_enabled: true,
  registration_enabled: true,
  registration_require_approval: false,
  site_name: '',
  announcement: '',
  upstream_base_url: '',
  upstream_api_key: '',
  storage_quota_mb_default: 0,
  file_retention_hours: 0,
  log_retention_hours: 168,
  audit_retention_days: 90,
  agreement_version: 1,
  agreement_text: '',
  notice_version: 1,
  notice_text: '',
  content_filter_enabled: true,
  content_filter_keywords: '',
  checkin_enabled: false,
  checkin_pool: 'permanent',
  checkin_valid_days: 1,
  checkin_valid_hours: 0,
  checkin_timezone: 'Asia/Shanghai',
  checkin_chat: 5,
  checkin_image: 0,
  checkin_search: 2,
  checkin_ppt: 0,
  max_inflight_default: 2,
})
const maskedKey = ref('')
const clearKeyOverride = ref(false)
const envBase = ref('')
const envConfigured = ref(false)
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)
const models = ref([])
const modelsVisible = ref(false)
const modelsLoading = ref(false)
const cleaning = ref(false)
const storageStats = ref({ total_bytes: 0, total_files: 0, total_logs: 0, by_kind: [], top_users: [], disk: null, db_bytes: null, cleanable: {}, last_cleanup: {} })
const builtinDefaults = ref({ keywords: '', agreement: '' })
let cleanupTimer = null

const savedBaseUrl = ref('') // 已保存的上游地址覆盖值（判定内置/外部以此为准）

const consoleUrl = computed(() => {
  const effective = savedBaseUrl.value || envBase.value
  if (!effective) return ''
  // 内置上游（docker 内网主机名）时，控制台映射在服务器本机 127.0.0.1:3000
  if (/^https?:\/\/chatgpt2api([:/]|$)/.test(effective)) return 'http://127.0.0.1:3000'
  return effective
})

const isBuiltinUpstream = computed(() =>
  /^https?:\/\/chatgpt2api([:/]|$)/.test(savedBaseUrl.value || envBase.value || '')
)

// 签到限时到期预览（与后端 compute_temp_expiry 同规则）
const checkinPreview = computed(() => {
  const f = form.value
  if (f.checkin_pool !== 'temporary') return '签到奖励进入永久组，无到期时间'
  try {
    const now = new Date()
    const base = new Date(now.getTime())
    base.setDate(base.getDate() + Math.max(f.checkin_valid_days, 1))
    base.setHours(0, 0, 0, 0)
    const expiry = new Date(base.getTime() + Math.max(f.checkin_valid_hours, 0) * 3600 * 1000)
    return `若现在签到，限时额度将于 ${expiry.toLocaleString('zh-CN', { hour12: false })}（${f.checkin_timezone} 规则）到期`
  } catch {
    return '-'
  }
})

function openConsole() {
  if (!consoleUrl.value) {
    ElMessage.warning('尚未配置上游服务')
    return
  }
  if (!isBuiltinUpstream.value) {
    window.open(consoleUrl.value, '_blank', 'noopener')
    return
  }
  ElMessageBox.alert(
    `内置上游默认<b>零端口暴露</b>（容器只在内网可达）。访问控制台有两种方式：<br><br>
    <b>方式一：开启本机绑定</b>（服务器本机浏览器访问）<br>
    在 .env 中加入 <code>COMPOSE_FILE=docker-compose.yml:docker-compose.console.yml</code>
    （Windows 服务器把冒号改成分号 <code>;</code>），执行 <code>docker compose up -d</code>，
    然后访问 <a href="http://127.0.0.1:3000" target="_blank" rel="noopener">http://127.0.0.1:3000</a>。<br><br>
    <b>方式二：SSH 隧道</b>（远程运维推荐）<br>
    先按方式一开启绑定，再在本机执行 <code>ssh -L 3000:127.0.0.1:3000 用户@服务器</code>，
    随后浏览器访问 <code>http://127.0.0.1:3000</code>。<br><br>
    控制台密钥即 .env 中的 UPSTREAM_API_KEY。`,
    '访问上游控制台',
    { dangerouslyUseHTMLString: true, confirmButtonText: '知道了' }
  ).catch(() => {})
}

async function load() {
  const data = await api.get('/api/admin/settings')
  form.value = {
    ...form.value,
    feature_chat_enabled: data.feature_chat_enabled,
    feature_image_enabled: data.feature_image_enabled,
    feature_search_enabled: data.feature_search_enabled,
    feature_ppt_enabled: data.feature_ppt_enabled,
    registration_enabled: data.registration_enabled,
    registration_require_approval: data.registration_require_approval,
    site_name: data.site_name,
    announcement: data.announcement,
    upstream_base_url: data.upstream_base_url,
    upstream_api_key: '',
    storage_quota_mb_default: data.storage_quota_mb_default,
    file_retention_hours: data.file_retention_hours,
    log_retention_hours: data.log_retention_hours,
    audit_retention_days: data.audit_retention_days,
    agreement_version: data.agreement_version,
    agreement_text: data.agreement_text,
    notice_version: data.notice_version,
    notice_text: data.notice_text,
    content_filter_enabled: data.content_filter_enabled,
    content_filter_keywords: data.content_filter_keywords,
    checkin_enabled: data.checkin_enabled,
    checkin_pool: data.checkin_pool,
    checkin_valid_days: data.checkin_valid_days,
    checkin_valid_hours: data.checkin_valid_hours,
    checkin_timezone: data.checkin_timezone,
    checkin_chat: data.checkin_chat,
    checkin_image: data.checkin_image,
    checkin_search: data.checkin_search,
    checkin_ppt: data.checkin_ppt,
    max_inflight_default: data.max_inflight_default,
  }
  maskedKey.value = data.upstream_api_key_masked
  envBase.value = data.env_upstream_base_url
  envConfigured.value = data.env_upstream_configured
  savedBaseUrl.value = data.upstream_base_url
  builtinDefaults.value = {
    keywords: data.content_filter_keywords_default || '',
    agreement: data.agreement_text_default || '',
  }
}

async function loadStorageStats() {
  const data = await api.get('/api/admin/storage')
  storageStats.value = data
  // 清理在后台运行时轮询刷新，结束后自动停
  if (data.cleanup_running && !cleanupTimer) {
    cleanupTimer = setInterval(loadStorageStats, 3000)
  } else if (!data.cleanup_running && cleanupTimer) {
    clearInterval(cleanupTimer)
    cleanupTimer = null
    cleaning.value = false
  }
}

const KIND_LABELS = { image: '图片', ppt: 'PPT', psd: 'PSD' }

const cleanableText = computed(() => {
  const c = storageStats.value.cleanable || {}
  const parts = []
  if (c.expired_files) parts.push(`过期产物 ${c.expired_files} 个（${fmtBytes(c.expired_bytes)}）`)
  if (c.orphan_files) parts.push(`孤儿文件 ${c.orphan_files} 个（${fmtBytes(c.orphan_bytes)}）`)
  if (c.old_usage_logs) parts.push(`旧调用日志 ${c.old_usage_logs} 条`)
  if (c.old_risk_events) parts.push(`旧风控事件 ${c.old_risk_events} 条`)
  if (c.old_audit_logs) parts.push(`旧审计日志 ${c.old_audit_logs} 条`)
  if (c.stale_sessions) parts.push(`失效会话 ${c.stale_sessions} 条`)
  return parts.length ? parts.join(' · ') : '当前没有按保留策略可清理的内容'
})

async function cleanupNow() {
  cleaning.value = true
  try {
    await api.post('/api/admin/storage/cleanup')
    ElMessage.success('清理任务已在后台开始执行')
    setTimeout(loadStorageStats, 1000)
  } catch (e) {
    cleaning.value = false
    ElMessage.error(e.message)
  }
}

async function save() {
  saving.value = true
  try {
    const payload = { ...form.value }
    if (clearKeyOverride.value) payload.upstream_api_key = ''
    else if (!payload.upstream_api_key) delete payload.upstream_api_key
    const data = await api.patch('/api/admin/settings', payload)
    maskedKey.value = data.upstream_api_key_masked
    form.value.upstream_api_key = ''
    clearKeyOverride.value = false
    savedBaseUrl.value = data.upstream_base_url
    ElMessage.success('设置已保存')
    auth.fetchMe(true)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  testResult.value = null
  try {
    const payload = {}
    if (form.value.upstream_base_url) payload.base_url = form.value.upstream_base_url
    if (form.value.upstream_api_key) payload.api_key = form.value.upstream_api_key
    testResult.value = await api.post('/api/admin/upstream/test', payload)
  } catch (e) {
    testResult.value = { ok: false, error: e.message }
  } finally {
    testing.value = false
  }
}

async function loadModels() {
  modelsVisible.value = true
  modelsLoading.value = true
  try {
    const data = await api.get('/api/admin/upstream/models')
    models.value = (data.data || []).map((m) => m.id).filter(Boolean)
  } catch (e) {
    ElMessage.error(e.message)
    modelsVisible.value = false
  } finally {
    modelsLoading.value = false
  }
}

onMounted(() => {
  load()
  loadStorageStats()
})
onUnmounted(() => {
  if (cleanupTimer) clearInterval(cleanupTimer)
})
</script>

<template>
  <div class="page" style="max-width: 860px">
    <h2 class="page-title">系统设置</h2>

    <el-card style="margin-bottom: 16px">
      <template #header>功能开关</template>
      <el-form label-width="160px">
        <el-form-item label="对话">
          <el-switch v-model="form.feature_chat_enabled" />
        </el-form-item>
        <el-form-item label="绘图">
          <el-switch v-model="form.feature_image_enabled" />
        </el-form-item>
        <el-form-item label="联网搜索">
          <el-switch v-model="form.feature_search_enabled" />
        </el-form-item>
        <el-form-item label="PPT / PSD">
          <el-switch v-model="form.feature_ppt_enabled" />
        </el-form-item>
        <el-form-item label="开放注册（邀请码）">
          <el-switch v-model="form.registration_enabled" />
        </el-form-item>
      </el-form>
      <div class="text-muted">关闭后对应功能立即对所有普通用户不可用，管理员不受影响。</div>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>注册</template>
      <el-form label-width="220px">
        <el-form-item label="注册需要管理员审核">
          <el-switch v-model="form.registration_require_approval" />
          <div class="text-muted">开启后新账号为待审核状态，在「用户管理」中通过后方能使用</div>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>每日签到</template>
      <el-form label-width="220px">
        <el-form-item label="开启签到">
          <el-switch v-model="form.checkin_enabled" />
        </el-form-item>
        <el-form-item label="奖励额度组">
          <el-radio-group v-model="form.checkin_pool">
            <el-radio value="permanent">永久组</el-radio>
            <el-radio value="temporary">限时组</el-radio>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.checkin_pool === 'temporary'">
          <el-form-item label="有效天数">
            <el-input-number v-model="form.checkin_valid_days" :min="1" :max="3650" />
            <span class="text-muted" style="margin-left: 10px">1 = 当天 24 点</span>
          </el-form-item>
          <el-form-item label="附加小时">
            <el-input-number v-model="form.checkin_valid_hours" :min="0" :max="8784" />
          </el-form-item>
        </template>
        <el-form-item label="时区">
          <el-input v-model="form.checkin_timezone" style="max-width: 260px" placeholder="Asia/Shanghai" />
          <div class="text-muted">IANA 时区名，如 Asia/Shanghai、Asia/Tokyo、UTC</div>
        </el-form-item>
        <el-form-item label="每日奖励（对话）">
          <el-input-number v-model="form.checkin_chat" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="每日奖励（绘图）">
          <el-input-number v-model="form.checkin_image" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="每日奖励（搜索）">
          <el-input-number v-model="form.checkin_search" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="每日奖励（PPT/PSD）">
          <el-input-number v-model="form.checkin_ppt" :min="0" :max="100000" />
        </el-form-item>
        <el-form-item label="到期时间预览">
          <el-tag type="info">{{ checkinPreview }}</el-tag>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>内容安全钩子</template>
      <el-form label-width="220px">
        <el-form-item label="关键词拦截">
          <el-switch v-model="form.content_filter_enabled" />
        </el-form-item>
        <el-form-item label="自定义关键词">
          <div style="width: 100%">
            <el-input
              v-model="form.content_filter_keywords"
              type="textarea"
              :rows="5"
              maxlength="20000"
              placeholder="每行一个，命中即拦截（在内置默认词库之外追加）"
            />
            <div style="display: flex; align-items: center; gap: 10px; margin-top: 6px; flex-wrap: wrap">
              <el-tag size="small" :type="form.content_filter_keywords ? 'warning' : 'info'" effect="plain">
                {{ form.content_filter_keywords ? `自定义 ${form.content_filter_keywords.split('\n').filter((x) => x.trim()).length} 条` : '当前使用内置默认词库' }}
              </el-tag>
              <el-button size="small" text type="primary" @click="form.content_filter_keywords = builtinDefaults.keywords">
                填入内置默认词库（{{ builtinDefaults.keywords.split('\n').filter((x) => x.trim()).length }} 条）以二次修改
              </el-button>
              <el-button v-if="form.content_filter_keywords" size="small" text type="danger" @click="form.content_filter_keywords = ''">
                清空（仅用内置词库）
              </el-button>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <div class="text-muted">对话、绘图、搜索、PPT 的提示词均会检查；命中只记录命中的词，不留存用户原文。</div>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>公告弹窗</template>
      <el-form label-width="220px">
        <el-form-item label="公告版本">
          <el-input-number v-model="form.notice_version" :min="1" :max="1000000" />
          <span class="text-muted" style="margin-left: 10px">调高版本后所有用户再弹一次</span>
        </el-form-item>
        <el-form-item label="公告内容">
          <el-input v-model="form.notice_text" type="textarea" :rows="4" maxlength="5000" placeholder="留空则不弹公告（支持 Markdown）" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>并发</template>
      <el-form label-width="220px">
        <el-form-item label="单用户默认并发上限">
          <el-input-number v-model="form.max_inflight_default" :min="1" :max="100" />
        </el-form-item>
      </el-form>
      <div class="text-muted">同时在途请求数（对话/绘图/搜索/PPT 共用）；单用户可在用户管理中覆盖。</div>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>站点</template>
      <el-form label-width="160px">
        <el-form-item label="站点名称">
          <el-input v-model="form.site_name" maxlength="100" style="max-width: 360px" />
        </el-form-item>
        <el-form-item label="全站公告">
          <el-input v-model="form.announcement" type="textarea" :rows="2" maxlength="2000" placeholder="留空则不显示" />
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>上游服务（chatgpt2api）</span>
          <div>
            <el-button size="small" :icon="Refresh" @click="loadModels" style="margin-right: 8px">查看模型目录</el-button>
            <el-button size="small" type="primary" plain @click="openConsole">
              打开上游控制台
            </el-button>
          </div>
        </div>
      </template>
      <el-alert type="info" :closable="false" style="margin-bottom: 16px">
        此处配置会覆盖 .env 中的 UPSTREAM_BASE_URL / UPSTREAM_API_KEY；留空则回退到环境变量
        （当前环境变量地址：{{ envBase || '未配置' }}{{ envConfigured ? '，密钥已配置' : '，密钥未配置' }}）。
        使用 compose 内置上游时无需修改。ChatGPT 账号的添加与维护在「上游控制台」中完成。
      </el-alert>
      <el-form label-width="160px">
        <el-form-item label="上游地址">
          <el-input v-model="form.upstream_base_url" placeholder="例如 http://chatgpt2api 或 https://api.example.com" style="max-width: 480px" />
        </el-form-item>
        <el-form-item label="上游密钥">
          <el-input
            v-model="form.upstream_api_key"
            :placeholder="maskedKey ? `当前已保存：${maskedKey}（留空保持不变）` : '留空则使用环境变量'"
            show-password
            type="password"
            style="max-width: 480px"
            autocomplete="new-password"
          />
        </el-form-item>
        <el-form-item v-if="maskedKey" label=" ">
          <el-checkbox v-model="clearKeyOverride">清除已保存的密钥覆盖（回退到环境变量）</el-checkbox>
        </el-form-item>
        <el-form-item label="连通性测试">
          <el-button :icon="Connection" :loading="testing" @click="test">测试连接</el-button>
          <div v-if="testResult" style="margin-left: 12px; display: inline-block">
            <el-tag v-if="testResult.ok" type="success">
              连接正常 · 延迟 {{ testResult.latency_ms }}ms · {{ testResult.models_count }} 个模型
            </el-tag>
            <el-tag v-else type="danger">{{ testResult.error }}</el-tag>
          </div>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span>存储与清理</span>
          <div style="display: flex; align-items: center; gap: 10px">
            <el-tag v-if="storageStats.cleanup_running" type="warning" effect="light">清理中…</el-tag>
            <el-button size="small" @click="loadStorageStats">刷新</el-button>
            <el-button size="small" type="warning" plain :loading="cleaning" :disabled="storageStats.cleanup_running" @click="cleanupNow">
              立即执行一轮清理
            </el-button>
          </div>
        </div>
      </template>

      <div v-if="storageStats.disk" style="margin-bottom: 14px">
        <div style="display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px">
          <span class="text-muted">数据分区磁盘占用</span>
          <span>
            已用 {{ fmtBytes(storageStats.disk.used) }} / 共 {{ fmtBytes(storageStats.disk.total) }}
            （剩余 {{ fmtBytes(storageStats.disk.free) }}）
          </span>
        </div>
        <el-progress
          :percentage="Math.round((storageStats.disk.used / storageStats.disk.total) * 1000) / 10"
          :status="storageStats.disk.used / storageStats.disk.total > 0.9 ? 'exception' : storageStats.disk.used / storageStats.disk.total > 0.75 ? 'warning' : ''"
          :stroke-width="12"
        />
      </div>

      <el-row :gutter="16" style="margin-bottom: 8px">
        <el-col :span="6" :xs="12"><el-statistic title="产物总大小" :value="fmtBytes(storageStats.total_bytes)" /></el-col>
        <el-col :span="6" :xs="12"><el-statistic title="产物文件数" :value="storageStats.total_files" /></el-col>
        <el-col :span="6" :xs="12"><el-statistic title="数据库体积" :value="fmtBytes(storageStats.db_bytes)" /></el-col>
        <el-col :span="6" :xs="12"><el-statistic title="调用日志条数" :value="storageStats.total_logs" /></el-col>
      </el-row>

      <div v-if="storageStats.by_kind?.length" style="margin: 6px 0 12px">
        <el-tag v-for="k in storageStats.by_kind" :key="k.kind" effect="plain" type="info" style="margin-right: 6px">
          {{ KIND_LABELS[k.kind] || k.kind }} {{ k.files }} 个 · {{ fmtBytes(k.bytes) }}
        </el-tag>
      </div>

      <el-alert type="info" :closable="false" style="margin-bottom: 14px">
        <template #title>
          <span style="font-size: 13px">按当前保留策略可立即清理：{{ cleanableText }}</span>
          <div v-if="storageStats.last_cleanup?.at" class="text-muted" style="margin-top: 4px">
            上次手动清理：{{ fmtTime(storageStats.last_cleanup.at) }}
          </div>
        </template>
      </el-alert>

      <el-form label-width="220px">
        <el-form-item label="单用户存储上限（MB，0 不限）">
          <el-input-number v-model="form.storage_quota_mb_default" :min="0" :max="1048576" />
        </el-form-item>
        <el-form-item label="产物保留时长（小时，0 永久）">
          <el-input-number v-model="form.file_retention_hours" :min="0" :max="87600" />
          <span class="text-muted" style="margin-left: 10px">设为 24 即产物保存一天后自动删除</span>
        </el-form-item>
        <el-form-item label="日志保留时长（小时，0 永久）">
          <el-input-number v-model="form.log_retention_hours" :min="0" :max="87600" />
        </el-form-item>
        <el-form-item label="审计日志保留（天，0 永久）">
          <el-input-number v-model="form.audit_retention_days" :min="0" :max="3650" />
        </el-form-item>
      </el-form>
      <div class="text-muted">后台每小时自动执行一轮清理；手动清理与自动清理互斥，不会同时运行。</div>
      <el-table v-if="storageStats.top_users?.length" :data="storageStats.top_users" size="small" style="margin-top: 12px">
        <el-table-column prop="username" label="用户" />
        <el-table-column label="存储占用" width="140">
          <template #default="{ row }">{{ fmtBytes(row.bytes) }}</template>
        </el-table-column>
        <el-table-column prop="files" label="文件数" width="100" />
      </el-table>
    </el-card>

    <el-card style="margin-bottom: 16px">
      <template #header>用户协议（免责条款）</template>
      <el-form label-width="160px">
        <el-form-item label="协议版本">
          <el-input-number v-model="form.agreement_version" :min="1" :max="1000000" />
          <span class="text-muted" style="margin-left: 10px">调高版本后，所有用户需重新阅读并同意</span>
        </el-form-item>
        <el-form-item label="协议内容">
          <div style="width: 100%">
            <el-input
              v-model="form.agreement_text"
              type="textarea"
              :rows="10"
              maxlength="20000"
              placeholder="留空则使用内置默认协议"
            />
            <div style="display: flex; align-items: center; gap: 10px; margin-top: 6px; flex-wrap: wrap">
              <el-tag size="small" :type="form.agreement_text ? 'warning' : 'info'" effect="plain">
                {{ form.agreement_text ? '使用自定义协议' : '当前使用内置默认协议' }}
              </el-tag>
              <el-button size="small" text type="primary" @click="form.agreement_text = builtinDefaults.agreement">
                填入内置默认协议以二次修改
              </el-button>
              <el-button v-if="form.agreement_text" size="small" text type="danger" @click="form.agreement_text = ''">
                清空（恢复内置默认）
              </el-button>
            </div>
          </div>
        </el-form-item>
      </el-form>
      <div class="text-muted">支持 Markdown 格式；用户登录后须滚动阅读全文才能点击同意。</div>
    </el-card>

    <div style="text-align: right">
      <el-button type="primary" size="large" :loading="saving" @click="save">保存全部设置</el-button>
    </div>

    <el-dialog v-model="modelsVisible" title="上游模型目录" width="min(520px, 94vw)">
      <div v-loading="modelsLoading">
        <el-tag v-for="m in models" :key="m" style="margin: 4px">{{ m }}</el-tag>
        <el-empty v-if="!modelsLoading && !models.length" description="无模型" />
      </div>
    </el-dialog>
  </div>
</template>
