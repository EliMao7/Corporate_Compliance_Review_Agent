import axios from 'axios'

/**
 * ============================================================
 * 认证：令牌与用户信息的唯一来源是 localStorage（§6.4 用户账号与数据隔离）
 *   ccr_token   —— JWT 访问令牌
 *   ccr_user    —— 当前用户名
 *   ccr_user_id —— 当前用户 ID（用于按用户隔离本地会话 ID）
 * ============================================================
 */
const TOKEN_KEY = 'ccr_token'
const USER_KEY = 'ccr_user'
const USER_ID_KEY = 'ccr_user_id'

// 认证状态订阅者：App.vue 据此在"登录态 / 未登录态"之间切换
const authListeners = new Set()

/** 读取当前访问令牌（无则空串） */
export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}

/** 读取当前用户名（无则空串） */
export function getUser() {
  return localStorage.getItem(USER_KEY) || ''
}

/** 读取当前用户 ID（无则空串） */
export function getUserId() {
  return localStorage.getItem(USER_ID_KEY) || ''
}

/**
 * 写入登录/注册成功后的令牌与用户信息，并通知订阅者（App 由此切到 ChatView）。
 * @param {{access_token:string, username?:string, user_id?:string}} data TokenResponse
 */
export function setAuth(data) {
  if (!data || !data.access_token) return
  localStorage.setItem(TOKEN_KEY, data.access_token)
  localStorage.setItem(USER_KEY, data.username || '')
  localStorage.setItem(USER_ID_KEY, data.user_id || '')
  notifyAuth()
}

/** 清空本地凭证并通知订阅者（App 由此回落到登录页） */
export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
  localStorage.removeItem(USER_ID_KEY)
  notifyAuth()
}

/** 退出登录：清空令牌与用户信息，App 自动回到登录页 */
export function logout() {
  clearAuth()
}

/**
 * 订阅认证状态变化，返回取消订阅函数。
 * @param {() => void} cb
 */
export function onAuthChange(cb) {
  authListeners.add(cb)
  return () => authListeners.delete(cb)
}

function notifyAuth() {
  for (const cb of authListeners) {
    try {
      cb()
    } catch (e) {
      console.warn('auth 状态回调异常:', e)
    }
  }
}

/**
 * axios 实例
 * - baseURL 为 /api：开发模式由 Vite 代理到 :8000，生产模式由 FastAPI 同源托管
 * - timeout 65s：略大于后端 60s 超时，确保后端先返回友好的 504，而非前端生硬掐断
 */
const http = axios.create({
  baseURL: '/api',
  timeout: 65000  // 略大于后端 60s 上限，避免后端仍在处理时前端先报错
})

// 请求拦截器：有令牌即附加 Authorization: Bearer <token>
http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截器：401（令牌缺失/失效）→ 清空本地凭证，App 自动回落登录页。
// 注意：拦截器内不做跳转，仅改状态，避免与路由/渲染形成循环。
http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const status = error?.response?.status
    const url = error?.config?.url || ''
    const isAuthEndpoint = url.includes('/auth/login') || url.includes('/auth/register')
    // 登录/注册自身的 401（凭据错误）不清理，交由页面内联提示
    if (status === 401 && !isAuthEndpoint) clearAuth()
    return Promise.reject(error)
  }
)

/**
 * 注册（POST /api/auth/register）
 * @param {string} username 用户名（后端要求 ≥ 3 字符）
 * @param {string} password 密码（后端要求 ≥ 6 字符）
 * @returns {Promise} TokenResponse { access_token, token_type, expires_in, user_id, username }
 */
export function register(username, password) {
  return http.post('/auth/register', { username, password })
}

/**
 * 登录（POST /api/auth/login）
 * @param {string} username 用户名
 * @param {string} password 密码
 * @returns {Promise} TokenResponse；凭据错误 → 401
 */
export function login(username, password) {
  return http.post('/auth/login', { username, password })
}

