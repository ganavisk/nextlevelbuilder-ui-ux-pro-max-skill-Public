import type { Metadata } from "next";
import { apiFetch, query } from "@/lib/api";
import { formatPhone } from "@/lib/format";
import { readSession } from "@/lib/session";
import type { CallbackRequest, Page } from "@/lib/types";
import { Badge, EmptyState, PageHeader, Pagination, SegmentedControl, Table, td, th, tr } from "@/components/ui";
import { StatusButtons } from "./status-buttons";

export const metadata: Metadata = { title: "Callbacks" };

const LIMIT = 25;
const STATUSES = ["open", "contacted", "resolved"];

function Reason({ text }: { text: string }) {
  // The auto shop's safety rule files do-not-drive calls as "URGENT ...".
  const urgent = /^urgent\b/i.test(text.trim());
  const rest = urgent ? text.trim().replace(/^urgent\b[:\s-]*/i, "") : text;
  return (
    <span className="flex items-start gap-2">
      {urgent && <Badge value="urgent" tone="critical" />}
      <span>{rest}</span>
    </span>
  );
}

export default async function CallbacksPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; offset?: string }>;
}) {
  const sp = await searchParams;
  const offset = Math.max(0, Number(sp.offset) || 0);
  const status = STATUSES.includes(sp.status ?? "") ? sp.status : undefined;
  const [data, session] = await Promise.all([
    apiFetch<Page<CallbackRequest>>(`/callback-requests${query({ status, limit: LIMIT, offset })}`),
    readSession(),
  ]);
  const canEdit = session?.role !== "viewer";

  return (
    <>
      <PageHeader
        title="Callbacks"
        description="Callers who were promised a call back. Open requests come first."
      />
      <SegmentedControl
        label="Filter by status"
        items={[undefined, ...STATUSES].map((s) => ({
          href: s ? `/callbacks?status=${s}` : "/callbacks",
          label: s ?? "All",
          active: s === status,
        }))}
      />

      {data.items.length === 0 ? (
        <div className="rounded-[14px] bg-surface">
          <EmptyState
            title={status ? `No ${status} callbacks` : "Nobody is waiting on a callback"}
            hint="When a caller asks for a person, or the receptionist hits something it shouldn't answer, the request lands here."
          />
        </div>
      ) : (
        <Table>
          <thead>
            <tr>
              <th className={th}>Caller</th>
              <th className={th}>Reason</th>
              <th className={th}>Best time</th>
              <th className={th}>Status</th>
              {canEdit && (
                <th className={th}>
                  <span className="sr-only">Actions</span>
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {data.items.map((r) => (
              <tr key={r.id} className={tr}>
                <td className={`${td} num whitespace-nowrap`}>
                  <span className="font-medium">{formatPhone(r.callback_e164 ?? r.caller_e164)}</span>
                  {r.callback_e164 && (
                    <span className="block text-[13px] text-label-2">
                      Called from {r.caller_e164 ? formatPhone(r.caller_e164) : "a blocked number"}
                    </span>
                  )}
                </td>
                <td className={`${td} max-w-[42ch]`}>
                  <Reason text={r.reason} />
                </td>
                <td className={`${td} text-label-2`}>{r.preferred_window ?? "Any time"}</td>
                <td className={td}>
                  <Badge value={r.status} />
                </td>
                {canEdit && (
                  <td className={`${td} text-right`}>
                    <StatusButtons id={r.id} status={r.status} />
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
      )}
      <Pagination basePath="/callbacks" params={{ status }} offset={offset} limit={LIMIT} total={data.total} />
    </>
  );
}
