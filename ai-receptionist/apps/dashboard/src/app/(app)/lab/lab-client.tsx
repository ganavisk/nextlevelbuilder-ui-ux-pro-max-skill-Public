"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  ArrowUp,
  CaretLeft,
  CaretRight,
  PhoneCall,
  PhoneDisconnect,
  Trash,
  Wrench,
  Lightning,
} from "@phosphor-icons/react";
import { Badge, btn, Notice } from "@/components/ui";
import type { ActivityItem, Board, LabSession, LabStatus, LabTurn, ToolEvent } from "@/lib/types";

// ── API (through /api/lab, which keeps the token server-side) ───────────────

class LabError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
  ) {
    super(message);
  }
}

async function lab<T>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  const res = await fetch(`/api/lab/${path}`, {
    method: init?.method ?? (init?.body !== undefined ? "POST" : "GET"),
    headers: init?.body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
    cache: "no-store",
  });
  if (res.status === 401) {
    window.location.href = "/api/auth/logout";
    throw new LabError(401, "authentication_required", "Signed out");
  }
  const data = (await res.json().catch(() => ({}))) as { error?: { code?: string; message?: string } };
  if (!res.ok) {
    throw new LabError(res.status, data.error?.code ?? "error", data.error?.message ?? res.statusText);
  }
  return data as T;
}

/** Run `fn` now and every `ms` while the tab is visible. */
function usePolling(fn: () => void, ms: number, deps: unknown[]) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | undefined;
    const tick = () => {
      if (document.visibilityState === "visible") saved.current();
    };
    tick();
    timer = setInterval(tick, ms);
    document.addEventListener("visibilitychange", tick);
    return () => {
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", tick);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps]);
}

// ── time helpers (the board is drawn in the business's timezone) ────────────

function minutesOfDay(iso: string | Date, timeZone: string): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    hour: "numeric",
    minute: "numeric",
    hourCycle: "h23",
    timeZone,
  }).formatToParts(typeof iso === "string" ? new Date(iso) : iso);
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0);
  return get("hour") * 60 + get("minute");
}

function localDate(date: Date, timeZone: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
}

function shiftDay(day: string, by: number): string {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + by);
  return d.toISOString().slice(0, 10);
}

function clock(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", timeZone }).format(new Date(iso));
}

function hhmm(value: string): number {
  const [h, m] = value.split(":").map(Number);
  return (h ?? 0) * 60 + (m ?? 0);
}

function ago(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  return `${Math.floor(s / 3600)}h ago`;
}

const humanize = (s: string) => s.replace(/_/g, " ");

function defaultStart(): string {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  while (d.getDay() === 0) d.setDate(d.getDate() + 1);
  return `${d.toISOString().slice(0, 10)}T10:00`;
}

// ── shared bits ─────────────────────────────────────────────────────────────

function Panel({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex min-w-0 flex-col gap-2">
      <div className="flex min-h-8 items-end justify-between gap-3 px-1">
        <h2 className="text-[13px] font-medium uppercase tracking-wide text-label-2">{title}</h2>
        {actions}
      </div>
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-[14px] bg-surface">{children}</div>
    </section>
  );
}

const fieldInput =
  "w-full rounded-[10px] border border-separator bg-surface-2 px-3 py-2 text-[15px] text-label transition duration-150 placeholder:text-label-3 focus:border-tint focus:outline-none focus:ring-3 focus:ring-tint/25";

function Field({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={htmlFor} className="text-[13px] font-medium text-label-2">
        {label}
      </label>
      {children}
    </div>
  );
}

function Latency({ ms }: { ms: number }) {
  // The in-call budget is ~2s per tool (Phase 1); flag anything slower.
  return (
    <span
      className={`num inline-flex items-center gap-0.5 rounded-full px-1.5 text-[11px] font-medium ${
        ms > 2000 ? "bg-warning-bg text-warning" : "bg-fill text-label-2"
      }`}
    >
      <Lightning size={10} weight="fill" aria-hidden />
      {ms} ms
    </span>
  );
}