/**
 * 合规审查（R3 契约：src/api/models.py 是响应字段的唯一主人，共 12 个字段）
 * @param {string} query 用户问题
 * @param {string} sessionId 会话 ID
 * @returns {Promise} 后端 ConsultResponse：
 *   { answer, intent, intent_confidence, doc_type, matters, risk_signals,
 *     authorities, measures, disclaimers, warnings, linked_entities, duration_ms }
 */
export function consult(query, sessionId) {
  return http.post('/consult', { query, session_id: sessionId })
}

/**
 * 健康检查
 * @returns {Promise} { status, neo4j_connected, vector_index_loaded, agent_system_ready, ... }
 */
export function health() {
  return http.get('/health')
}

/**
 * 对话历史列表（按 updated_at 倒序，后端读 MySQL）
 * @returns {Promise} { conversations: [{id, title, created_at, updated_at}] }
 */
export function listConversations() {
  return http.get('/conversations')
}

/**
 * 恢复单个对话（全部消息，时间正序）
 * @param {string} id 对话 ID（= session_id）
 * @returns {Promise} { conversation_id, messages: [{role, content, intent, created_at}] }
 */
export function getConversation(id) {
  return http.get(`/conversations/${id}`)
}

/**
 * 删除对话（MySQL 两表 + Redis key 同步清空）
 * @param {string} id 对话 ID
 */
export function deleteConversation(id) {
  return http.delete(`/conversations/${id}`)
}

/**
 * 流式合规审查（SSE）
 * EventSource 不支持 POST，也不支持自定义请求头，故用 fetch + ReadableStream
 * 手工解析 SSE 帧，并在 fetch 上附加 Authorization: Bearer <token>。
 * 事件协议（对应后端 POST /api/consult/stream）：
 *   event: progress — { node, label, facts }      每个 Agent 节点完成的进度
 *   event: answer   — ConsultResponse JSON        完整终答（字段与 /consult 一致）
 *   event: error    — { message }                 错误信息
 *   event: done     — [DONE]                      结束标记
 *
 * @param {string} query 用户问题
 * @param {string} sessionId 会话 ID
 * @param {object} callbacks { onProgress, onAnswer, onError, onDone }
 * @returns {Promise<void>} 连接层失败时 reject（调用方据此降级回 consult()）
 */
export async function consultStream(query, sessionId, callbacks = {}) {
  // fetch 可自定义请求头（EventSource 不行），因此这里能带上 JWT
  const headers = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`

  const resp = await fetch('/api/consult/stream', {
    method: 'POST',
    headers,
    body: JSON.stringify({ query, session_id: sessionId })
  })

  // 令牌缺失/失效：清空本地凭证（App 自动回落登录页），并抛出带 401 标记的错误
  if (resp.status === 401) {
    clearAuth()
    const err = new Error('登录状态已失效，请重新登录。')
    err.status = 401
    throw err
  }
  if (!resp.ok || !resp.body) {
    throw new Error(`SSE 连接失败：HTTP ${resp.status}`)
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  const dispatch = (frame) => {
    // 一帧形如："event: progress\ndata: {...}"（data 可能多行）
    let event = 'message'
    const dataLines = []
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) event = line.slice(6).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
    }
    const data = dataLines.join('\n')
    try {
      if (event === 'progress') callbacks.onProgress?.(JSON.parse(data))
      else if (event === 'answer') callbacks.onAnswer?.(JSON.parse(data))
      else if (event === 'error') callbacks.onError?.(JSON.parse(data))
      else if (event === 'done') callbacks.onDone?.()
    } catch (e) {
      console.warn('SSE 帧解析失败:', event, data, e)
    }
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    // SSE 帧以空行（\n\n）分隔
    let sepIndex
    while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sepIndex)
      buffer = buffer.slice(sepIndex + 2)
      if (frame.trim()) dispatch(frame)
    }
  }
}

export default http
