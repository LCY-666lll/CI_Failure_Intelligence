<script setup>
import { RouterLink, RouterView } from "vue-router";
import { readToken, clearReadToken } from "./auth";
import TokenGate from "./components/TokenGate.vue";
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <RouterLink
        class="brand"
        to="/runs"
        aria-label="CI Failure Intelligence 首页"
      >
        <span class="brand-mark">CI</span>
        <span
          ><strong>Failure Intelligence</strong
          ><small>CI 故障工作台</small></span
        >
      </RouterLink>
      <div class="nav-caption">工作区</div>
      <RouterLink class="nav-link" to="/runs">运行记录</RouterLink>
      <div class="sidebar-bottom">
        <span class="connection-dot" :class="{ active: readToken }"></span>
        <span>{{ readToken ? "已连接共享工作区" : "等待访问令牌" }}</span>
        <button
          v-if="readToken"
          class="text-button"
          type="button"
          @click="clearReadToken"
        >
          断开
        </button>
      </div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <span>CI Failure Intelligence</span
        ><span class="topbar-note">共享团队工作台</span>
      </header>
      <main class="main-content">
        <RouterView v-if="readToken" :key="$route.path" />
        <TokenGate v-else />
      </main>
    </div>
  </div>
</template>
