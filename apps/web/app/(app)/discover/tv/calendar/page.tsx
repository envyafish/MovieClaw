import type { Metadata } from "next";

import { TraktCalendarView } from "@/components/trakt-calendar-view";

export const metadata: Metadata = { title: "追剧日历" };

/** 追剧日历完整页：从发现页「查看完整日历」进入。 */
export default function TraktCalendarPage() {
  return (
    <div className="flex h-full flex-col">
      <TraktCalendarView />
    </div>
  );
}
