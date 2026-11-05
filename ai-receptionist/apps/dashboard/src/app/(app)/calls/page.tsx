import Link from "next/link";
import type { Metadata } from "next";
import { apiFetch, query } from "@/lib/api";
import { formatDateTime, formatDuration, formatPhone, humanize } from "@/lib/format";
import type { CallSummary, Page } from "@/lib/types";
import { Badge, EmptyState, PageHeader, Pagination, SegmentedControl, Table, td, th, tr } from "@/components/ui";

export const metadata: Metadata = { title: "Calls" };

const LIMIT = 25;
const OUTCOMES = [
  "appointment_booked",
  "lead_qualified",
  "callback_requested",
  "answered_faq",
  "voicemail",
  "abandoned",
  "other",
];

export default async function CallsPage({
  searchParams,
}: {
  searchParams: Promise<{ outcome?: string; offset?: string }>;
}) {
  const sp = await searchParams;
  const offset = Math.max(0, Number(sp.offset) || 0);
  const outcome = OUTCOMES.includes(sp.outcome ?? "") ? sp.outcome : undefined;
  const data = await apiFetch<Page<CallSummary>>(`/calls${query({ outcome, limit: LIMIT, offset })}`);

  return (
    <>
      <PageHeader title="Calls" description="Every call, newest first. Rehearsals from the Test Lab are marked test." />
      <SegmentedControl
        label="Filter by outcome"
        items={[undefined, ...OUTCOMES].map((o) => ({
          href: o ? `/calls?outcome=${o}` : "/calls",
          label: o ? humanize(o) : "All",
          active: o === outcome,
        }))}
      />

      {data.items.length === 0 ? (
        <div className="rounded-[14px] bg-surface">
          <EmptyState
            title={outcome ? "No calls with this outcome" : "No calls yet"}
            hint={
              outcome
                ? "Try a different filter, or show all calls."
                : "Calls appear here after they end, with a transcript and a one-line summary."
            }
          />
        </div>
      ) : (
        <Table>
          <thead>
            <tr>
              <th className={th}>Caller</th>
              <th className={th}>When</th>
              <th className={th}>Length</th>
              <th className={th}>Outcome</th>
              <th className={th}>Sentiment</th>
              <th className={th}>Summary</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((c) => (
              <tr key={c.id} className={tr}>
                <td className={`${td} whitespace-nowrap`}>
                  <Link href={`/calls/${c.id}`} className="num font-medium text-tint-text hover:underline">
                    {formatPhone(c.caller_e164)}
                  </Link>
                  {c.direction === "simulated" && (
                    <span className="ml-2 align-middle">
                      <Badge value="simulated" />
                    </span>
                  )}
                </td>
                <td className={`${td} whitespace-nowrap text-label-2`}>{formatDateTime(c.started_at)}</td>
                <td className={`${td} num text-label-2`}>{formatDuration(c.started_at, c.ended_at)}</td>
                <td className={td}>
                  <Badge value={c.outcome} />
                </td>
                <td className={td}>
                  <Badge value={c.sentiment} />
                </td>
                <td className={`${td} max-w-[30ch] truncate text-label-2`} title={c.summary ?? ""}>
                  {c.summary ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <Pagination basePath="/calls" params={{ outcome }} offset={offset} limit={LIMIT} total={data.total} />
    </>
  );
}
