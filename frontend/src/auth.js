import { ref } from "vue";

const READ_TOKEN_KEY = "cfi.readToken";

function restoreReadToken() {
  try {
    return globalThis.sessionStorage?.getItem(READ_TOKEN_KEY) || "";
  } catch {
    // 浏览器禁止存储时仍允许当前页面使用令牌。
    return "";
  }
}

// 读取凭据按标签页保存；刷新恢复，断开或401时移除。
export const readToken = ref(restoreReadToken());

export function setReadToken(token) {
  readToken.value = token.trim();
  try {
    if (readToken.value) globalThis.sessionStorage?.setItem(READ_TOKEN_KEY, readToken.value);
    else globalThis.sessionStorage?.removeItem(READ_TOKEN_KEY);
  } catch {
    // 存储不可用时回退到内存，不阻断接口访问。
  }
}

export function clearReadToken() {
  readToken.value = "";
  try {
    globalThis.sessionStorage?.removeItem(READ_TOKEN_KEY);
  } catch {
    // 内存中的访问状态已经清除。
  }
}
