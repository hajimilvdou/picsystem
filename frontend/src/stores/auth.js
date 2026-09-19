import { defineStore } from 'pinia'
import { api } from '../api/client'

export const FEATURE_LABELS = { chat: '对话', image: '绘图', search: '搜索', ppt: 'PPT/PSD' }

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null,
    quotas: {},
    storage: { used_bytes: 0, limit_mb: 0 },
    agreementVersion: 0,
    agreementRequired: 1,
    checkinEnabled: false,
    checkedInToday: false,
    features: { chat: true, image: true, search: true, ppt: true },
    siteName: 'PicSystem',
    announcement: '',
    registrationEnabled: true,
    loaded: false,
  }),
  getters: {
    isAdmin: (s) => s.user?.role === 'admin',
    needAgreement: (s) => !!s.user && s.agreementVersion < s.agreementRequired,
  },
  actions: {
    // 匿名可用：登录/注册页展示站点名与注册开关（不依赖会话）
    async fetchPublicConfig() {
      try {
        const data = await api.get('/api/auth/public-config')
        this.siteName = data.site_name || this.siteName
        this.registrationEnabled = data.registration_enabled !== false
        document.title = this.siteName
      } catch {
        /* 接口不可用时保持默认值 */
      }
    },
    async fetchMe(force = false) {
      if (this.loaded && !force) return
      const data = await api.get('/api/auth/me')
      this.user = { id: data.id, username: data.username, role: data.role, status: data.status }
      this.quotas = data.quotas || {}
      this.storage = data.storage || { used_bytes: 0, limit_mb: 0 }
      this.agreementVersion = data.agreement_version ?? 0
      this.agreementRequired = data.agreement_required ?? 1
      this.checkinEnabled = data.checkin_enabled || false
      this.checkedInToday = data.checked_in_today || false
      this.features = data.features || this.features
      this.siteName = data.site_name || 'PicSystem'
      this.announcement = data.announcement || ''
      this.registrationEnabled = data.registration_enabled !== false
      this.loaded = true
      document.title = this.siteName
    },
    async login(username, password) {
      await api.post('/api/auth/login', { username, password })
      this.loaded = false
      await this.fetchMe(true)
    },
    async register(username, password, inviteCode, fp, note) {
      const data = await api.post('/api/auth/register', {
        username: username || null,
        password: password || null,
        invite_code: inviteCode,
        fp: fp || '',
        note: note || '',
      })
      this.loaded = false
      await this.fetchMe(true)
      return data
    },
    async logout() {
      try {
        await api.post('/api/auth/logout')
      } finally {
        this.$reset()
        location.href = '/login'
      }
    },
    quotaText(feature) {
      const q = this.quotas[feature]
      if (!q) return '0'
      if (q.total < 0) return '不限'
      const rest = Math.max(q.total - q.used, 0) + (q.temp || 0)
      return `${rest}`
    },
  },
})
