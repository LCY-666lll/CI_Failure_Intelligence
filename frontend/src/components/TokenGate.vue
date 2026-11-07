<script setup>
import { ref } from "vue";
import { listRuns } from "../api";
import { setReadToken } from "../auth";

const enteredToken = ref("");
const pending = ref(false);
const error = ref("");

async function connect() {
  const token = enteredToken.value.trim();
  if (!token || pending.value) return;
  pending.value = true;
  error.value = "";
  try {
    await listRuns(token);
    setReadToken(token);
    enteredToken.value = "";
  } catch (cause) {
    error.value = cause.message;
  } finally {
    pending.value = false;
  }
}
</script>

<template>
  <div class="gate-wrap">
    <div class="gate-panel">
      <div class="eyebrow">受邀访问</div>
      <h1>进入 CI 故障工作台</h1>
      <p>输入项目所有者提供的读取令牌，查看已接入仓库的失败运行与诊断记录。</p>
      <form @submit.prevent="connect">
        <label for="read-token">读取令牌</label>
        <input
          id="read-token"
          v-model="enteredToken"
          type="password"
          autocomplete="off"
          spellcheck="false"
          placeholder="粘贴读取令牌"
        />
        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <button
          class="primary-button"
          type="submit"
          :disabled="pending || !enteredToken.trim()"
        >
          {{ pending ? "正在验证…" : "进入工作台" }}
        </button>
      </form>
      <p class="gate-note">
        读取令牌保留在当前标签页，刷新可继续使用；点击“断开”可清除。
      </p>
    </div>
  </div>
</template>
