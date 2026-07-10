// localStorage 引导状态封装：仅控制同一浏览器是否展示新手引导页面。

const ONBOARDING_COMPLETED_KEY = 'file-agent:onboarding-completed:v1';

export function hasCompletedOnboarding(): boolean {
  // 未在当前浏览器标记完成即视为首次使用。
  return localStorage.getItem(ONBOARDING_COMPLETED_KEY) === 'true';
}

export function markOnboardingCompleted(): void {
  // 用户点击"开始使用"或"我知道了"后写入标记，避免重复进入引导页。
  localStorage.setItem(ONBOARDING_COMPLETED_KEY, 'true');
}

export function clearOnboardingCompletedForDebug(): void {
  // 仅供本地调试使用，不在页面上提供按钮，避免误操作。
  localStorage.removeItem(ONBOARDING_COMPLETED_KEY);
}
