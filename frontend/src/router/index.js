import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  { path: '/login', component: () => import('../views/LoginView.vue'), meta: { public: true } },
  { path: '/register', component: () => import('../views/RegisterView.vue'), meta: { public: true } },
  { path: '/pending', component: () => import('../views/PendingView.vue'), meta: { title: '等待审核' } },
  {
    path: '/',
    component: () => import('../layouts/UserLayout.vue'),
    children: [
      { path: '', redirect: '/chat' },
      { path: 'chat', component: () => import('../views/ChatView.vue'), meta: { title: '对话' } },
      { path: 'draw', component: () => import('../views/DrawView.vue'), meta: { title: '绘图' } },
      { path: 'search', component: () => import('../views/SearchView.vue'), meta: { title: '搜索' } },
      { path: 'ppt', component: () => import('../views/PptView.vue'), meta: { title: 'PPT/PSD' } },
      { path: 'files', component: () => import('../views/FilesView.vue'), meta: { title: '我的文件' } },
      { path: 'keys', component: () => import('../views/KeysView.vue'), meta: { title: 'API 密钥' } },
      { path: 'profile', component: () => import('../views/ProfileView.vue'), meta: { title: '个人中心' } },
    ],
  },
  {
    path: '/admin',
    component: () => import('../layouts/AdminLayout.vue'),
    meta: { admin: true },
    children: [
      { path: '', component: () => import('../views/admin/DashboardView.vue'), meta: { title: '仪表盘' } },
      { path: 'users', component: () => import('../views/admin/UsersView.vue'), meta: { title: '用户管理' } },
      { path: 'invites', component: () => import('../views/admin/InvitesView.vue'), meta: { title: '邀请码' } },
      { path: 'redeem-codes', component: () => import('../views/admin/RedeemCodesView.vue'), meta: { title: '兑换码' } },
      { path: 'logs', component: () => import('../views/admin/LogsView.vue'), meta: { title: '调用日志' } },
      { path: 'risk', component: () => import('../views/admin/RiskView.vue'), meta: { title: '风控中心' } },
      { path: 'upstream', component: () => import('../views/admin/UpstreamAccountsView.vue'), meta: { title: '上游账号' } },
      { path: 'settings', component: () => import('../views/admin/SettingsView.vue'), meta: { title: '系统设置' } },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (to.meta.public) {
    if (auth.loaded && auth.user) return '/'
    try {
      await auth.fetchMe()
      return '/'
    } catch {
      return true
    }
  }
  try {
    await auth.fetchMe()
  } catch {
    return { path: '/login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : {} }
  }
  // 待审核账号只能停留在等待页
  if (auth.user?.status === 'pending') {
    return to.path === '/pending' ? true : '/pending'
  }
  if (to.path === '/pending') return '/'
  if (to.meta.admin && !auth.isAdmin) return '/'
  document.title = to.meta.title ? `${to.meta.title} · ${auth.siteName}` : auth.siteName
  return true
})

export default router
