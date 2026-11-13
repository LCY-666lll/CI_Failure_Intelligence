// 只把契约内的数字身份转换为内部路由，模型文本不能成为任意URL。
export function evidenceLocation(value, currentEventId) {
  if (typeof value !== "string") return null;
  const match = /^(failure):(\d+):(\d+)$|^(history):(\d+):(\d+):(\d+)$/.exec(
    value,
  );
  if (!match) return null;
  const historical = Boolean(match[4]);
  const eventId = historical ? Number(match[5]) : Number(currentEventId);
  const testcase = Number(historical ? match[6] : match[2]);
  const issue = Number(historical ? match[7] : match[3]);
  if (
    !Number.isSafeInteger(eventId) ||
    eventId <= 0 ||
    !Number.isSafeInteger(testcase) ||
    !Number.isSafeInteger(issue)
  )
    return null;
  return {
    to: { path: `/runs/${eventId}`, hash: `#failure:${testcase}:${issue}` },
    label: historical
      ? `历史事件 #${eventId} · 证据 ${testcase}:${issue}`
      : `当前证据 ${testcase}:${issue}`,
  };
}

export function historyStatus(result) {
  if (result.retrieval_status === "not_ready")
    return {
      label: "尚未就绪",
      message:
        result.parse_status === "failed"
          ? "当前报告解析失败，暂时无法检索历史。"
          : "报告尚在等待解析。后台处理完成后，可刷新详情查看历史。",
    };
  if (result.retrieval_status === "degraded")
    return {
      label: "检索暂不可用",
      message:
        "历史检索服务暂时不可用。当前失败与已有诊断仍可查看，请稍后重试。",
      warning: true,
    };
  if (result.retrieval_status === "disabled")
    return { label: "未启用", message: "当前环境未启用历史检索。" };
  if (result.retrieval_status === "available")
    return {
      label: "检索完成",
      message: result.history.length
        ? ""
        : "检索已完成，未找到匹配的历史失败。",
    };
  return {
    label: "状态未知",
    message: "暂时无法确认历史检索状态，请稍后刷新。",
    warning: true,
  };
}
