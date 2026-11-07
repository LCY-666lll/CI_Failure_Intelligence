<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { RouterLink, useRoute } from "vue-router";
import { getDiagnosis, getFailures, triggerDiagnosis } from "../api";

const route = useRoute();
const id = computed(() => Number(route.params.eventId));
const run = ref(null);
const diagnosisState = ref(null);
const loading = ref(true);
const error = ref("");
const diagnosisError = ref("");
const triggerOpen = ref(false);
const triggerToken = ref("");
const triggering = ref(false);
const triggerFeedback = ref(null);

function showTriggerFeedback(status, message) {
  triggerFeedback.value = {
    status,
    message,
    time: new Date().toLocaleTimeString("zh-CN", { hour12: false }),
  };
}
let timer;
let active = true;
let diagnosisPending = false;
const processingExpired = computed(
  () =>
    diagnosisState.value?.status === "processing" &&
    diagnosisState.value.lease_expires_at &&
    new Date(diagnosisState.value.lease_expires_at).getTime() <= Date.now(),
);
const isProcessing = computed(
  () =>
    diagnosisState.value?.status === "processing" && !processingExpired.value,
);

function stopPolling() {
  if (timer) clearInterval(timer);
  timer = null;
}

async function loadDiagnosis() {
  if (diagnosisPending || !active) return;
  diagnosisPending = true;
  try {
    const result = await getDiagnosis(id.value);
    if (!active) return;
    diagnosisState.value = result;
    diagnosisError.value = "";
    if (triggerFeedback.value?.status === "in_progress") {
      if (result.status === "completed")
        showTriggerFeedback("generated", "诊断任务已完成，结果已更新。");
      else if (result.status === "failed")
        showTriggerFeedback(
          "error",
          "诊断任务未完成。请查看诊断状态后再决定是否重试。",
        );
      else if (processingExpired.value)
        showTriggerFeedback(
          "error",
          "诊断等待已超时。请先刷新状态，再决定是否重新触发。",
        );
    }
    if (!isProcessing.value) stopPolling();
  } catch (cause) {
    if (!active) return;
    diagnosisError.value = cause.message;
    stopPolling();
  } finally {
    diagnosisPending = false;
  }
}

async function load() {
  stopPolling();
  if (!Number.isSafeInteger(id.value) || id.value <= 0) {
    error.value = "事件编号无效。";
    loading.value = false;
    return;
  }
  loading.value = true;
  error.value = "";
  diagnosisState.value = null;
  try {
    const result = await getFailures(id.value);
    if (!active) return;
    run.value = result;
    await loadDiagnosis();
    if (isProcessing.value) startPolling();
  } catch (cause) {
    error.value = cause.message;
  } finally {
    loading.value = false;
  }
}

function startPolling() {
  stopPolling();
  if (!active) return;
  timer = setInterval(loadDiagnosis, 5000);
}

async function trigger() {
  if (!triggerToken.value.trim() || triggering.value) return;
  triggering.value = true;
  diagnosisError.value = "";
  showTriggerFeedback("submitting", "正在提交诊断请求，请等待返回结果…");
  try {
    const result = await triggerDiagnosis(id.value, triggerToken.value.trim());
    if (!active) return;
    triggerOpen.value = false;
    triggerToken.value = "";
    const messages = {
      cached: "已复用已有诊断。本次没有调用 AI 模型，结果保持不变。",
      generated: "新诊断已完成，结果已更新。",
      in_progress:
        "已有诊断任务正在处理中，本次未重复启动。页面会自动查询结果。",
    };
    showTriggerFeedback(
      result.status,
      messages[result.status] || "请求已返回，请刷新诊断状态确认结果。",
    );
    await loadDiagnosis();
    if (diagnosisError.value)
      triggerFeedback.value.message += " 详情刷新失败，请点击刷新详情确认。";
    if (result.status === "in_progress") startPolling();
  } catch (cause) {
    if (!active) return;
    showTriggerFeedback(
      "error",
      cause.status === 0
        ? "暂时无法确认触发结果。请先刷新诊断状态，避免立即重复提交。"
        : `触发未成功：${cause.message}`,
    );
  } finally {
    triggering.value = false;
    triggerToken.value = "";
  }
}

