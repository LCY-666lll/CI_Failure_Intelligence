<script setup>
import { computed, onMounted, onUnmounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { getHistory } from "../api.js";
import { evidenceLocation, historyStatus } from "../evidence.js";

const props = defineProps({ eventId: { type: Number, required: true } });
const result = ref(null);
const pending = ref(true);
const error = ref("");
let active = true;
const status = computed(() =>
  result.value ? historyStatus(result.value) : null,
);
async function load() {
  if (pending.value && result.value) return;
  pending.value = true;
  error.value = "";
  try {
    const response = await getHistory(props.eventId);
    if (active) result.value = response;
  } catch (cause) {
    if (active)
      error.value =
        cause.status === 404 ? "这条事件的历史信息不可用。" : cause.message;
  } finally {
    if (active) pending.value = false;
  }
}
const source = (item) =>
  evidenceLocation(
    `history:${item.event_id}:${item.failure.testcase_index}:${item.failure.issue_index}`,
    props.eventId,
  );
const date = (value) =>
  new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
onMounted(load);
onUnmounted(() => {
  active = false;
});
</script>

<template>
  <section
    class="section-block history-section"
    aria-labelledby="history-title"
  >
    <div class="section-heading">
      <h2 id="history-title">相似历史</h2>
      <span
        v-if="status && !pending && !error"
        :class="{ 'history-warning-text': status.warning }"
        >{{ status.label }}</span
      >
    </div>
    <p class="section-intro">同仓库已接收的历史失败，最多 5 条。</p>
    <p v-if="pending" class="empty-note" role="status">正在检索历史失败…</p>
    <div v-else-if="error" class="history-notice history-warning" role="alert">
      <p>{{ error }}</p>
      <button class="text-button" type="button" @click="load">重新检索</button>
    </div>
    <template v-else-if="result">
      <p
        v-if="result.current_failures_truncated"
        class="history-notice history-warning"
      >
        本次仅选取部分当前失败参与检索，结果未覆盖全部报告。
      </p>
      <p
        v-if="status.message"
        class="history-notice"
        :class="{ 'history-warning': status.warning }"
      >
        {{ status.message }}
      </p>
      <div
        v-if="result.retrieval_status === 'available' && result.history.length"
        class="history-list"
      >
        <article
          v-for="item in result.history"
          :key="`${item.event_id}:${item.failure.testcase_index}:${item.failure.issue_index}`"
          class="history-row"
        >
          <div class="history-row-heading">
            <RouterLink
              v-if="source(item)"
              :to="source(item).to"
              class="history-source"
              >来源事件 #{{ item.event_id }}
              <span aria-hidden="true">↗</span></RouterLink
            ><span v-else>来源暂不可用</span
            ><span class="similarity" title="检索排序分数，不代表根因概率"
              >相似分数
              <strong class="mono">{{
                item.similarity_score.toFixed(3)
              }}</strong></span
            >
          </div>
          <h3>{{ item.failure.test_name }}</h3>
          <p class="history-context">
            {{ item.repository_full_name }} · 运行 #{{ item.run_id }} /
            {{ item.run_attempt }}
          </p>
          <p class="history-context">
            {{ item.job_key }} · {{ date(item.received_at) }}
          </p>
          <p v-if="item.failure.message" class="failure-message">
            {{ item.failure.message }}
          </p>
          <details>
            <summary>查看历史错误详情</summary>
            <pre>{{ item.failure.details }}</pre>
          </details>
        </article>
        <p class="history-footnote">
          相似分数用于检索排序，不代表根因概率。查看来源以核对上下文。
        </p>
      </div>
    </template>
  </section>
</template>
