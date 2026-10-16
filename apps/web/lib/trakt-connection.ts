/** 连接卡片上的一行状态。不依赖 @/，node --test 可以直接加载。 */

export interface TraktConnectionSnapshot {
  app_configured: boolean;
  status: string;
  username?: string | null;
  message?: string | null;
}

export function connectionLabel(view: TraktConnectionSnapshot): string {
  if (!view.app_configured) return "管理员尚未配置 Trakt";
  if (view.status === "disconnected" && view.message) return view.message;
  if (view.username) return view.username;
  if (view.status === "connected") return "已连接";
  return "";
}
