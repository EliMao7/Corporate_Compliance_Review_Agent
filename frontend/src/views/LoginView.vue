<!--
  ============================================================
  LoginView.vue —— 登录 / 注册页（§6.4 用户账号与数据隔离）
  - 居中 el-card，登录 / 注册 切换
  - el-form 校验：用户名 ≥ 3，密码 ≥ 6；注册需二次确认
  - 后端错误内联展示：409 → 用户名已存在；401 → 用户名或密码错误
  - 提交 loading，回车提交
  - 与 ChatView 同一套视觉语言（App.vue 中的 CSS 变量）
  ============================================================
-->
<script setup>
import { ref, reactive, computed, nextTick } from 'vue'
import { login, register, setAuth } from '@/api/index.js'

// 当前模式：登录 / 注册
const mode = ref('login')
const isRegister = computed(() => mode.value === 'register')

const formRef = ref(null)
const submitting = ref(false)
const errorMsg = ref('')

const form = reactive({
  username: '',
  password: '',
  confirm: ''
})

// 二次密码校验（仅注册模式生效）
function validateConfirm(_rule, value, callback) {
  if (!isRegister.value) return callback()
  if (!value) return callback(new Error('请再次输入密码'))
  if (value !== form.password) return callback(new Error('两次输入的密码不一致'))
  callback()
}

const rules = computed(() => ({
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 64, message: '用户名至少 3 个字符', trigger: 'blur' }
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 128, message: '密码至少 6 个字符', trigger: 'blur' }
  ],
  confirm: [{ validator: validateConfirm, trigger: 'blur' }]
}))

function switchMode(next) {
  if (mode.value === next) return
  mode.value = next
  errorMsg.value = ''
  form.confirm = ''
  nextTick(() => formRef.value?.clearValidate())
}

async function submit() {
  if (submitting.value) return
  errorMsg.value = ''
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    const username = form.username.trim()
    const { data } = isRegister.value
      ? await register(username, form.password)
      : await login(username, form.password)
    // 写入 localStorage 并通知 App → 自动切换到 ChatView
    setAuth(data)
  } catch (err) {
    errorMsg.value = resolveError(err)
  } finally {
    submitting.value = false
  }
}

// 后端错误 → 中文内联文案
function resolveError(err) {
  const status = err?.response?.status
  const detail = err?.response?.data?.detail
  if (status === 409) return '用户名已存在，请更换后重试。'
  if (status === 401 || /invalid username or password/i.test(detail || '')) {
    return '用户名或密码错误。'
  }
  if (status === 422) {
    return isRegister.value ? '用户名至少 3 位、密码至少 6 位。' : '请检查用户名与密码格式。'
  }
  if (status === 429) return '操作过于频繁，请稍后再试。'
  if (!err?.response) return '无法连接后端，请确认服务已启动（:8000）。'
  return `请求失败（${status}），请稍后重试。`
}
</script>

<template>
  <div class="login-shell">
    <!-- 环境氛围层，与主界面同源 -->
    <div class="ambient" aria-hidden="true">
      <span class="glow glow-a"></span>
      <span class="glow glow-b"></span>
      <span class="grid-pattern"></span>
    </div>

    <el-card class="login-card" shadow="always">
      <div class="brand">
        <span class="brand-mark"><el-icon :size="26"><DocumentChecked /></el-icon></span>
        <h1 class="brand-name">企业合规审查 Agent 系统</h1>
        <p class="brand-sub">企业合规 · 知识图谱驱动</p>
      </div>

      <p class="intro">
        登录后即可开展制度与合同的合规排查。您的对话数据按账号隔离，仅本人可见。
      </p>

      <!-- 登录 / 注册 切换 -->
      <div class="mode-toggle" role="tablist">
        <button
          class="mode-btn"
          :class="{ active: mode === 'login' }"
          role="tab"
          :aria-selected="mode === 'login'"
          @click="switchMode('login')"
        >
          登录
        </button>
        <button
          class="mode-btn"
          :class="{ active: mode === 'register' }"
          role="tab"
          :aria-selected="mode === 'register'"
          @click="switchMode('register')"
        >
          注册
        </button>
      </div>

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        class="login-form"
        @submit.prevent="submit"
      >
        <el-form-item label="用户名" prop="username">
          <el-input
            v-model="form.username"
            size="large"
            :disabled="submitting"
            placeholder="请输入用户名（至少 3 个字符）"
            autocomplete="username"
            @keyup.enter="submit"
          >
            <template #prefix><el-icon><User /></el-icon></template>
          </el-input>
        </el-form-item>

        <el-form-item label="密码" prop="password">
          <el-input
            v-model="form.password"
            type="password"
            size="large"
            show-password
            :disabled="submitting"
            placeholder="请输入密码（至少 6 个字符）"
            :autocomplete="isRegister ? 'new-password' : 'current-password'"
            @keyup.enter="submit"
          >
            <template #prefix><el-icon><Lock /></el-icon></template>
          </el-input>
        </el-form-item>

        <el-form-item v-if="isRegister" label="确认密码" prop="confirm">
          <el-input
            v-model="form.confirm"
            type="password"
            size="large"
            show-password
            :disabled="submitting"
            placeholder="请再次输入密码"
            autocomplete="new-password"
            @keyup.enter="submit"
          >
            <template #prefix><el-icon><Lock /></el-icon></template>
          </el-input>
        </el-form-item>

        <!-- 后端错误内联提示 -->
        <el-alert
          v-if="errorMsg"
          class="form-error"
          type="error"
          :closable="false"
          show-icon
          :title="errorMsg"
        />

        <el-button
          class="submit-btn"
          type="primary"
          size="large"
          :loading="submitting"
          @click="submit"
        >
          {{ submitting ? '处理中' : (isRegister ? '注册并进入' : '登录') }}
        </el-button>
      </el-form>

      <p class="switch-hint">
        <template v-if="isRegister">
          已有账号？<a @click="switchMode('login')">返回登录</a>
        </template>
        <template v-else>
          还没有账号？<a @click="switchMode('register')">注册新账号</a>
        </template>
      </p>

      <!-- 合规底线：进入前先让用户看到边界 -->
      <el-alert
        class="login-disclaimer"
        type="warning"
        :closable="false"
        show-icon
        title="本系统只提供法规依据检索与风险提示，不提供法律意见，也不对文件是否合法作出结论。"
      />
    </el-card>
  </div>
