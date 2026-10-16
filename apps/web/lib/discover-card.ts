/** 发现卡片上 Trakt 才有的字段。不依赖 @/，node --test 可以直接加载。 */

export interface DiscoverCardFields {
  note?: string;
  navigable: boolean;
  posterUrl: string;
}

export function discoverCardFields(input: {
  note?: string;
  navigable?: boolean;
  poster_url?: string;
}): DiscoverCardFields {
  return {
    note: input.note || undefined,
    navigable: input.navigable !== false,
    posterUrl: input.poster_url ?? "",
  };
}

export function showCollectionNotice(
  notice: { code: string; message: string } | null | undefined,
  itemCount: number,
): boolean {
  return Boolean(notice) && itemCount === 0;
}
