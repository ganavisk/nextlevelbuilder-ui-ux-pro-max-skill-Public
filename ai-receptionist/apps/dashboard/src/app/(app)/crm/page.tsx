import Link from "next/link";
import type { Metadata } from "next";
import { apiFetch } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type { CrmSync } from "@/lib/types";
import { Badge, EmptyState, PageHeader, Table, td, th, tr } from "@/components/ui";

export const metadata: Metadata = { title: "CRM sync" };

export default async function CrmPage() {
  const rows = await apiFetch<CrmSync[]>("/crm/failed-syncs?limit=100");

  return (
    <>
      <PageHeader
        title="CRM sync"
        description="Calls that didn't reach your CRM. Failed syncs retry on their own; dead ones gave up and need a look."
      />
      {rows.length === 0 ? (
        <div className="rounded-[14px] bg-surface">
          <EmptyState
            title="Everything is synced"
            hint="Contacts, call notes and deals reach your CRM after each call. Anything that fails shows up here with the reason."
          />
        </div>
      ) : (
        <Table>
          <thead>
            <tr>
              <th className={th}>Provider</th>
              <th className={th}>Status</th>
              <th className={th}>Attempts</th>
              <th className={th}>First failed</th>
              <th className={th}>Error</th>
              <th className={th}>Call</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className={tr}>
                <td className={`${td} capitalize`}>{r.provider}</td>
                <td className={td}>
                  <Badge value={r.status} />
                </td>
                <td className={`${td} num`}>{r.attempts}</td>
                <td className={`${td} whitespace-nowrap text-label-2`}>{formatDateTime(r.created_at)}</td>
                <td className={`${td} max-w-[36ch] break-words font-mono text-[12px] text-label-2`}>
                  {r.last_error ?? "—"}
                </td>
                <td className={td}>
                  {r.call_id ? (
                    <Link href={`/calls/${r.call_id}`} className="text-tint-text hover:underline">
                      View
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </>
  );
}
