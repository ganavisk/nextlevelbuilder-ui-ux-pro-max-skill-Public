import type { Metadata } from "next";
import { apiFetch, query } from "@/lib/api";
import { formatPercent, humanize } from "@/lib/format";
import type { AnalyticsSummary } from "@/lib/types";
import { EmptyState, PageHeader, SegmentedControl } from "@/components/ui";

export const metadata: Metadata = { title: "Overview" };

const WINDOWS = [7, 30, 90];

function compact(n: number): string {
  return n >= 10_000 ? `${(n / 1000).toFixed(1)}K` : n.toLocaleString("en-US");
}

function StatTile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="tile flex flex-col justify-between gap-3 p-5">
      <p className="text-[13px] font-medium text-label-2">{label}</p>
      <div>
        <p className="num text-[32px] font-semibold leading-none tracking-[-0.02em]">{value}</p>
        {note && <p className="mt-1.5 text-[13px] text-label-2">{note}</p>}
      </div>
    </div>
  );
}

/** Single-series column chart (dataviz: one hue, <=24px columns with a 4px
 * rounded top on a square baseline, a gap between columns, the peak labelled,
 * a hover + keyboard-focus tooltip per column, and a table view). */
function VolumeChart({ data }: { data: AnalyticsSummary["daily_call_volume"] }) {
  const peak = Math.max(1, ...data.map((d) => d.count));
  const peakIndex = data.findIndex((d) => d.count === peak);
  const day = (iso: string) =>
    new Date(`${iso}T12:00:00Z`).toLocaleDateString("en-US", { month: "short", day: "numeric" });
  return (
    <div className="tile flex flex-col gap-4 p-5 md:col-span-2 md:row-span-2">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-[17px] font-semibold">Calls per day</h2>
        <p className="num text-[13px] text-label-2">Peak {peak}</p>
      </div>
      <div className="relative flex-1">
        {/* Recessive hairline grid: the peak and the baseline. */}
        <div aria-hidden className="absolute inset-x-0 top-0 border-t border-separator" />
        <div
          role="list"
          aria-label="Calls per day"
          className="relative grid h-48 items-end border-b border-separator md:h-full md:min-h-56"
          style={{ gridTemplateColumns: `repeat(${data.length}, minmax(0, 1fr))` }}
        >
          {data.map((d, i) => (
            <div
              key={d.date}
              role="listitem"
              tabIndex={0}
              aria-label={`${day(d.date)}: ${d.count} call${d.count === 1 ? "" : "s"}`}
              className="group relative flex h-full items-end justify-center rounded-sm outline-none"
            >
              <div
                className="w-[72%] max-w-6 rounded-t-[4px] bg-chart transition-opacity duration-150 group-hover:opacity-80"
                style={{ height: d.count === 0 ? "2px" : `${(d.count / peak) * 100}%` }}
              />
              {i === peakIndex && (
                <span className="num absolute -top-5 text-[11px] font-medium text-label-2">{d.count}</span>
              )}
              <span
                role="tooltip"
                className="pointer-events-none absolute bottom-full z-10 mb-2 hidden whitespace-nowrap rounded-[8px] bg-label px-2.5 py-1.5 text-[12px] text-surface shadow-lg group-hover:block group-focus-visible:block"
              >
                <span className="num font-semibold">{d.count}</span> calls · {day(d.date)}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div className="num flex justify-between text-[12px] text-label-2">
        <span>{data[0] && day(data[0].date)}</span>
        <span>{data.at(-1) && day(data.at(-1)!.date)}</span>
      </div>
      <details className="text-[13px]">
        <summary className="text-tint-text">Show as table</summary>
        <table className="mt-2 w-full">
          <thead>
            <tr className="text-left text-label-2">
              <th className="py-1 font-medium">Day</th>
              <th className="py-1 text-right font-medium">Calls</th>
            </tr>
          </thead>
          <tbody className="num">
            {data.map((d) => (
              <tr key={d.date} className="border-t border-separator">
                <td className="py-1">{day(d.date)}</td>
                <td className="py-1 text-right">{d.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

function Breakdown({ title, data }: { title: string; data: Record<string, number> }) {
  const entries = Object.entries(data).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...entries.map(([, n]) => n));
  return (
    <section className="tile p-5 md:col-span-2">
      <h2 className="mb-3 text-[17px] font-semibold">{title}</h2>
      {entries.length === 0 ? (
        <p className="text-[14px] text-label-2">Nothing classified yet.</p>
      ) : (
        <ul className="flex flex-col gap-2.5">
          {entries.map(([key, n]) => (
            <li key={key} className="grid grid-cols-[9.5rem_1fr_2.5rem] items-center gap-3 text-[14px]">
              <span className="truncate">{humanize(key)}</span>
              <span className="h-2 rounded-full bg-fill">
                <span
                  className="block h-full rounded-full bg-chart"
                  style={{ width: `${Math.max(2, (n / max) * 100)}%` }}
                />
              </span>
              <span className="num text-right text-label-2">{n}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default async function OverviewPage({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const { days: daysParam } = await searchParams;
  const days = WINDOWS.includes(Number(daysParam)) ? Number(daysParam) : 30;
  const s = await apiFetch<AnalyticsSummary>(`/analytics/summary${query({ days })}`);

  return (
    <>
      <PageHeader
        title="Overview"
        description={`Customer calls over the last ${s.window_days} days. Test Lab calls are not counted.`}
        actions={
          <SegmentedControl
            label="Time window"
            items={WINDOWS.map((w) => ({ href: `/?days=${w}`, label: `${w} days`, active: w === days }))}
          />
        }
      />

      {s.total_calls === 0 ? (
        <div className="tile">
          <EmptyState
            title="No calls in this window"
            hint="Once your number is connected to the voice system, calls appear here within seconds of hanging up. Rehearse in the Test Lab meanwhile."
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-4 md:grid-rows-[auto_auto_auto]">
          <VolumeChart data={s.daily_call_volume} />
          <StatTile label="Total calls" value={compact(s.total_calls)} />
          <StatTile label="Booking rate" value={formatPercent(s.booking_rate)} note="of classified calls" />
          <StatTile label="Escalation rate" value={formatPercent(s.escalation_rate)} note="callbacks requested" />
          <StatTile label="Classified" value={compact(s.classified_calls)} note={`of ${compact(s.total_calls)} calls`} />
          <Breakdown title="Outcomes" data={s.calls_by_outcome} />
          <Breakdown title="Caller sentiment" data={s.calls_by_sentiment} />
        </div>
      )}
    </>
  );
}
