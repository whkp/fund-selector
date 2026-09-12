<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowRight, Check, Compass, Lock, Mail, RefreshCw, ShieldCheck, User } from 'lucide-vue-next'
import { login, register, type AuthUser } from '../services/auth'
import { ApiError } from '../services/http'

const emit = defineEmits<{ authenticated: [AuthUser] }>()

const mode = ref<'login' | 'register'>('login')
const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const displayName = ref('')
const isSubmitting = ref(false)
const errorMessage = ref('')

const isRegister = computed(() => mode.value === 'register')
const canSubmit = computed(() => {
  if (!email.value.trim() || !password.value) return false
  if (isRegister.value && password.value !== confirmPassword.value) return false
  return true
})

function switchMode(next: 'login' | 'register') {
  mode.value = next
  errorMessage.value = ''
}

async function submit() {
  if (isSubmitting.value) return
  errorMessage.value = ''
  if (isRegister.value && password.value !== confirmPassword.value) {
    errorMessage.value = '两次输入的密码不一致'
    return
  }
  isSubmitting.value = true
  try {
    const user = isRegister.value
      ? await register(email.value.trim(), password.value, displayName.value.trim())
      : await login(email.value.trim(), password.value)
    emit('authenticated', user)
  } catch (error) {
    errorMessage.value = error instanceof ApiError ? error.message : '登录失败，请稍后重试'
  } finally {
    isSubmitting.value = false
  }
}
</script>

<template>
  <main class="auth-shell">
    <section class="auth-brand">
      <div class="brand-head">
        <div class="brand-mark"><Compass :size="26" stroke-width="1.7" /></div>
        <div>
          <span class="brand-name">基金罗盘</span>
          <span class="brand-subtitle">FUND COMPASS</span>
        </div>
      </div>

      <h1>从目标开始，<br />建立你的候选池</h1>
      <p class="brand-lead">登录之后，你的观察列表、筛选条件和每一次研究结论都归属到自己的账号，换台设备也能接着看。</p>

      <ul class="brand-points">
        <li><Check :size="15" />自选与研究记录按账号隔离，互不可见</li>
        <li><Check :size="15" />候选来自 AKShare 公开参考数据，不用模拟数据填充</li>
        <li><Check :size="15" />模型结论由服务端校验，不构成投资建议</li>
      </ul>

      <p class="brand-foot">数据来源 · AKShare 公开参考</p>
    </section>

    <section class="auth-panel">
      <div class="auth-card">
        <div class="auth-tabs" role="tablist">
          <button role="tab" :aria-selected="!isRegister" :class="{ active: !isRegister }" @click="switchMode('login')">登录</button>
          <button role="tab" :aria-selected="isRegister" :class="{ active: isRegister }" @click="switchMode('register')">注册</button>
        </div>

        <h2>{{ isRegister ? '创建你的账号' : '欢迎回来' }}</h2>
        <p class="auth-hint">{{ isRegister ? '注册后即可保存自选与研究记录。' : '输入邮箱与密码继续。' }}</p>

        <form @submit.prevent="submit">
          <label v-if="isRegister">
            <span>称呼</span>
            <div class="field"><User :size="16" /><input v-model.trim="displayName" type="text" autocomplete="name" placeholder="选填，用于界面显示" /></div>
          </label>
          <label>
            <span>邮箱</span>
            <div class="field"><Mail :size="16" /><input v-model.trim="email" type="email" autocomplete="email" placeholder="you@example.com" required /></div>
          </label>
          <label>
            <span>密码</span>
            <div class="field"><Lock :size="16" /><input v-model="password" type="password" :autocomplete="isRegister ? 'new-password' : 'current-password'" :placeholder="isRegister ? '至少 8 位' : '输入密码'" required /></div>
          </label>
          <label v-if="isRegister">
            <span>确认密码</span>
            <div class="field"><Lock :size="16" /><input v-model="confirmPassword" type="password" autocomplete="new-password" placeholder="再输入一次" required /></div>
          </label>

          <p v-if="errorMessage" class="auth-error" role="alert">{{ errorMessage }}</p>

          <button class="auth-submit" type="submit" :disabled="!canSubmit || isSubmitting">
            <RefreshCw v-if="isSubmitting" :size="16" class="spinning" />
            <ArrowRight v-else :size="16" />
            {{ isSubmitting ? '处理中…' : (isRegister ? '注册并登录' : '登录') }}
          </button>
        </form>

        <p class="auth-switch">
          {{ isRegister ? '已经有账号了？' : '还没有账号？' }}
          <button @click="switchMode(isRegister ? 'login' : 'register')">{{ isRegister ? '去登录' : '去注册' }}</button>
        </p>

        <p class="auth-security"><ShieldCheck :size="14" />密码以 PBKDF2 加盐哈希存储，登录令牌只留在你自己的浏览器里。</p>
      </div>
    </section>
  </main>
</template>

<style scoped>
.auth-shell { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); min-height: 100vh; background: #f7f7f2; }

