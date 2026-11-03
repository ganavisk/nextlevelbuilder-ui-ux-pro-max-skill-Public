import type { Metadata } from "next";
import { apiFetch, query } from "@/lib/api";
import { formatDateTime, formatPhone, humanize } from "@/lib/format";
import { readSession } from "@/lib/session";
import type { Appointment, Page } from "@/lib/types";
import { Badge, EmptyState, PageHeader, Pagination, SegmentedControl, Table, td, th, tr } from "@/components/ui";
import { CancelButton } from "./cancel-button";

export const metadata: Metadata = { title: "Appointments" };

const LIMIT = 25;
const STATUSES = ["confirmed", "cancelled"];

function DetailsCell({ details }: { details: Record<string, string> }) {
  const entries = Object.entries(details ?? {});
  if (entries.length === 0) return <span className="text-label-3">—</span>;
  return (
    <dl className="flex flex-col gap-0.5 text-[13px]">
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-1.5">
          <dt className="text-label-2">{humanize(k)}</dt>
          <dd className="truncate">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

export default async function AppointmentsPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; offset?: string }>;
}) {
  const sp = await searchParams;
  const offset = Math.max(0, Number(sp.offset) || 0);
  const status = STATUSES.includes(sp.status ?? "") ? sp.status : undefined;
  const [data, session] = await Promise.all([
    apiFetch<Page<Appointment>>(`/appointments${query({ status, limit: LIMIT, offset })}`),
    readSession(),
  ]);
  const canCancel = session?.role !== "viewer";
  const multiBay = data.items.some((a) => a.bay > 1);
  const hasDetails = data.items.some((a) => Object.keys(a.details ?? {}).length > 0);

  return (
    <>
      <PageHeader title="Appointments" description="Bookings the receptionist made on calls. Test Lab bookings are marked test." />
      <SegmentedControl
        label="Filter by status"
        items={[undefined, ...STATUSES].map((s) => ({
          href: s ? `/appointments?status=${s}` : "/appointments",
          label: s ?? "All",
          active: s === status,
        }))}
      />

      {data.items.length === 0 ? (
        <div className="rounded-[14px] bg-surface">
          <EmptyState
            title="No appointments"
            hint="When a caller books through the receptionist, the appointment shows up here and on the connected calendar. Try a booking in the Test Lab."
          />
        </div>
      ) : (
        <Table minWidth="44rem">
          <thead>
            <tr>
              <th className={th}>Customer</th>
              <th className={th}>Service</th>
              {hasDetails && <th className={th}>Details</th>}
              <th className={th}>Starts</th>
              {multiBay && <th className={th}>Bay</th>}
              <th className={th}>Status</th>
              {canCancel && (
                <th className={th}>
                  <span className="sr-only">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {data.items.map((a) => (
              <tr key={a.id} className={tr}>
                <td className={td}>
                  <p className="font-medium">{a.customer_name || "—"}</p>
                  <p className="num text-[13px] text-label-2">{formatPhone(a.customer_phone)}</p>
                </td>
                <td className={td}>{a.service}</td>
                {hasDetails && (
                  <td className={`${td} max-w-[18rem]`}>
                    <DetailsCell details={a.details} />
                  </td>
                )}
                <td className={`${td} whitespace-nowrap`}>
                  <span className="num">{formatDateTime(a.starts_at, a.timezone)}</span>
                  <span className="ml-1.5 text-[12px] text-label-2">
                    {a.timezone.split("/").pop()?.replace(/_/g, " ")}
                  </span>
                </td>
                {multiBay && <td className={`${td} num`}>{a.bay}</td>}
                <td className={td}>
                  <span className="flex flex-wrap items-center gap-1.5">
                    <Badge value={a.status} />
                    {a.simulated && <Badge value="simulated" />}
                  </span>
                </td>
                {canCancel && (
                  <td className={`${td} text-right`}>
                    {a.status === "confirmed" && <CancelButton id={a.id} name={a.customer_name} />}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <Pagination basePath="/appointments" params={{ status }} offset={offset} limit={LIMIT} total={data.total} />
    </>
  );
}