</template>

<style scoped>
.login-shell {
  position: relative;
  height: 100%;
  display: grid;
  place-items: center;
  padding: 24px;
  overflow: hidden;
}

/* 环境氛围（与 ChatView 一致） */
.ambient { position: absolute; inset: 0; pointer-events: none; overflow: hidden; }
.glow { position: absolute; border-radius: 50%; filter: blur(90px); opacity: 0.5; }
.glow-a { width: 520px; height: 520px; top: -180px; left: -140px;
  background: radial-gradient(circle, rgba(20, 184, 166, 0.32), transparent 70%);
  animation: drift 18s ease-in-out infinite alternate; }
.glow-b { width: 460px; height: 460px; bottom: -160px; right: -120px;
  background: radial-gradient(circle, rgba(232, 137, 107, 0.26), transparent 70%);
  animation: drift 22s ease-in-out infinite alternate-reverse; }
.grid-pattern { position: absolute; inset: 0;
  background-image: linear-gradient(rgba(15,118,110,.05) 2px, transparent 2px),
    linear-gradient(90deg, rgba(15,118,110,.05) 2px, transparent 2px);
  background-size: 44px 44px;
  mask-image: radial-gradient(ellipse at 50% 0%, black 0%, transparent 80%); }
@keyframes drift { from { transform: translate(0,0) scale(1); } to { transform: translate(60px,40px) scale(1.12); } }

/* 卡片 */
.login-card {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 440px;
  border-radius: 20px;
  border: 1px solid rgba(15, 118, 110, 0.12);
  background: rgba(255, 255, 255, 0.9);
  backdrop-filter: blur(10px);
  box-shadow: 0 20px 50px rgba(11, 61, 58, 0.14);
  animation: rise 0.5s ease both;
}
@keyframes rise { from { opacity: 0; transform: translateY(18px); } to { opacity: 1; transform: none; } }
.login-card :deep(.el-card__body) { padding: 34px 32px 30px; }

/* 品牌区 */
.brand { text-align: center; margin-bottom: 14px; }
.brand-mark {
  display: inline-grid; place-items: center; width: 56px; height: 56px;
  border-radius: 16px; color: #fff; margin-bottom: 12px;
  background: linear-gradient(145deg, var(--teal-600), var(--teal-900));
  box-shadow: 0 8px 20px rgba(11, 61, 58, 0.28);
  animation: brand-pulse 2.6s ease-in-out infinite;
}
@keyframes brand-pulse { 0%,100%{transform:scale(1)} 50%{transform:scale(1.06)} }
.brand-name { font-family: var(--font-display); font-size: 23px; font-weight: 900; color: var(--teal-900); }
.brand-sub { font-size: 12px; color: #5c8480; letter-spacing: 0.08em; margin-top: 4px; }

.intro { font-size: 13px; line-height: 1.7; color: #5c8480; text-align: center; margin-bottom: 18px; }

/* 模式切换 */
.mode-toggle {
  display: grid; grid-template-columns: 1fr 1fr; gap: 4px;
  padding: 4px; margin-bottom: 20px; border-radius: 12px;
  background: rgba(217, 242, 236, 0.6);
  border: 1px solid rgba(15, 118, 110, 0.12);
}
.mode-btn {
  padding: 9px 0; border: none; border-radius: 9px; cursor: pointer;
  font-family: inherit; font-size: 14px; font-weight: 600; color: #46605c;
  background: transparent; transition: background 0.18s, color 0.18s, box-shadow 0.18s;
}
.mode-btn.active {
  color: #fff; background: linear-gradient(145deg, var(--teal-500), var(--teal-700));
  box-shadow: 0 4px 12px rgba(15, 118, 110, 0.28);
}

.login-form :deep(.el-form-item__label) { font-weight: 600; color: var(--ink); }
.login-form :deep(.el-input__wrapper) { border-radius: 12px; }

.form-error { margin-bottom: 16px; border-radius: 10px; }

.submit-btn {
  width: 100%; border: none; border-radius: 13px; height: 46px;
  font-size: 15px; font-weight: 700;
  background: linear-gradient(145deg, var(--teal-500), var(--teal-700));
  box-shadow: 0 6px 18px rgba(15, 118, 110, 0.3);
  transition: transform 0.15s, box-shadow 0.15s;
}
.submit-btn:hover { transform: translateY(-1px); box-shadow: 0 9px 22px rgba(15, 118, 110, 0.4); }

.switch-hint { text-align: center; font-size: 13px; color: #5c8480; margin: 16px 0; }
.switch-hint a { color: var(--teal-700); font-weight: 600; cursor: pointer; }
.switch-hint a:hover { text-decoration: underline; }

.login-disclaimer { border-radius: 10px; }
.login-disclaimer :deep(.el-alert__title) { font-size: 12px; line-height: 1.6; }
</style>
