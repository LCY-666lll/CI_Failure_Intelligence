import { clearReadToken, readToken } from "./auth.js";

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(path, token, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      signal: AbortSignal.timeout(options.method === "POST" ? 120000 : 15000),
      ...options,
      headers: { Authorization: `Bearer ${token}` },
    });
  } catch {
    throw new ApiError(0, "无法连接服务，请检查网络或稍后重试。");
  }

  if (!response.ok) {
    if (response.status === 401 && token === readToken.value) clearReadToken();
    const messages = {
      401: "访问令牌无效或已失效。",
      404: "这条运行记录不存在。",
      409: "当前状态不允许执行此操作，请刷新后查看。",
      422: "请求参数无效。",
      502: "诊断服务返回了无效结果，请稍后重试。",
      503: "服务暂时不可用，请稍后重试。",
    };
    throw new ApiError(
      response.status,
      messages[response.status] || "请求未能完成。",
    );
  }

  try {
    return await response.json();
  } catch {
    throw new ApiError(response.status, "服务返回的数据无法读取。");
  }
}

export const listRuns = (token = readToken.value) =>
  request("/api/v1/ci-runs?limit=20", token);
export const getFailures = (id) =>
  request(`/api/v1/ci-runs/${id}/failures`, readToken.value);
export const getDiagnosis = (id) =>
  request(`/api/v1/ci-runs/${id}/diagnosis`, readToken.value);
export const triggerDiagnosis = (id, token) =>
  request(`/api/v1/ci-runs/${id}/diagnosis`, token, { method: "POST" });