.auth-brand {
  display: flex; flex-direction: column; justify-content: center; gap: 22px;
  padding: 56px clamp(32px, 5vw, 78px); color: #e6f0ec;
  background: radial-gradient(circle at 16% 18%, #1d4a52 0%, #122d35 52%, #0d2229 100%);
}
.brand-head { display: flex; align-items: center; gap: 12px; }
.brand-mark { display: grid; width: 46px; height: 46px; color: #f5c568; place-items: center; border: 1px solid rgba(245, 197, 104, .6); border-radius: 50%; }
.brand-name { display: block; color: #fff; font-family: 'Noto Serif SC', serif; font-size: 21px; font-weight: 700; line-height: 1.1; }
.brand-subtitle { display: block; margin-top: 3px; color: #81a9a5; font-family: 'IBM Plex Mono', monospace; font-size: 9px; letter-spacing: .09em; }
.auth-brand h1 { margin: 18px 0 0; color: #fff; font-family: 'Noto Serif SC', serif; font-size: clamp(27px, 3vw, 38px); font-weight: 600; line-height: 1.4; }
.brand-lead { max-width: 430px; margin: 0; color: #a8c8c2; font-size: 13px; line-height: 1.85; }
.brand-points { display: grid; gap: 12px; margin: 4px 0 0; padding: 0; list-style: none; }
.brand-points li { display: flex; align-items: center; gap: 9px; color: #cfe1dc; font-size: 12.5px; }
.brand-points svg { flex: 0 0 auto; color: #7dc7a5; }
.brand-foot { margin: 20px 0 0; padding-top: 18px; color: #6f9490; font-family: 'IBM Plex Mono', monospace; font-size: 10px; letter-spacing: .06em; border-top: 1px solid rgba(151, 202, 191, .16); }

.auth-panel { display: grid; place-items: center; padding: 40px clamp(22px, 4vw, 60px); }
.auth-card { width: 100%; max-width: 392px; padding: 30px 32px 26px; background: #fff; border: 1px solid #dce6e1; border-top: 3px solid #1d6b68; border-radius: 7px; box-shadow: 0 16px 34px rgba(19, 55, 51, .07); }
.auth-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 4px; background: #f2f6f3; border: 1px solid #e2eae5; border-radius: 6px; }
.auth-tabs button { padding: 8px 0; color: #6b807d; font-size: 12.5px; font-weight: 600; background: transparent; border: 0; border-radius: 4px; transition: background .16s ease, color .16s ease; }
.auth-tabs button.active { color: #14413f; background: #fff; box-shadow: 0 1px 3px rgba(24, 70, 65, .12); }
.auth-card h2 { margin: 22px 0 0; color: #152e34; font-family: 'Noto Serif SC', serif; font-size: 22px; font-weight: 600; }
.auth-hint { margin: 7px 0 22px; color: #81918e; font-size: 12px; }
.auth-card form { display: grid; gap: 15px; }
.auth-card label { display: grid; gap: 7px; }
.auth-card label > span { color: #576e6d; font-size: 11px; font-weight: 600; }
.field { display: flex; align-items: center; gap: 9px; padding: 0 11px; background: #f8faf8; border: 1px solid #e0e8e4; border-radius: 5px; transition: border-color .16s ease, background .16s ease; }
.field:focus-within { background: #fff; border-color: #1d6b68; }
.field svg { flex: 0 0 auto; color: #85a09a; }
.field input { width: 100%; padding: 10px 0; color: #173137; font-size: 13px; background: transparent; border: 0; outline: 0; }
.field input::placeholder { color: #a6b3b0; }

.auth-error { margin: 0; padding: 9px 11px; color: #9a4b32; font-size: 11.5px; line-height: 1.5; background: #fdf1ec; border: 1px solid #f2d6c8; border-radius: 5px; }
.auth-submit { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 42px; margin-top: 3px; color: #fff; font-size: 13px; font-weight: 700; background: #1d6b68; border: 1px solid #185c59; border-radius: 5px; box-shadow: 0 3px 8px rgba(29, 107, 104, .2); transition: background .16s ease; }
.auth-submit:hover:not(:disabled) { background: #145b59; }
.auth-submit:disabled { color: #e4ece9; background: #9db8b2; border-color: #93afa9; box-shadow: none; cursor: not-allowed; }
.auth-submit svg { color: #f1d27f; }
.spinning { animation: auth-spin .8s linear infinite; }
@keyframes auth-spin { to { transform: rotate(360deg); } }

.auth-switch { margin: 20px 0 0; color: #81918e; font-size: 12px; text-align: center; }
.auth-switch button { padding: 0 2px; color: #1d6b68; font-size: 12px; font-weight: 600; background: none; border: 0; text-decoration: underline; text-underline-offset: 3px; }
.auth-security { display: flex; align-items: flex-start; gap: 7px; margin: 18px 0 0; padding-top: 15px; color: #93a29e; font-size: 10.5px; line-height: 1.6; border-top: 1px solid #eef2ef; }
.auth-security svg { flex: 0 0 auto; margin-top: 1px; color: #5a9482; }

@media (max-width: 880px) {
  .auth-shell { grid-template-columns: minmax(0, 1fr); }
  .auth-brand { padding: 38px clamp(22px, 6vw, 44px); }
  .auth-brand h1 { font-size: 25px; }
  .brand-points { display: none; }
  .auth-panel { padding: 32px clamp(18px, 5vw, 40px) 46px; }
}
</style>
