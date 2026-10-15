<script setup>
// 根组件：按登录态在 登录页 / 聊天主界面 之间切换（§6.4）
import { ref, onMounted, onUnmounted } from 'vue'
import ChatView from '@/views/ChatView.vue'
import LoginView from '@/views/LoginView.vue'
import { getToken, getUserId, onAuthChange } from '@/api/index.js'

const token = ref(getToken())
const userId = ref(getUserId())

let unsubscribe = null

onMounted(() => {
  // 登录 / 退出 / 401 失效都会触发：重新读取状态驱动视图切换
  unsubscribe = onAuthChange(() => {
    const nextUserId = getUserId()
    // 账号变化 → 清掉本地会话 ID，确保新账号从全新对话开始，不继承上一账号的会话
    if (nextUserId !== userId.value) {
      localStorage.removeItem('ccr_session_id')
    }
    token.value = getToken()
    userId.value = nextUserId
  })
})

onUnmounted(() => {
  if (unsubscribe) unsubscribe()
})
</script>

<template>
  <!-- 无令牌 → 登录页；有令牌 → 聊天主界面（按用户 ID 作 key，账号切换时重挂载） -->
  <LoginView v-if="!token" />
  <ChatView v-else :key="userId || 'default'" />
</template>

<style>
/* ============ 全局基础样式 ============ */
:root {
  /* 配色：沉稳青绿 + 暖色点缀（合规审查的严谨感与可读性） */
  --ink: #123332;
  --teal-900: #0b3d3a;
  --teal-700: #0e5a54;
  --teal-600: #0f766e;
  --teal-500: #14b8a6;
  --mint: #d9f2ec;
  --paper: #f6fbf9;
  --coral: #e8896b;

  /* 字体 */
  --font-display: 'Noto Serif SC', 'Songti SC', 'SimSun', serif;
  --font-body: 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

* {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html,
body,
#app {
  height: 100%;
}

body {
  font-family: var(--font-body);
  color: var(--ink);
  background: var(--paper);
  -webkit-font-smoothing: antialiased;
}
</style>