function ToolTrace({ event }: { event: ToolEvent }) {
  return (
    <details className="group mx-auto w-full max-w-[34rem] rounded-[10px] bg-surface-2 text-[13px]">
      <summary className="flex list-none items-center gap-2 px-3 py-2 [&::-webkit-details-marker]:hidden">
        <Wrench size={14} className="shrink-0 text-label-2" aria-hidden />
        <span className="font-medium">{humanize(event.tool)}</span>
        <span className="min-w-0 flex-1 truncate text-label-2">{event.result}</span>
        <Latency ms={event.latency_ms} />
        <CaretRight size={12} className="shrink-0 text-label-3 transition group-open:rotate-90" aria-hidden />
      </summary>
      <div className="flex flex-col gap-2 border-t border-separator px-3 py-2.5">
        <div>
          <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-label-2">Arguments</p>
          <pre className="overflow-x-auto whitespace-pre-wrap break-words font-mono text-[12px]">
            {JSON.stringify(event.arguments, null, 2)}
          </pre>
        </div>
        <div>
          <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-label-2">Said to the caller</p>
          <p>{event.result}</p>
        </div>
      </div>
    </details>
  );
}

// ── conversation ────────────────────────────────────────────────────────────

type ChatItem =
  | { kind: "say"; role: "caller" | "assistant"; text: string }
  | { kind: "tool"; event: ToolEvent };

function Conversation({
  status,
  session,
  ended,
  onStart,
  onEnd,
  onDiscard,
  onActivity,
  caller,
  setCaller,
  items,
  setItems,
}: {
  status: LabStatus;
  session: LabSession | null;
  ended: boolean;
  onStart: () => Promise<void>;
  onEnd: () => Promise<void>;
  onDiscard: () => Promise<void>;
  onActivity: () => void;
  caller: string;
  setCaller: (v: string) => void;
  items: ChatItem[];
  setItems: (fn: (prev: ChatItem[]) => ChatItem[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    bottom.current?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "end" });
  }, [items.length, sending]);

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
    } finally {
      setBusy(false);
    }
  };

  const send = async (e: FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || !session || sending) return;
    setDraft("");
    setError(null);
    setSending(true);
    setItems((prev) => [...prev, { kind: "say", role: "caller", text }]);
    try {
      const turn = await lab<LabTurn>(`sessions/${session.session_id}/messages`, { body: { text } });
      setItems((prev) => [
        ...prev,
        ...turn.tool_events.map((event) => ({ kind: "tool" as const, event })),
        { kind: "say", role: "assistant", text: turn.reply },
      ]);
      onActivity();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The receptionist didn't answer.");
    } finally {
      setSending(false);
    }
  };

  const chatDisabled = !session || ended || !status.llm_available;

  return (
    <Panel
      title="Conversation"
      actions={
        session && (
          <div className="flex gap-1.5">
            {!ended && (
              <button type="button" disabled={busy} onClick={() => run(onEnd)} className={`${btn.base} ${btn.gray}`}>
                <PhoneDisconnect size={16} /> End call
              </button>
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                if (confirm("Discard this test call? Its bookings, callbacks and calendar events are removed.")) {
                  void run(onDiscard);
                }
              }}
              className={`${btn.base} ${btn.destructive}`}
            >
              <Trash size={16} /> Discard
            </button>
          </div>
        )
      }
    >
      {!session ? (
        <form
          className="flex flex-1 flex-col items-center justify-center gap-4 px-6 py-12 text-center"
          onSubmit={(e) => {
            e.preventDefault();
            void run(onStart);
          }}
        >
          <span className="grid size-12 place-items-center rounded-full bg-info-bg text-tint-text">
            <PhoneCall size={24} weight="fill" aria-hidden />
          </span>
          <div>
            <p className="text-[17px] font-semibold">Start a test call</p>
            <p className="mt-1 max-w-[40ch] text-[14px] text-label-2">
              You play the caller. The receptionist uses your live prompt, knowledge and tools.
            </p>
          </div>
          <div className="w-full max-w-[16rem] text-left">
            <Field label="Calling from" htmlFor="lab-caller">
              <input
                id="lab-caller"
                type="tel"
                inputMode="tel"
                value={caller}
                onChange={(e) => setCaller(e.target.value)}
                className={`${fieldInput} num`}
              />
            </Field>
          </div>
          <button type="submit" disabled={busy} className={`${btn.base} ${btn.filled} h-10 px-5 text-[15px]`}>
            {busy ? "Connecting…" : "Call the receptionist"}
          </button>
          {error && <Notice tone="bad">{error}</Notice>}
        </form>
      ) : (
        <>
          <div className="flex h-[26rem] flex-col gap-2 overflow-y-auto px-4 py-4" aria-live="polite">
            {items.map((item, i) =>
              item.kind === "tool" ? (
                <ToolTrace key={i} event={item.event} />
              ) : (
                <div key={i} className={`flex ${item.role === "caller" ? "justify-end" : "justify-start"}`}>
                  <p
                    className={`max-w-[80%] whitespace-pre-wrap rounded-[18px] px-3.5 py-2 text-[15px] leading-5 ${
                      item.role === "caller"
                        ? "rounded-br-[6px] bg-tint text-white"
                        : "rounded-bl-[6px] bg-surface-2 text-label"
                    }`}
                  >
                    {item.text}
                  </p>
                </div>
              ),
            )}
            {sending && (
              <div className="flex justify-start" aria-label="The receptionist is replying">
                <span className="flex gap-1 rounded-[18px] rounded-bl-[6px] bg-surface-2 px-3.5 py-3">
                  {[0, 1, 2].map((d) => (
                    <span
                      key={d}
                      className="size-1.5 animate-pulse rounded-full bg-label-3"
                      style={{ animationDelay: `${d * 160}ms` }}
                    />
                  ))}
                </span>
              </div>
            )}
            {ended && (
              <p className="mt-2 text-center text-[13px] text-label-2">
                Call ended. Its summary and outcome appear in Calls once the worker runs.
              </p>
            )}
            <div ref={bottom} />
          </div>
          {error && (
            <div className="px-4 pb-2">
              <Notice tone="bad">{error}</Notice>
            </div>
          )}
          <form onSubmit={send} className="flex items-center gap-2 border-t border-separator px-3 py-2.5">
            <label htmlFor="lab-say" className="sr-only">
              Say something as the caller
            </label>
            <input
              id="lab-say"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              disabled={chatDisabled}
              autoComplete="off"
              placeholder={
                ended
                  ? "This call has ended"
                  : status.llm_available
                    ? "Say something as the caller…"
                    : "Chat needs ANTHROPIC_API_KEY — use the tool bench"
              }
              className="h-9 min-w-0 flex-1 rounded-full border border-separator bg-surface-2 px-4 text-[15px] placeholder:text-label-3 focus:border-tint focus:outline-none focus:ring-3 focus:ring-tint/25 disabled:opacity-60"
            />
            <button
              type="submit"
              aria-label="Send"
              disabled={chatDisabled || sending || !draft.trim()}
              className="grid size-9 shrink-0 place-items-center rounded-full bg-tint text-white transition active:scale-95 disabled:opacity-40"
            >
              <ArrowUp size={18} weight="bold" />
            </button>
          </form>
        </>
      )}
    </Panel>
  );
}

