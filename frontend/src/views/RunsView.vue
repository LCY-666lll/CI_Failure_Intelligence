<script setup>
import { onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { listRuns } from "../api";

const runs = ref([]);
const loading = ref(true);
const error = ref("");

async function load() {
  loading.value = true;
  error.value = "";
  try {
    const result = await listRuns();
    runs.value = result.items;
  } catch (cause) {
    error.value = cause.message;
  } finally {
    loading.value = false;
  }
}

function statusLabel(run) {
  if (run.parse_status === "pending") return "待解析";
  if (run.parse_status === "failed") return "解析失败";
  return run.failure_count > 0 ? "发现失败" : "已解析";
}

function formatDate(value) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

onMounted(load);
</script>

<template>
  <div class="page-heading">
    <div>
      <div class="eyebrow">运行记录</div>
      <h1>最近的 CI 事件</h1>
      <p>查看已接入仓库的最近 20 条失败报告。</p>
    </div>
    <button
      class="secondary-button"
      type="button"
      :disabled="loading"
      @click="load"
    >
      刷新列表
    </button>
  </div>
  <div class="table-toolbar">
    <span>最近接收</span><span>按接收时间排序</span>
  </div>
  <div v-if="loading" class="state-panel" role="status">正在读取运行记录…</div>
  <div v-else-if="error" class="state-panel">
    <p class="inline-error" role="alert">{{ error }}</p>
    <button class="secondary-button" type="button" @click="load">重试</button>
  </div>
  <div v-else-if="runs.length === 0" class="state-panel">
    <h2>暂无事件</h2>
    <p>接入的 CI 失败运行会出现在这里。</p>
  </div>
  <div v-else class="table-scroll">
    <table class="runs-table">
      <thead>
        <tr>
          <th>仓库 / 工作流</th>
          <th>运行</th>
          <th>接收时间</th>
          <th>解析状态</th>
          <th class="numeric">失败数</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="run in runs" :key="run.event_id">
          <td>
            <RouterLink class="run-title" :to="`/runs/${run.event_id}`">{{
              run.repository_full_name
            }}</RouterLink
            ><span class="subline"
              >{{ run.workflow_name }} · {{ run.job_name }}</span
            >
          </td>
          <td>
            <span class="mono">#{{ run.run_id }} / {{ run.run_attempt }}</span
            ><span class="subline mono"
              >{{ run.head_branch }} · {{ run.commit_sha.slice(0, 7) }}</span
            >
          </td>
          <td class="quiet">{{ formatDate(run.received_at) }}</td>
          <td>
            <span
              class="status-label"
              :class="
                run.parse_status === 'failed'
                  ? 'status-error'
                  : run.parse_status === 'pending'
                    ? 'status-pending'
                    : run.failure_count
                      ? 'status-failure'
                      : 'status-ok'
              "
              >{{ statusLabel(run) }}</span
            >
          </td>
          <td class="numeric mono">{{ run.failure_count }}</td>
        </tr>
      </tbody>
    </table>
  </div>
  <p v-if="!loading && runs.length" class="table-footnote">
    仅显示最近 20 条已接收事件；当前版本不提供完整历史分页。
  </p>
</template>
