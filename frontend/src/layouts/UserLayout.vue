<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore, FEATURE_LABELS } from '../stores/auth'
import {
  ChatDotRound, Brush, Search, Document, Folder, Key, User, Setting, SwitchButton, Menu,
} from '@element-plus/icons-vue'

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

const menus = computed(() => [
  { path: '/chat', label: '对话', icon: ChatDotRound, show: auth.features.chat },
  { path: '/draw', label: '绘图', icon: Brush, show: auth.features.image },
  { path: '/search', label: '搜索', icon: Search, show: auth.features.search },
  { path: '/ppt', label: 'PPT / PSD', icon: Document, show: auth.features.ppt },
  { path: '/files', label: '我的文件', icon: Folder, show: true },
  { path: '/keys', label: 'API 密钥', icon: Key, show: true },
  { path: '/profile', label: '个人中心', icon: User, show: true },
])

function go(path) {
  router.push(path)
  drawer.value = false
}
</script>

<template>
  <el-container style="height: 100vh">
    <!-- 桌面端侧边栏 -->
    <el-aside v-if="!isMobile" width="220px" class="layout-aside">
      <div class="layout-logo">{{ auth.siteName }}</div>
      <el-menu :default-active="route.path" class="layout-menu" @select="go">
        <el-menu-item v-for="m in menus.filter((x) => x.show)" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.isAdmin" index="/admin">
          <el-icon><Setting /></el-icon>
          <span>管理后台</span>
        </el-menu-item>
      </el-menu>
      <div class="layout-footer">
        <div class="quota-chips" style="margin-bottom: 10px">
          <el-tooltip v-for="(label, f) in FEATURE_LABELS" :key="f" :content="`${label}剩余次数`">
            <el-tag size="small" effect="plain" type="info">{{ label }} {{ auth.quotaText(f) }}</el-tag>
          </el-tooltip>
        </div>
        <el-button size="small" text :icon="SwitchButton" @click="auth.logout()">退出登录</el-button>
      </div>
    </el-aside>

    <!-- 移动端抽屉菜单 -->
    <el-drawer v-model="drawer" direction="ltr" size="240px" :with-header="false">
      <div class="layout-logo">{{ auth.siteName }}</div>
      <el-menu :default-active="route.path" class="layout-menu" @select="go">
        <el-menu-item v-for="m in menus.filter((x) => x.show)" :key="m.path" :index="m.path">
          <el-icon><component :is="m.icon" /></el-icon>
          <span>{{ m.label }}</span>
        </el-menu-item>
        <el-menu-item v-if="auth.isAdmin" index="/admin">
          <el-icon><Setting /></el-icon>
          <span>管理后台</span>
        </el-menu-item>
      </el-menu>
      <div class="layout-footer" style="margin-top: 16px">
        <el-button size="small" text :icon="SwitchButton" @click="auth.logout()">退出登录</el-button>
      </div>
    </el-drawer>

    <el-container>
      <el-header v-if="isMobile" class="mobile-topbar">
        <el-button text :icon="Menu" @click="drawer = true" />
        <span class="mobile-title">{{ route.meta.title || auth.siteName }}</span>
      </el-header>
      <el-main style="padding: 0; overflow-y: auto">
        <el-alert
          v-if="auth.announcement"
          :title="auth.announcement"
          type="info"
          :closable="false"
          show-icon
          style="border-radius: 0"
        />
        <router-view v-slot="{ Component }">
          <transition name="page-fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>
  </el-container>
</template>
