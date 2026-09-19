<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import { copyText } from '../utils/format'

const auth = useAuthStore()
const router = useRouter()
const mode = ref('custom') // custom | random
const form = reactive({ username: '', password: '', confirm: '', inviteCode: '', note: '' })
const loading = ref(false)
const generated = ref(null) // {username, password}

onMounted(() => auth.fetchPublicConfig())

// 设备指纹：localStorage 持久随机串 + UA，防单人批量注册（仅存哈希）。
// 不用 crypto.randomUUID（非 HTTPS 环境不可用），getRandomValues 无此限制。
function getFp() {
  let rid = localStorage.getItem('ps_rid')
  if (!rid) {
    const buf = new Uint8Array(16)
    crypto.getRandomValues(buf)
    rid = Array.from(buf, (b) => b.toString(16).padStart(2, '0')).join('')
    localStorage.setItem('ps_rid', rid)
  }
  return rid + '|' + navigator.userAgent.slice(0, 60)
}

async function submit() {
  if (mode.value === 'custom') {
    if (!/^[A-Za-z0-9_\-]{3,32}$/.test(form.username)) {
      ElMessage.warning('用户名需为 3-32 位字母、数字、下划线或连字符')
      return
    }
    if (form.password.length < 8) {
      ElMessage.warning('密码长度至少 8 位')
      return
    }
    if (form.password !== form.confirm) {
      ElMessage.warning('两次输入的密码不一致')
      return
    }
  }
  if (!form.inviteCode) {
    ElMessage.warning('请输入邀请码')
    return
  }
  loading.value = true
  try {
    const data = await auth.register(
      mode.value === 'custom' ? form.username : null,
      mode.value === 'custom' ? form.password : null,
      form.inviteCode,
      getFp(),
      form.note
    )
    if (data.generated) {
      generated.value = { username: data.username, password: data.password, pending: data.pending }
    } else if (data.pending) {
      ElMessage.success('申请已提交，等待管理员审核')
      router.push('/pending')
    } else {
      ElMessage.success('注册成功，已自动登录')
      router.push('/')
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function copyCredentials() {
  await copyText(`用户名：${generated.value.username}\n密码：${generated.value.password}`)
  ElMessage.success('已复制')
}

function goHome() {
  router.push(generated.value?.pending ? '/pending' : '/')
}
</script>

<template>
  <div class="auth-wrap">
    <el-card class="auth-card">
      <!-- 注册已关闭 -->
      <template v-if="!auth.registrationEnabled">
        <el-result icon="warning" title="注册暂未开放" sub-title="本站当前已关闭注册，如需账号请联系管理员获取">
          <template #extra>
            <el-button type="primary" @click="router.push('/login')">返回登录</el-button>
          </template>
        </el-result>
      </template>
      <!-- 随机凭证展示（注册成功且为随机模式） -->
      <template v-else-if="generated">
        <h2 class="auth-title">注册成功</h2>
        <el-alert type="warning" :closable="false" title="系统随机生成的账号凭证仅此一次完整展示，请立即保存" style="margin-bottom: 16px" />
        <div class="cred-box">
          <div><span class="text-muted">用户名：</span><code>{{ generated.username }}</code></div>
          <div><span class="text-muted">密码：</span><code>{{ generated.password }}</code></div>
        </div>
        <el-button type="primary" plain style="width: 100%; margin-bottom: 10px" @click="copyCredentials">复制凭证</el-button>
        <el-button type="primary" style="width: 100%" @click="goHome">已保存，进入系统</el-button>
      </template>

      <template v-else>
        <h2 class="auth-title">注册账号</h2>
        <p class="text-muted" style="text-align: center; margin-top: -12px">需要有效的邀请码</p>
        <el-tabs v-model="mode" stretch style="margin-bottom: 10px">
          <el-tab-pane label="自定义账号" name="custom" />
          <el-tab-pane label="随机生成账号" name="random" />
        </el-tabs>
        <el-form @submit.prevent="submit">
          <template v-if="mode === 'custom'">
            <el-form-item>
              <el-input v-model="form.username" placeholder="用户名（3-32 位字母数字_-）" size="large" autocomplete="username" />
            </el-form-item>
            <el-form-item>
              <el-input v-model="form.password" type="password" placeholder="密码（至少 8 位）" size="large" show-password autocomplete="new-password" />
            </el-form-item>
            <el-form-item>
              <el-input v-model="form.confirm" type="password" placeholder="确认密码" size="large" show-password autocomplete="new-password" />
            </el-form-item>
          </template>
          <el-alert v-else type="info" :closable="false" style="margin-bottom: 16px"
            title="系统将随机生成一组用户名和密码，注册后完整展示一次，可登录后在个人中心修改" />
          <el-form-item>
            <el-input v-model="form.inviteCode" placeholder="邀请码" size="large" @keyup.enter="submit" />
          </el-form-item>
          <el-form-item>
            <el-input
              v-model="form.note"
              type="textarea"
              :rows="2"
              maxlength="300"
              placeholder="申请备注（选填，如管理员要求填写约定内容）"
            />
          </el-form-item>
          <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="submit">
            注 册
          </el-button>
        </el-form>
        <div style="text-align: center; margin-top: 16px">
          <span class="text-muted">已有账号？</span>
          <router-link to="/login" style="color: var(--ps-primary)">直接登录</router-link>
        </div>
      </template>
    </el-card>
  </div>
</template>