// ── tool bench ──────────────────────────────────────────────────────────────

const BENCH_TOOLS = [
  { tool: "check_availability", label: "Availability" },
  { tool: "book_appointment", label: "Book" },
  { tool: "answer_faq", label: "FAQ" },
  { tool: "request_callback", label: "Callback" },
];

function ToolBench({
  status,
  ensureSession,
  onActivity,
}: {
  status: LabStatus;
  ensureSession: () => Promise<LabSession>;
  onActivity: () => void;
}) {
  const [tool, setTool] = useState("check_availability");
  const [values, setValues] = useState<Record<string, string>>({
    service: status.services[0] ?? "",
    preferred_time: defaultStart(),
    starts_at: defaultStart(),
    customer_name: "Priya Raman",
    question: "Do you offer loaner cars?",
    reason: "Wants to talk to a technician about a noise",
  });
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<ToolEvent[]>([]);
  const set = (k: string) => (e: { target: { value: string } }) => setValues((v) => ({ ...v, [k]: e.target.value }));

  const service = (
    <Field label="Service" htmlFor="bench-service">
      <select id="bench-service" value={values.service} onChange={set("service")} className={fieldInput}>
        {status.services.length === 0 && <option value="">Any</option>}
        {status.services.map((s) => (
          <option key={s}>{s}</option>
        ))}
      </select>
    </Field>
  );

  const fields: Record<string, ReactNode> = {
    check_availability: (
      <>
        {service}
        <Field label="Preferred time (business local)" htmlFor="bench-pref">
          <input id="bench-pref" type="datetime-local" value={values.preferred_time} onChange={set("preferred_time")} className={fieldInput} />
        </Field>
      </>
    ),
    book_appointment: (
      <>
        {service}
        <Field label="Start (business local)" htmlFor="bench-start">
          <input id="bench-start" type="datetime-local" value={values.starts_at} onChange={set("starts_at")} className={fieldInput} />
        </Field>
        <Field label="Customer name" htmlFor="bench-name">
          <input id="bench-name" value={values.customer_name} onChange={set("customer_name")} autoComplete="off" className={fieldInput} />
        </Field>
        <Field label="Text confirmation to (optional)" htmlFor="bench-phone">
          <input
            id="bench-phone"
            type="tel"
            value={values.customer_phone ?? ""}
            onChange={set("customer_phone")}
            placeholder="Caller ID"
            autoComplete="off"
            className={fieldInput}
          />
        </Field>
        {status.booking_fields.map((f) => (
          <Field key={f.key} label={f.description} htmlFor={`bench-${f.key}`}>
            <input id={`bench-${f.key}`} value={values[f.key] ?? ""} onChange={set(f.key)} autoComplete="off" className={fieldInput} />
          </Field>
        ))}
      </>
    ),
    answer_faq: (
      <Field label="Question" htmlFor="bench-q">
        <input id="bench-q" value={values.question} onChange={set("question")} className={fieldInput} />
      </Field>
    ),
    request_callback: (
      <>
        <Field label="Reason" htmlFor="bench-reason">
          <input id="bench-reason" value={values.reason} onChange={set("reason")} className={fieldInput} />
        </Field>
        <Field label="Best time to call" htmlFor="bench-window">
          <input id="bench-window" value={values.preferred_window ?? ""} onChange={set("preferred_window")} placeholder="e.g. after 3pm" className={fieldInput} />
        </Field>
        <Field label="Call back on (optional)" htmlFor="bench-callback-phone">
          <input
            id="bench-callback-phone"
            type="tel"
            value={values.callback_phone ?? ""}
            onChange={set("callback_phone")}
            placeholder="Caller ID"
            autoComplete="off"
            className={fieldInput}
          />
        </Field>
      </>
    ),
  };

  const argumentsFor = (): Record<string, string> => {
    const pick = (...keys: string[]) =>
      Object.fromEntries(keys.filter((k) => values[k]).map((k) => [k, values[k] as string]));
    switch (tool) {
      case "check_availability":
        return pick("service", "preferred_time");
      case "book_appointment":
        return pick(
          "service",
          "starts_at",
          "customer_name",
          "customer_phone",
          ...status.booking_fields.map((f) => f.key),
        );
      case "answer_faq":
        return pick("question");
      default: {
        // Same tool argument as booking; a separate form key so each tab keeps its own value.
        const args = pick("reason", "preferred_window");
        return values.callback_phone ? { ...args, customer_phone: values.callback_phone } : args;
      }
    }
  };

  const run = async (e: FormEvent) => {
    e.preventDefault();
    setRunning(true);
    setError(null);
    try {
      const session = await ensureSession();
      const event = await lab<ToolEvent>(`sessions/${session.session_id}/tools/${tool}`, {
        body: { arguments: argumentsFor() },
      });
      setRuns((r) => [event, ...r].slice(0, 6));
      onActivity();
    } catch (err) {
      setError(err instanceof Error ? err.message : "The tool failed.");
    } finally {
      setRunning(false);
    }
  };

  return (
    <Panel title="Tool bench">
      <form onSubmit={run} className="flex flex-col gap-4 p-4">
        <div role="tablist" aria-label="Tool" className="inline-flex w-fit max-w-full overflow-x-auto rounded-[9px] bg-fill p-0.5">
          {BENCH_TOOLS.map((t) => (
            <button
              key={t.tool}
              type="button"
              role="tab"
              aria-selected={tool === t.tool}
              onClick={() => setTool(t.tool)}
              className={`whitespace-nowrap rounded-[7px] px-3 py-1 text-[13px] font-medium transition duration-200 ease-apple ${
                tool === t.tool
                  ? "bg-surface text-label shadow-[0_1px_2px_rgba(0,0,0,0.12),0_0_0_0.5px_rgba(0,0,0,0.04)]"
                  : "text-label-2 hover:text-label"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <p className="-mt-1 text-[13px] text-label-2">
          Runs <code className="font-mono text-[12px]">{tool}</code> directly — no model involved, so results are
          deterministic.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">{fields[tool]}</div>
        <button type="submit" disabled={running} className={`${btn.base} ${btn.filled} w-fit`}>
          {running ? "Running…" : "Run tool"}
        </button>
        {error && <Notice tone="bad">{error}</Notice>}
      </form>
      {runs.length > 0 && (
        <ul className="border-t border-separator">
          {runs.map((r, i) => (
            <li key={i} className="flex flex-col gap-1 border-b border-separator px-4 py-3 last:border-0">
              <div className="flex items-center gap-2 text-[13px]">
                <span className="font-medium">{humanize(r.tool)}</span>
                <Latency ms={r.latency_ms} />
              </div>
              <p className="text-[15px] leading-5">“{r.result}”</p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// ── day board ───────────────────────────────────────────────────────────────

function DayBoard({ status, refreshKey }: { status: LabStatus; refreshKey: number }) {
  const tz = status.timezone;
  const [day, setDay] = useState(() => localDate(new Date(), tz));
  const [board, setBoard] = useState<Board | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const load = useCallback(() => {
    lab<Board>(`board?day=${day}`)
      .then((b) => {
        setBoard(b);
        setError(null);
        setNow(Date.now());
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Couldn't load the board."));
  }, [day]);
  usePolling(load, 5000, [day, refreshKey]);

  const hours = board?.opening_hours ?? [];
  const open = hours.length ? Math.min(...hours.map((h) => hhmm(h[0] ?? "0:00"))) : 7 * 60;
  const close = hours.length ? Math.max(...hours.map((h) => hhmm(h[1] ?? "0:00"))) : 19 * 60;
  const span = Math.max(60, close - open);
  const pct = (m: number) => `${Math.min(100, Math.max(0, ((m - open) / span) * 100))}%`;
  const ticks: number[] = [];
  for (let m = Math.ceil(open / 60) * 60; m <= close; m += 60) ticks.push(m);
  const isToday = day === localDate(new Date(now), tz);
  const nowMinutes = minutesOfDay(new Date(now), tz);
  const label = new Date(`${day}T12:00:00Z`).toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });

  return (
    <Panel
      title="Day board"
      actions={
        <div className="flex items-center gap-1.5">
          <span className="mr-1 inline-flex items-center gap-1.5 text-[12px] font-medium text-good">
            <span className="relative flex size-2">
              <span className="absolute inline-flex size-full animate-ping rounded-full bg-good opacity-60" />
              <span className="relative inline-flex size-2 rounded-full bg-good" />
            </span>
            Live
          </span>
          <button type="button" aria-label="Previous day" onClick={() => setDay((d) => shiftDay(d, -1))} className={`${btn.base} ${btn.gray} w-8 px-0`}>
            <CaretLeft size={14} weight="bold" />
          </button>
          <button type="button" onClick={() => setDay(localDate(new Date(), tz))} className={`${btn.base} ${btn.gray}`}>
            Today
          </button>
          <button type="button" aria-label="Next day" onClick={() => setDay((d) => shiftDay(d, 1))} className={`${btn.base} ${btn.gray} w-8 px-0`}>
            <CaretRight size={14} weight="bold" />
          </button>
        </div>
      }
    >
      <div className="flex flex-col gap-3 p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <p className="text-[17px] font-semibold">{label}</p>
          <p className="text-[13px] text-label-2">
            {status.bays} {status.bays === 1 ? "bay" : "bays"} · {tz.replace(/_/g, " ")} time
          </p>
        </div>
        {error && <Notice tone="bad">{error}</Notice>}
        {!board ? (
          <div className="flex flex-col gap-2" aria-busy="true">
            {Array.from({ length: status.bays }, (_, i) => (
              <div key={i} className="skeleton h-11" />
            ))}
          </div>
        ) : hours.length === 0 ? (
          <p className="py-8 text-center text-[15px] text-label-2">Closed on this day.</p>
        ) : (
          <div className="overflow-x-auto">
            <div className="min-w-[40rem]">
              <div className="relative ml-16 h-5 text-[11px] text-label-2">
                {ticks.map((m) => (
                  <span key={m} className="num absolute -translate-x-1/2" style={{ left: pct(m) }}>
                    {((Math.floor(m / 60) + 11) % 12) + 1}
                    {m / 60 < 12 ? "a" : "p"}
                  </span>
                ))}
              </div>
              <div className="relative flex flex-col gap-1.5">
                {Array.from({ length: board.bays }, (_, i) => i + 1).map((bay) => (
                  <div key={bay} className="flex items-center gap-2">
                    <span className="w-14 shrink-0 text-[13px] font-medium text-label-2">Bay {bay}</span>
                    <div className="relative h-11 flex-1 rounded-[10px] bg-surface-2">
                      {ticks.map((m) => (
                        <span key={m} aria-hidden className="absolute inset-y-0 w-px bg-separator" style={{ left: pct(m) }} />
                      ))}
                      {board.appointments
                        .filter((a) => a.bay === bay)
                        .map((a) => {
                          const start = minutesOfDay(a.starts_at, tz);
                          const end = minutesOfDay(a.ends_at, tz) || 24 * 60;
                          const vehicle = a.details.vehicle;
                          const text = `${clock(a.starts_at, tz)}–${clock(a.ends_at, tz)} · ${a.service} · ${a.customer_name}${
                            vehicle ? ` · ${vehicle}` : ""
                          }${a.simulated ? " · test booking" : ""}`;
                          return (
                            <div
                              key={a.id}
                              tabIndex={0}
                              aria-label={text}
                              title={text}
                              className={`group absolute inset-y-1 flex min-w-0 items-center overflow-hidden rounded-[7px] bg-tint px-2 text-[12px] font-medium text-white ${
                                a.simulated ? "hatch" : ""
                              }`}
                              style={{
                                left: pct(start),
                                width: `calc(${pct(end)} - ${pct(start)} - 2px)`,
                              }}
                            >
                              <span className="truncate">
                                {a.simulated && "Test · "}
                                {a.service}
                                {vehicle ? ` · ${vehicle}` : ""}
                              </span>
                            </div>
                          );
                        })}
                    </div>
                  </div>
                ))}
                {isToday && nowMinutes >= open && nowMinutes <= close && (
                  <div aria-hidden className="pointer-events-none absolute inset-y-0 left-16 right-0">
                    <span className="absolute inset-y-0 w-0.5 rounded-full bg-critical" style={{ left: pct(nowMinutes) }} />
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-4 text-[12px] text-label-2">
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden className="h-3 w-5 rounded-[4px] bg-tint" /> Booked
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden className="hatch h-3 w-5 rounded-[4px] bg-tint" /> Test booking
          </span>
          {isToday && (
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className="h-3 w-0.5 rounded-full bg-critical" /> Now
            </span>
          )}
        </div>
        {board && board.appointments.length > 0 && (
          <details className="text-[13px]">
            <summary className="text-tint-text">Show as table</summary>
            <table className="mt-2 w-full">
              <thead>
                <tr className="text-left text-label-2">
                  <th className="py-1 font-medium">Time</th>
                  <th className="py-1 font-medium">Bay</th>
                  <th className="py-1 font-medium">Service</th>
                  <th className="py-1 font-medium">Customer</th>
                  <th className="py-1 font-medium">Details</th>
                </tr>
              </thead>
              <tbody>
                {board.appointments.map((a) => (
                  <tr key={a.id} className="border-t border-separator">
                    <td className="num py-1">
                      {clock(a.starts_at, tz)}–{clock(a.ends_at, tz)}
                    </td>
                    <td className="num py-1">{a.bay}</td>
                    <td className="py-1">
                      {a.service} {a.simulated && <Badge value="simulated" />}
                    </td>
                    <td className="py-1">{a.customer_name}</td>
                    <td className="py-1 text-label-2">{Object.values(a.details).join(" · ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
      </div>
    </Panel>
  );
}

// ── activity ────────────────────────────────────────────────────────────────

function Activity({ refreshKey }: { refreshKey: number }) {
  const [items, setItems] = useState<ActivityItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const latest = useRef<string | null>(null);

  const load = useCallback(() => {
    const after = latest.current ? `?after=${encodeURIComponent(latest.current)}` : "";
    lab<ActivityItem[]>(`activity${after}`)
      .then((fresh) => {
        setNow(Date.now());
        setError(null);
        if (fresh[0]) latest.current = fresh[0].occurred_at;
        setItems((prev) => (after ? [...fresh, ...(prev ?? [])].slice(0, 50) : fresh));
      })
      .catch((e) => setError(e instanceof Error ? e.message : "Couldn't load activity."));
  }, []);
  usePolling(load, 3000, [refreshKey]);

  return (
    <Panel title="Activity">
      {error && (
        <div className="p-4">
          <Notice tone="bad">{error}</Notice>
        </div>
      )}
      {items === null ? (
        <div className="flex flex-col gap-2 p-4" aria-busy="true">
          {[0, 1, 2].map((i) => (
            <div key={i} className="skeleton h-10" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <p className="px-4 py-10 text-center text-[15px] text-label-2">
          Nothing yet. Tool calls from test calls and real calls stream in here.
        </p>
      ) : (
        <ul aria-live="polite" className="max-h-[22rem] overflow-y-auto">
          {items.map((i) => (
            <li key={i.id} className="flex items-start gap-3 border-b border-separator px-4 py-2.5 last:border-0">
              <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-full bg-fill text-label-2">
                {i.tool ? <Wrench size={14} aria-hidden /> : <PhoneCall size={14} aria-hidden />}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[13px]">
                  <span className="font-medium">{humanize(i.tool ?? i.event_type)}</span>
                  {i.simulated && <Badge value="simulated" />}
                  {i.latency_ms !== null && <Latency ms={i.latency_ms} />}
                  <span className="num ml-auto text-[12px] text-label-2">{ago(i.occurred_at, now)}</span>
                </div>
                {i.summary && <p className="truncate text-[13px] text-label-2">{i.summary}</p>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// ── page ────────────────────────────────────────────────────────────────────

export function LabClient({ status }: { status: LabStatus }) {
  const [session, setSession] = useState<LabSession | null>(null);
  const [ended, setEnded] = useState(false);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [caller, setCaller] = useState(status.default_caller);
  const [refreshKey, setRefreshKey] = useState(0);
  const bump = useCallback(() => setRefreshKey((k) => k + 1), []);
  const pending = useRef<Promise<LabSession> | null>(null);

  const start = useCallback(async () => {
    const s = await lab<LabSession>("sessions", { body: { caller_e164: caller } });
    setSession(s);
    setEnded(false);
    setItems([{ kind: "say", role: "assistant", text: s.first_message }]);
    return s;
  }, [caller]);

  const ensureSession = useCallback(async () => {
    if (session && !ended) return session;
    pending.current ??= start().finally(() => {
      pending.current = null;
    });
    return pending.current;
  }, [session, ended, start]);

  return (
    <div className="flex flex-col gap-6">
      {!status.llm_available && (
        <Notice tone="info">
          Chatting with the receptionist needs <code className="font-mono text-[13px]">ANTHROPIC_API_KEY</code> on
          the API. The tool bench, day board and activity work without it.
        </Notice>
      )}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Conversation
          status={status}
          session={session}
          ended={ended}
          caller={caller}
          setCaller={setCaller}
          items={items}
          setItems={setItems}
          onActivity={bump}
          onStart={async () => {
            await start();
          }}
          onEnd={async () => {
            if (!session) return;
            await lab(`sessions/${session.session_id}/end`, { body: {} });
            setEnded(true);
          }}
          onDiscard={async () => {
            if (!session) return;
            await lab(`sessions/${session.session_id}`, { method: "DELETE" });
            setSession(null);
            setItems([]);
            bump();
          }}
        />
        <ToolBench status={status} ensureSession={ensureSession} onActivity={bump} />
      </div>
      <DayBoard status={status} refreshKey={refreshKey} />
      <Activity refreshKey={refreshKey} />
    </div>
  );
}
