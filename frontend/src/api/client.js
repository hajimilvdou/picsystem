/** 统一 fetch 封装：同源 Cookie + 防伪头 + 错误规整 */
export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : detail?.error?.message || detail?.message || `请求失败（${status}）`)
    this.status = status
    this.detail = detail
  }
}

async function parseBody(resp) {
  const type = resp.headers.get('content-type') || ''
  if (type.includes('application/json')) return resp.json()
  return resp.text()
}

export async function request(path, { method = 'GET', body, formData } = {}) {
  const headers = { 'x-requested-with': 'XMLHttpRequest' }
  let payload
  if (formData) {
    payload = formData
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(body)
  }
  let resp
  try {
    resp = await fetch(path, { method, headers, body: payload, credentials: 'same-origin' })
  } catch {
    throw new ApiError(0, '网络异常，请检查连接')
  }
  const data = await parseBody(resp)
  if (resp.status === 401 && !/^\/(login|register)/.test(location.pathname)) {
    location.href = '/login'
    throw new ApiError(401, '登录已失效')
  }
  if (resp.status === 428) {
    // 协议版本更新：刷新用户信息触发协议弹窗（AgreementDialog 监听 needAgreement）
    import('../stores/auth').then(({ useAuthStore }) => useAuthStore().fetchMe(true))
    throw new ApiError(428, typeof data === 'object' ? data.detail ?? '请先阅读并同意用户协议' : '请先阅读并同意用户协议')
  }
  if (!resp.ok) {
    throw new ApiError(resp.status, typeof data === 'object' ? data.detail ?? data : data)
  }
  return data
}

export const api = {
  get: (p) => request(p),
  post: (p, body) => request(p, { method: 'POST', body }),
  postForm: (p, formData) => request(p, { method: 'POST', formData }),
  patch: (p, body) => request(p, { method: 'PATCH', body }),
  del: (p) => request(p, { method: 'DELETE' }),
}

/**
 * 对话 SSE 流：解析 event: meta/delta/error/done
 */
export async function streamChat({ body, signal, onMeta, onDelta, onError, onDone }) {
  let resp
  try {
    resp = await fetch('/api/chat/completions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-requested-with': 'XMLHttpRequest' },
      body: JSON.stringify(body),
      credentials: 'same-origin',
      signal,
    })
  } catch (e) {
    if (e.name === 'AbortError') throw e
    throw new ApiError(0, '网络异常，请检查连接')
  }
  if (!resp.ok) {
    const data = await parseBody(resp)
    throw new ApiError(resp.status, typeof data === 'object' ? data.detail ?? data : data)
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buffer.indexOf('\n\n')) >= 0) {
      const block = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      let event = 'message'
      let dataStr = ''
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) dataStr += line.slice(5).trim()
      }
      if (!dataStr) continue
      let data
      try {
        data = JSON.parse(dataStr)
      } catch {
        continue
      }
      if (event === 'meta') onMeta?.(data)
      else if (event === 'delta') onDelta?.(data.text || '')
      else if (event === 'error') onError?.(data.detail || '生成失败')
      else if (event === 'done') onDone?.(data)
    }
  }
}
