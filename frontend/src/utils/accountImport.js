/**
 * 上游账号导入解析工具。
 *
 * 与 chatgpt2api v3.2.3 的导入口径保持一致（web-vue/src/views/accounts/accountImportRuntime.ts）：
 * - Access Token：一行一个，忽略空行与 `#` 注释行；
 * - Session JSON / CPA JSON / Sub2API JSON / 完整备份：任意嵌套结构，只要记录里能取到
 *   access_token / accessToken / token（可位于 credentials / credential / tokens / auth 子对象）；
 * - 同时兼容 refresh_token / refreshToken、id_token / idToken。
 *
 * 导入文件在浏览器侧解析成账号负载后提交，避免把原始备份整包传给上游。
 */

const ARCHIVE_ROW_KEYS = ['accounts', 'items', 'results']
const CREDENTIAL_CONTAINER_KEYS = ['credentials', 'credential', 'tokens', 'auth']
const ACCESS_TOKEN_ALIASES = ['access_token', 'accessToken', 'token']
const REFRESH_TOKEN_ALIASES = ['refresh_token', 'refreshToken']
const ID_TOKEN_ALIASES = ['id_token', 'idToken']

export function uniqueTokens(list) {
  return Array.from(new Set((list || []).map((t) => String(t || '').trim()).filter(Boolean)))
}

/** 每行一个 token；空行与 `#` 注释行会被忽略，重复项自动去重。 */
export function parseTokenLines(text) {
  return uniqueTokens(
    String(text || '')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#'))
  )
}

function asRecord(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value
}

function credentialText(sources, aliases) {
  for (const source of sources) {
    for (const alias of aliases) {
      const value = String(source[alias] ?? '').trim()
      if (value) return value
    }
  }
  return ''
}

/** 把一条记录规范化为可提交的账号负载；取不到 access token 时返回 null。 */
export function normalizeAccountImportPayload(value) {
  const source = asRecord(value)
  if (!source) return null
  const nested = CREDENTIAL_CONTAINER_KEYS
    .map((key) => asRecord(source[key]))
    .filter(Boolean)
  const sources = [...nested, source]
  const accessToken = credentialText(sources, ACCESS_TOKEN_ALIASES)
  if (!accessToken) return null

  const refreshToken = credentialText(sources, REFRESH_TOKEN_ALIASES)
  const idToken = credentialText(sources, ID_TOKEN_ALIASES)
  const payload = { ...source, access_token: accessToken }
  if (refreshToken) payload.refresh_token = refreshToken
  if (idToken) payload.id_token = idToken
  return payload
}

function archiveRows(value) {
  if (Array.isArray(value)) return value
  const root = asRecord(value)
  if (!root) return []
  if (normalizeAccountImportPayload(root)) return [root]

  const rows = []
  for (const key of ARCHIVE_ROW_KEYS) {
    if (Array.isArray(root[key])) rows.push(...root[key])
  }
  const data = root.data
  if (Array.isArray(data)) {
    rows.push(...data)
  } else {
    const dataRecord = asRecord(data)
    if (dataRecord) {
      if (normalizeAccountImportPayload(dataRecord)) rows.push(dataRecord)
      for (const key of ARCHIVE_ROW_KEYS) {
        if (Array.isArray(dataRecord[key])) rows.push(...dataRecord[key])
      }
    }
  }
  return rows
}

/** 解析备份 / CPA / Sub2API JSON 文本，返回账号负载数组。 */
export function parseAccountArchive(rawText, label = '文件') {
  const text = String(rawText || '').trim()
  if (!text) throw new Error(`${label} 是空文件`)
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error(`${label} 不是合法的 JSON`)
  }
  const accounts = archiveRows(parsed)
    .map(normalizeAccountImportPayload)
    .filter(Boolean)
  if (!accounts.length) throw new Error(`${label} 中没有找到 access_token`)
  return accounts
}

/** 解析 chatgpt.com/api/auth/session 返回的会话 JSON（通常只含 AT）。 */
export function parseSessionJsonPayload(rawText) {
  const text = String(rawText || '').trim()
  if (!text) throw new Error('请先粘贴 Session JSON')
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    throw new Error('Session JSON 格式不正确')
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('Session JSON 格式不正确')
  }
  const payload = normalizeAccountImportPayload(parsed)
  if (!payload) throw new Error('Session JSON 中没有找到 accessToken')
  return payload
}

/** 读取本地文件文本（多选时按顺序合并）。 */
export async function readAccountFiles(fileList) {
  const files = Array.from(fileList || [])
  const accounts = []
  const errors = []
  for (const file of files) {
    try {
      const text = await file.text()
      accounts.push(...parseAccountArchive(text, file.name))
    } catch (e) {
      errors.push(`${file.name}：${e.message}`)
    }
  }
  return { accounts, errors }
}
