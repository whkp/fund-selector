<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ArrowRight, Check, Compass, Lock, Mail, RefreshCw, ShieldCheck, Ticket, User } from 'lucide-vue-next'
import { fetchAuthPolicy, login, register, type AuthUser } from '../services/auth'
import { ApiError } from '../services/http'

const emit = defineEmits<{ authenticated: [AuthUser] }>()

const mode = ref<'login' | 'register'>('login')
const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const displayName = ref('')
const inviteCode = ref('')
const isSubmitting = ref(false)
const errorMessage = ref('')

// 默认按「需要邀请码」渲染。真实策略由 /auth/policy 决定，但万一那个请求
// 失败，宁可多显示一个输入框，也不要让界面把一个受控入口画成敞开的。
const inviteRequired = ref(true)

const isRegister = computed(() => mode.value === 'register')
const canSubmit = computed(() => {
  if (!email.value.trim() || !password.value) return false
  if (isRegister.value && password.value !== confirmPassword.value) return false
  if (isRegister.value && inviteRequired.value && !inviteCode.value.trim()) return false
  return true
})

onMounted(async () => {
  try {
    const policy = await fetchAuthPolicy()
    inviteRequired.value = policy.inviteRequired
  } catch {
    /* 保持保守默认值，不做任何提示：这只是展示层的优化。 */
  }
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
      ? await register(email.value.trim(), password.value, displayName.value.trim(), inviteCode.value.trim())
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
        <p class="auth-hint">{{ isRegister ? (inviteRequired ? '本系统为邀请制，注册需要邀请码。' : '注册后即可保存自选与研究记录。') : '输入邮箱与密码继续。' }}</p>

        <form @submit.prevent="submit">
          <label v-if="isRegister && inviteRequired">
            <span>邀请码</span>
            <div class="field"><Ticket :size="16" /><input v-model.trim="inviteCode" type="text" autocomplete="off" spellcheck="false" placeholder="向邀请你的人索取" required /></div>
          </label>
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
.auth-shell { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr); min-height: 100vh; background: #f3f5f9; }

/* 品牌侧：深海军蓝 + 极光浮动 */
.auth-brand {
  position: relative; overflow: hidden;
  display: flex; flex-direction: column; justify-content: center; gap: 22px;
  padding: 56px clamp(32px, 5vw, 78px); color: #c8d5ea;
  background: linear-gradient(200deg, #0a1220 0%, #0d1830 48%, #13264a 100%);
}
.auth-brand::before, .auth-brand::after {
  position: absolute; content: ''; border-radius: 50%; filter: blur(70px); pointer-events: none;
}
.auth-brand::before {
  top: -14%; right: -10%; width: 460px; height: 460px;
  background: radial-gradient(circle, rgba(42, 77, 128, .55), transparent 65%);
  animation: drift 13s ease-in-out infinite alternate;
}
.auth-brand::after {
  bottom: -18%; left: -12%; width: 420px; height: 420px;
  background: radial-gradient(circle, rgba(201, 162, 75, .22), transparent 65%);
  animation: drift 17s ease-in-out infinite alternate-reverse;
}
.auth-brand > * { position: relative; z-index: 1; animation: rise .6s cubic-bezier(.22, .9, .32, 1) both; }
.auth-brand > *:nth-child(2) { animation-delay: .08s; }
.auth-brand > *:nth-child(3) { animation-delay: .16s; }
.auth-brand > *:nth-child(4) { animation-delay: .24s; }
.auth-brand > *:nth-child(5) { animation-delay: .32s; }

.brand-head { display: flex; align-items: center; gap: 12px; }
.brand-mark { display: grid; width: 46px; height: 46px; color: #ddba72; place-items: center; border: 1px solid rgba(221, 186, 114, .55); border-radius: 50%; box-shadow: 0 0 22px rgba(221, 186, 114, .25), inset 0 0 10px rgba(221, 186, 114, .12); }
.brand-name { display: block; color: #fff; font-family: 'Noto Serif SC', serif; font-size: 21px; font-weight: 700; line-height: 1.1; letter-spacing: .02em; }
.brand-subtitle { display: block; margin-top: 3px; color: #7f90ad; font-family: 'IBM Plex Mono', monospace; font-size: 9px; letter-spacing: .14em; }
.auth-brand h1 { margin: 18px 0 0; color: #fff; font-family: 'Noto Serif SC', serif; font-size: clamp(27px, 3vw, 38px); font-weight: 600; line-height: 1.4; letter-spacing: .01em; }
.brand-lead { max-width: 430px; margin: 0; color: #9fb0cb; font-size: 13px; line-height: 1.85; }
.brand-points { display: grid; gap: 12px; margin: 4px 0 0; padding: 0; list-style: none; }
.brand-points li { display: flex; align-items: center; gap: 9px; color: #bfcde2; font-size: 12.5px; }
.brand-points svg { flex: 0 0 auto; color: #ddba72; }
.brand-foot { margin: 20px 0 0; padding-top: 18px; color: #66779a; font-family: 'IBM Plex Mono', monospace; font-size: 10px; letter-spacing: .1em; border-top: 1px solid rgba(148, 170, 205, .16); }

.auth-panel { display: grid; place-items: center; padding: 40px clamp(22px, 4vw, 60px); background: radial-gradient(700px 400px at 80% 0%, rgba(30, 58, 102, .05), transparent 60%); }
.auth-card { position: relative; width: 100%; max-width: 392px; overflow: hidden; padding: 30px 32px 26px; background: #fff; border: 1px solid #dfe5ef; border-radius: 14px; box-shadow: 0 2px 4px rgba(15, 26, 46, .05), 0 24px 56px rgba(15, 26, 46, .1); animation: card-in .55s cubic-bezier(.22, .9, .32, 1) .1s both; }
.auth-card::before { position: absolute; top: 0; right: 0; left: 0; height: 3px; content: ''; background: linear-gradient(90deg, #1a3358, #2a4d80 55%, #c9a24b); }
.auth-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 4px; background: #f0f3f9; border: 1px solid #e3e9f3; border-radius: 9px; }
.auth-tabs button { padding: 8px 0; color: #5f7191; font-size: 12.5px; font-weight: 600; background: transparent; border: 0; border-radius: 6px; transition: background .18s ease, color .18s ease, box-shadow .18s ease; }
.auth-tabs button.active { color: #14263f; background: #fff; box-shadow: 0 1px 4px rgba(15, 26, 46, .14); }
.auth-card h2 { margin: 22px 0 0; color: #0f1a2e; font-family: 'Noto Serif SC', serif; font-size: 22px; font-weight: 600; }
.auth-hint { margin: 7px 0 22px; color: #8494ad; font-size: 12px; }
.auth-card form { display: grid; gap: 15px; }
.auth-card label { display: grid; gap: 7px; }
.auth-card label > span { color: #5f7191; font-size: 11px; font-weight: 600; }
.field { display: flex; align-items: center; gap: 9px; padding: 0 11px; background: #f7f9fc; border: 1px solid #e3e9f3; border-radius: 8px; transition: border-color .18s ease, background .18s ease, box-shadow .18s ease; }
.field:focus-within { background: #fff; border-color: #1e3a66; box-shadow: 0 0 0 3px rgba(30, 58, 102, .12); }
.field svg { flex: 0 0 auto; color: #8494ad; transition: color .18s ease; }
.field:focus-within svg { color: #1e3a66; }
.field input { width: 100%; padding: 10px 0; color: #1b2b45; font-size: 13px; background: transparent; border: 0; outline: 0; }
.field input::placeholder { color: #a7b3c7; }

.auth-error { margin: 0; padding: 9px 11px; color: #96391f; font-size: 11.5px; line-height: 1.5; background: #fdf0ea; border: 1px solid #f0d0bd; border-radius: 8px; animation: rise .25s ease both; }
.auth-submit { position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-height: 42px; margin-top: 3px; overflow: hidden; color: #fff; font-size: 13px; font-weight: 700; background: linear-gradient(135deg, #1a3358, #2a4d80); border: 1px solid #16305a; border-radius: 8px; box-shadow: 0 4px 14px rgba(26, 51, 88, .3), inset 0 1px 0 rgba(255, 255, 255, .12); transition: transform .18s cubic-bezier(.22, .9, .32, 1), filter .18s ease, box-shadow .18s ease; }
.auth-submit::after { position: absolute; top: 0; bottom: 0; left: -80%; width: 45%; content: ''; background: linear-gradient(105deg, transparent, rgba(255, 255, 255, .22), transparent); transform: skewX(-18deg); transition: left .55s cubic-bezier(.22, .9, .32, 1); }
.auth-submit:hover:not(:disabled) { filter: brightness(1.12); transform: translateY(-1px); box-shadow: 0 7px 20px rgba(26, 51, 88, .36); }
.auth-submit:hover:not(:disabled)::after { left: 130%; }
.auth-submit:active:not(:disabled) { transform: scale(.98); }
.auth-submit:disabled { color: #e4e9f2; background: #9faec7; border-color: #96a6bf; box-shadow: none; cursor: not-allowed; }
.auth-submit svg { color: #ecd9a8; }
.spinning { animation: auth-spin .8s linear infinite; }
@keyframes auth-spin { to { transform: rotate(360deg); } }

.auth-switch { margin: 20px 0 0; color: #8494ad; font-size: 12px; text-align: center; }
.auth-switch button { padding: 0 2px; color: #1e3a66; font-size: 12px; font-weight: 600; background: none; border: 0; text-decoration: underline; text-underline-offset: 3px; transition: color .15s ease; }
.auth-switch button:hover { color: #a9822f; }
.auth-security { display: flex; align-items: flex-start; gap: 7px; margin: 18px 0 0; padding-top: 15px; color: #8494ad; font-size: 10.5px; line-height: 1.6; border-top: 1px solid #edf1f7; }
.auth-security svg { flex: 0 0 auto; margin-top: 1px; color: #17795c; }

@keyframes rise { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }
@keyframes card-in { from { opacity: 0; transform: translateY(18px) scale(.985); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes drift { from { transform: translate(0, 0) scale(1); } to { transform: translate(36px, 26px) scale(1.08); } }

@media (max-width: 880px) {
  .auth-shell { grid-template-columns: minmax(0, 1fr); }
  .auth-brand { padding: 38px clamp(22px, 6vw, 44px); }
  .auth-brand h1 { font-size: 25px; }
  .brand-points { display: none; }
  .auth-panel { padding: 32px clamp(18px, 5vw, 40px) 46px; }
}
@media (prefers-reduced-motion: reduce) {
  .auth-brand::before, .auth-brand::after, .auth-brand > *, .auth-card { animation: none !important; }
}
</style>
