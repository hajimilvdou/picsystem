<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()
const checking = ref(false)
let timer = null

async function poll() {
  checking.value = true
  try {
    await auth.fetchMe(true)
    if (auth.user?.status === 'active') {
      router.push('/')
    }
  } catch {
    /* 401 等情况由守卫处理 */
  } finally {
    checking.value = false
  }
}

onMounted(() => {
  timer = setInterval(poll, 5000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="auth-wrap">
    <el-card class="auth-card pending-card">
      <el-result icon="info" title="注册申请已提交" sub-title="管理员审核通过后即可正常使用，本页面会自动刷新状态">
        <template #extra>
          <el-button type="primary" :loading="checking" @click="poll">立即检查</el-button>
          <el-button text @click="auth.logout()">退出登录</el-button>
        </template>
      </el-result>
      <div class="text-muted" style="text-align: center; margin-top: 8px">
        账号：{{ auth.user?.username }} · 每 5 秒自动检查一次
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.pending-card {
  width: min(520px, 94vw);
}
</style>
