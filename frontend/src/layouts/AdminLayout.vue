<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { Odometer, User, Ticket, Document, Setting, Back, SwitchButton, Lock, Cloudy, Menu, Present } from '@element-plus/icons-vue'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const drawer = ref(false)
const isMobile = ref(false)

function checkMobile() {
  isMobile.value = window.innerWidth <= 768
}
onMounted(() => {
  checkMobile()
  window.addEventListener('resize', checkMobile)
})
onUnmounted(() => window.removeEventListener('resize', checkMobile))

const menus = [
  { path: '/admin', label: '仪表盘', icon: Odometer },
  { path: '/admin/users', label: '用户管理', icon: User },
  { path: '/admin/invites', label: '邀请码', icon: Ticket },
  { path: '/admin/redeem-codes', label: '兑换码', icon: Present },
  { path: '/admin/logs', label: '调用日志', icon: Document },
  { path: '/admin/risk', label: '风控中心', icon: Lock },
  { path: '/admin/upstream', label: '上游账号', icon: Cloudy },
  { path: '/admin/settings', label: '系统设置', icon: Setting },
]

function go(path) {
  router.push(path)
  drawer.value = false
}
</script>

<template>
  <el-container style="height: 100vh">
    <el-aside v-if="!isMobile" width="220px" class="layout-aside">
      <div class="layout-logo">{{ auth.siteName }} · 管理</div>
      <el-menu :default-active="route.path" class="layout-menu" @select="go">
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><Back /></el-icon>
          <span>返回用户端</span>
        </el-menu-item>
      </el-menu>
      <div class="layout-footer">
        <div class="text-muted" style="margin-bottom: 8px">管理员：{{ auth.user?.username }}</div>
        <el-button size="small" text :icon="SwitchButton" @click="auth.logout()">退出登录</el-button>
      </div>
    </el-aside>

    <el-drawer v-model="drawer" direction="ltr" size="240px" :with-header="false">
      <div class="layout-logo">{{ auth.siteName }} · 管理</div>
      <el-menu :default-active="route.path" class="layout-menu" @select="go">
        <el-menu-item v-for="m in menus" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><Back /></el-icon>
          <span>返回用户端</span>
        </el-menu-item>
      </el-menu>
      <div class="layout-footer" style="margin-top: 16px">
        <el-button size="small" text :icon="SwitchButton" @click="auth.logout()">退出登录</el-button>
      </div>
    </el-drawer>

    <el-container>
      <el-header v-if="isMobile" class="mobile-topbar">
        <el-button text :icon="Menu" @click="drawer = true" />
        <span class="mobile-title">{{ route.meta.title || '管理后台' }}</span>
      </el-header>
      <el-main style="padding: 0; overflow-y: auto">
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>
