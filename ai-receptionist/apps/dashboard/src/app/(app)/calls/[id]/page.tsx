import Link from "next/link";
import { notFound } from "next/navigation";
import { CaretLeft } from "@phosphor-icons/react/dist/ssr";
import { ApiError, apiFetch } from "@/lib/api";
import { formatCents, formatDateTime, formatDuration, formatPhone } from "@/lib/format";
import type { CallDetail } from "@/lib/types";
import { Badge, EmptyState, PageHeader, Section } from "@/components/ui";

export const metadata = { title: "Call" };

export default async function CallDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  let call: CallDetail;
  try {
    call = await apiFetch<CallDetail>(`/calls/${encodeURIComponent(id)}`);
  } catch (e) {
    if (e instanceof ApiError && (e.status === 404 || e.status === 422)) notFound();
    throw e;
  }

  return (
    <>
      <Link href="/calls" className="-mb-4 inline-flex w-fit items-center gap-0.5 text-[15px] text-tint-text">
        <CaretLeft size={16} weight="bold" /> Calls
      </Link>
      <PageHeader
        title={formatPhone(call.caller_e164)}
        description={call.summary ?? "No summary was generated for this call."}
        actions={
          <>
            {call.direction === "simulated" && <Badge value="simulated" />}
            <Badge value={call.outcome} />
            <Badge value={call.sentiment} />
          </>
        }
      />

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[2fr_1fr]">
        <Section title="Transcript">
          {call.transcript.length === 0 ? (
            <EmptyState
              title="No transcript"
              hint="The call ended before anything was said, or the transcript hasn't arrived from the voice system yet."
            />
          ) : (
            <ol className="flex flex-col gap-2">
              {call.transcript.map((turn, i) => {
                const caller = turn.role === "user" || turn.role === "caller";
                return (
                  <li key={i} className={`flex flex-col gap-0.5 ${caller ? "items-end" : "items-start"}`}>
                    <span className="px-3 text-[11px] text-label-2">{caller ? "Caller" : "Receptionist"}</span>
                    <p
                      className={`max-w-[75%] rounded-[18px] px-3.5 py-2 text-[15px] leading-5 ${
                        caller ? "rounded-br-[6px] bg-tint text-white" : "rounded-bl-[6px] bg-surface-2 text-label"
                      }`}
                    >
                      {turn.text}
                    </p>
                  </li>
                );
              })}
            </ol>
          )}
        </Section>

        <Section title="Details" flush>
          <dl className="text-[15px]">
            {[
              ["Started", formatDateTime(call.started_at)],
              ["Length", formatDuration(call.started_at, call.ended_at)],
              ["Direction", call.direction],
              ["Voice cost", formatCents(call.vendor_cost_cents)],
              ["AI cost", formatCents(call.llm_cost_cents)],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between gap-4 border-b border-separator px-4 py-3 last:border-0">
                <dt>{k}</dt>
                <dd className="num capitalize text-label-2">{v}</dd>
              </div>
            ))}
            {call.recording_url && (
              <div className="px-4 py-3">
                <a href={call.recording_url} target="_blank" rel="noreferrer" className="text-tint-text hover:underline">
                  Open recording
                </a>
              </div>
            )}
          </dl>
        </Section>
      </div>
    </>
  );
}
