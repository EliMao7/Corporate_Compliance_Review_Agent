<!--
  ============================================================
  DisclaimerBanner.vue —— 全局免责声明横幅
  从 ChatView.vue 抽出的独立组件（与讲义 day05 §3.2 目录结构一致）。
  合规底线：任何页面只要涉及审查/建议，就必须展示本横幅，且不可关闭。
  ============================================================
-->
<script setup>
defineProps({
  // 允许调用方覆盖文案，默认使用标准合规免责声明
  text: {
    type: String,
    default: '本系统只提供法规依据检索与风险提示，不提供法律意见，也不对文件是否合法作出结论。以上内容请由法务或执业律师复核后使用。重大事项请咨询专业律师或主管部门。'
  }
})
</script>

<template>
  <el-alert
    class="disclaimer"
    type="warning"
    :closable="false"
    show-icon
    :title="text"
  />
</template>

<style scoped>
.disclaimer { border-radius: 12px; }
</style>