function evidenceId(failure) {
  return `failure:${failure.testcase_index}:${failure.issue_index}`;
}

function formatDate(value) {
  return value
    ? new Intl.DateTimeFormat("zh-CN", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(new Date(value))
    : "—";
}

watch(id, load);
onMounted(load);
onUnmounted(() => {
  active = false;
  stopPolling();
});
</script>

<template>
  <RouterLink class="back-link" to="/runs">← 返回运行列表</RouterLink>
  <div v-if="loading" class="state-panel" role="status">正在读取事件详情…</div>
  <div v-else-if="error" class="state-panel">
    <p class="inline-error" role="alert">{{ error }}</p>
    <button class="secondary-button" type="button" @click="load">重试</button>
  </div>
  <template v-else-if="run">
    <div class="page-heading detail-heading">
      <div>
        <div class="eyebrow">事件 #{{ run.event_id }}</div>
        <h1>{{ run.repository_full_name }}</h1>
        <p>{{ run.workflow_name }} / {{ run.job_name }}</p>
      </div>
      <button
        class="secondary-button"
        type="button"
        :disabled="triggering"
        @click="load"
      >
        刷新详情
      </button>
    </div>
    <div class="meta-strip">
      <span
        >运行
        <strong class="mono"
          >#{{ run.run_id }} / {{ run.run_attempt }}</strong
        ></span
      ><span
        >分支 <strong class="mono">{{ run.head_branch }}</strong></span
      ><span
        >提交
        <strong class="mono">{{ run.commit_sha.slice(0, 12) }}</strong></span
      ><span
        >解析
        <strong>{{
          run.parse_status === "pending"
            ? "待解析"
            : run.parse_status === "failed"
              ? "解析失败"
              : "已完成"
        }}</strong></span
      >
    </div>

    <div class="detail-grid">
      <section class="section-block">
        <div class="section-heading">
          <h2>失败证据</h2>
          <span>{{ run.failures.length }} 条</span>
        </div>
        <div v-if="run.parse_status === 'pending'" class="empty-note">
          报告仍在等待解析，当前没有可展示的失败证据。
        </div>
        <div
          v-else-if="run.parse_status === 'failed'"
          class="empty-note error-note"
        >
          报告解析失败。{{ run.parse_error_message || "请检查报告格式。" }}
        </div>
        <div v-else-if="run.failures.length === 0" class="empty-note">
          解析已完成，没有记录到 failure 或 error。
        </div>
        <article
          v-for="failure in run.failures"
          :id="evidenceId(failure)"
          :key="failure.id"
          class="failure-row"
        >
          <div class="failure-top">
            <span class="issue-type" :class="failure.issue_type">{{
              failure.issue_type
            }}</span
            ><span class="evidence-key mono">{{ evidenceId(failure) }}</span>
          </div>
          <h3>{{ failure.test_name }}</h3>
          <p class="failure-path mono">
            {{
              [failure.suite_name, failure.classname]
                .filter(Boolean)
                .join(" / ")
            }}
          </p>
          <p v-if="failure.message" class="failure-message">
            {{ failure.message }}
          </p>
          <details>
            <summary>查看错误详情</summary>
            <pre>{{ failure.details }}</pre>
          </details>
        </article>
      </section>

      <section class="section-block diagnosis-block">
        <div class="section-heading">
          <h2>AI 诊断</h2>
          <span v-if="diagnosisState">{{
            diagnosisState.status === "completed"
              ? "已完成"
              : diagnosisState.status === "processing"
                ? "处理中"
                : diagnosisState.status === "failed"
                  ? "失败"
                  : "尚未开始"
          }}</span>
        </div>
        <p class="section-intro">模型建议用于辅助排查，请结合原始证据判断。</p>
        <p v-if="diagnosisError" class="inline-error" role="alert">
          {{ diagnosisError }}
        </p>
        <div v-if="processingExpired" class="empty-note">
          诊断等待已超时。请刷新状态；授权者可以再次明确触发。
        </div>
        <div v-else-if="isProcessing" class="empty-note">
          诊断正在处理中，页面会自动查询结果。
        </div>
        <div
          v-else-if="diagnosisState?.status === 'failed'"
          class="empty-note error-note"
        >
          上一次诊断未完成。{{ diagnosisState.error_message || "可稍后重试。" }}
        </div>
        <div
          v-else-if="
            diagnosisState?.status === 'completed' && diagnosisState.diagnosis
          "
          class="diagnosis-content"
        >
          <h3>结论摘要</h3>
          <p>{{ diagnosisState.diagnosis.summary }}</p>
          <h3>可能原因</h3>
          <ol class="cause-list">
            <li
              v-for="cause in diagnosisState.diagnosis.root_causes"
              :key="cause.title"
            >
              <strong>{{ cause.title }}</strong
              ><span class="confidence">{{
                cause.confidence === "high"
                  ? "较高置信度"
                  : cause.confidence === "medium"
                    ? "中等置信度"
                    : "较低置信度"
              }}</span>
              <p>{{ cause.explanation }}</p>
              <div class="evidence-links">
                证据：<a
                  v-for="refId in cause.evidence_ids"
                  :key="refId"
                  :href="`#${refId}`"
                  class="mono"
                  >{{ refId }}</a
                >
              </div>
            </li>
          </ol>
          <h3>建议操作</h3>
          <ol class="action-list">
            <li
              v-for="action in diagnosisState.diagnosis.recommended_actions"
              :key="action"
            >
              {{ action }}
            </li>
          </ol>
          <div v-if="diagnosisState.diagnosis.needs_more_information">
            <h3>仍需补充</h3>
            <ul>
              <li
                v-for="item in diagnosisState.diagnosis.missing_information"
                :key="item"
              >
                {{ item }}
              </li>
            </ul>
          </div>
          <p class="diagnosis-meta">
            完成时间：{{ formatDate(diagnosisState.finished_at) }} · 尝试
            {{ diagnosisState.attempt_count }} 次
          </p>
        </div>
        <div v-else-if="!diagnosisError" class="empty-note">
          这条事件还没有保存的诊断。
        </div>

        <div class="trigger-area">
          <div
            v-if="triggerFeedback"
            class="trigger-feedback"
            :class="`trigger-feedback-${triggerFeedback.status}`"
            role="status"
            aria-live="polite"
            aria-atomic="true"
          >
            <span
              v-if="triggering"
              class="submit-spinner"
              aria-hidden="true"
            ></span>
            <div>
              <p class="feedback-time">
                最近一次操作 · {{ triggerFeedback.time }}
              </p>
              <p>{{ triggerFeedback.message }}</p>
            </div>
          </div>
          <button
            v-if="!triggerOpen"
            class="secondary-button"
            type="button"
            :disabled="
              run.parse_status !== 'parsed' ||
              run.failures.length === 0 ||
              isProcessing
            "
            @click="triggerOpen = true"
          >
            使用授权令牌触发诊断
          </button>
          <form v-else @submit.prevent="trigger">
            <label for="trigger-token">付费诊断触发令牌</label>
            <input
              id="trigger-token"
              v-model="triggerToken"
              type="password"
              autocomplete="off"
              spellcheck="false"
              placeholder="仅授权者输入"
              :disabled="triggering"
            />
            <p class="helper-text">
              这项操作可能调用模型并产生费用；已有有效结果通常会复用。
            </p>
            <div class="button-row">
              <button
                class="primary-button"
                type="submit"
                :disabled="triggering || !triggerToken.trim()"
              >
                {{ triggering ? "正在提交…" : "确认触发" }}</button
              ><button
                class="text-button"
                type="button"
                :disabled="triggering"
                @click="
                  triggerOpen = false;
                  triggerToken = '';
                "
              >
                取消
              </button>
            </div>
          </form>
        </div>
      </section>
    </div>
  </template>
</template>
