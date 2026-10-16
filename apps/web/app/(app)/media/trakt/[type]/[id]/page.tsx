import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { MediaDetailView } from "@/components/media-detail-view";

/** 兜底标题；片名要等接口返回，就绪后由视图内的 usePageTitle 覆盖为「{片名}」。 */
export const metadata: Metadata = { title: "影片详情" };

/** Trakt 详情（/media/trakt/movie|tv/[id]）：视觉与 TMDB / 豆瓣详情页复用。 */
export default async function TraktMediaDetailPage({
  params,
}: {
  params: Promise<{ type: string; id: string }>;
}) {
  const { type, id } = await params;
  if ((type !== "movie" && type !== "tv") || !/^\d+$/.test(id)) notFound();
  return <MediaDetailView key={`trakt:${type}:${id}`} type={type} id={id} source="trakt" />;
}
