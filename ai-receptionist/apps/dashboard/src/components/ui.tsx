import Link from "next/link";
import { WarningCircle, CheckCircle, Info, Tray, CaretLeft, CaretRight } from "@phosphor-icons/react/dist/ssr";
import type { ReactNode } from "react";

// Apple's button styles: filled, tinted, gray, destructive, plain.
// See design-system/MASTER.md. The short aliases keep older call sites working.
const base =
  "inline-flex h-8 select-none items-center justify-center gap-1.5 whitespace-nowrap rounded-lg px-3 text-[14px] font-medium transition duration-200 ease-apple active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40";
const filled = "bg-tint text-white hover:brightness-110";
const tinted = "bg-info-bg text-tint-text hover:brightness-105";
const gray = "bg-fill text-label hover:bg-fill-strong";
const destructive = "bg-critical-bg text-critical hover:brightness-105";
const plain = "text-tint-text hover:bg-fill";

export const btn = {
  base,
  filled,
  tinted,
  gray,
  destructive,
  plain,
  primary: filled,
  secondary: gray,
  danger: destructive,
};

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="text-[28px] font-bold leading-[34px] tracking-[-0.02em] md:text-[34px] md:leading-[41px]">
          {title}
        </h1>
        {description && <p className="mt-1 max-w-[65ch] text-[15px] text-label-2">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}

/** An inset grouped list section: footnote header above a rounded surface. */
export function Section({
  title,
  footer,
  actions,
  children,
  flush = false,
}: {
  title?: ReactNode;
  footer?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  flush?: boolean;
}) {
  return (
    <section className="flex flex-col gap-2">
      {(title || actions) && (
        <div className="flex items-end justify-between gap-3 px-1">
          {title && (
            <h2 className="text-[13px] font-medium uppercase tracking-wide text-label-2">{title}</h2>
          )}
          {actions}
        </div>
      )}
      <div className={`overflow-hidden rounded-[14px] bg-surface ${flush ? "" : "p-4"}`}>{children}</div>
      {footer && <p className="px-1 text-[13px] text-label-2">{footer}</p>}
    </section>
  );
}

type Tone = "good" | "warning" | "critical" | "info" | "neutral";

const TONE_CLASS: Record<Tone, string> = {
  good: "bg-good-bg text-good",
  warning: "bg-warning-bg text-warning",
  critical: "bg-critical-bg text-critical",
  info: "bg-info-bg text-info",
  neutral: "bg-neutral-bg text-neutral",
};

const STATUS_TONE: Record<string, Tone> = {
  appointment_booked: "good",
  confirmed: "good",
  synced: "good",
  connected: "good",
  resolved: "good",
  positive: "good",
  completed: "good",
  live: "good",
  escalated: "warning",
  callback_requested: "warning",
  pending: "warning",
  failed: "warning",
  in_progress: "warning",
  open: "warning",
  contacted: "info",
  simulated: "info",
  updating: "info",
  negative: "critical",
  dead: "critical",
  cancelled: "critical",
  abandoned: "critical",
  no_show: "critical",
};

const STATUS_LABEL: Record<string, string> = { simulated: "test" };

export function Badge({ value, tone }: { value: string | null; tone?: Tone }) {
  if (!value) return <span className="text-label-3">—</span>;
  const cls = TONE_CLASS[tone ?? STATUS_TONE[value] ?? "neutral"];
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full px-2 py-0.5 text-[12px] font-medium leading-4 ${cls}`}
    >
      {(STATUS_LABEL[value] ?? value).replace(/_/g, " ")}
    </span>
  );
}

/** Apple's segmented control, as links (filters live in the URL). */
export function SegmentedControl({
  label,
  items,
}: {
  label: string;
  items: { href: string; label: string; active: boolean }[];
}) {
  return (
    <nav aria-label={label} className="max-w-full overflow-x-auto">
      <div className="inline-flex rounded-[9px] bg-fill p-0.5">
        {items.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            aria-current={item.active ? "true" : undefined}
            className={`whitespace-nowrap rounded-[7px] px-3 py-1 text-[13px] font-medium capitalize transition duration-200 ease-apple ${
              item.active
                ? "bg-surface text-label shadow-[0_1px_2px_rgba(0,0,0,0.12),0_0_0_0.5px_rgba(0,0,0,0.04)]"
                : "text-label-2 hover:text-label"
            }`}
          >
            {item.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}

export function EmptyState({ title, hint, action }: { title: string; hint: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
      <Tray size={34} weight="light" className="text-label-3" aria-hidden />
      <p className="text-[17px] font-semibold">{title}</p>
      <p className="max-w-[48ch] text-[14px] text-label-2">{hint}</p>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div role="alert" className="flex items-start gap-3 rounded-[14px] bg-critical-bg p-4 text-[14px] text-critical">
      <WarningCircle size={20} weight="fill" className="mt-px shrink-0" aria-hidden />
      <div>
        <p className="font-semibold">Couldn't load this page</p>
        <p className="mt-0.5">{message}</p>
      </div>
    </div>
  );
}

export function Notice({ tone, children }: { tone: "good" | "bad" | "info"; children: ReactNode }) {
  const style = {
    good: { cls: "bg-good-bg text-good", Icon: CheckCircle, role: "status" },
    bad: { cls: "bg-critical-bg text-critical", Icon: WarningCircle, role: "alert" },
    info: { cls: "bg-info-bg text-info", Icon: Info, role: "status" },
  }[tone];
  const { Icon } = style;
  return (
    <p role={style.role} className={`flex items-start gap-2 rounded-[10px] px-3 py-2 text-[14px] ${style.cls}`}>
      <Icon size={18} weight="fill" className="mt-px shrink-0" aria-hidden />
      <span>{children}</span>
    </p>
  );
}

export function Pagination({
  basePath,
  params,
  offset,
  limit,
  total,
}: {
  basePath: string;
  params: Record<string, string | undefined>;
  offset: number;
  limit: number;
  total: number;
}) {
  if (total <= limit) return null;
  const href = (nextOffset: number) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
    if (nextOffset > 0) q.set("offset", String(nextOffset));
    const s = q.toString();
    return s ? `${basePath}?${s}` : basePath;
  };
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  return (
    <nav aria-label="Pages" className="flex items-center justify-between px-1 text-[13px] text-label-2">
      <span className="num">
        {from}–{to} of {total}
      </span>
      <div className="flex gap-1">
        {offset > 0 ? (
          <Link aria-label="Previous page" className={`${btn.base} ${btn.gray} w-8 px-0`} href={href(Math.max(0, offset - limit))}>
            <CaretLeft size={14} weight="bold" />
          </Link>
        ) : null}
        {offset + limit < total ? (
          <Link aria-label="Next page" className={`${btn.base} ${btn.gray} w-8 px-0`} href={href(offset + limit)}>
            <CaretRight size={14} weight="bold" />
          </Link>
        ) : null}
      </div>
    </nav>
  );
}

export function SkeletonRows({ rows = 8 }: { rows?: number }) {
  return (
    <div className="divide-y divide-separator rounded-[14px] bg-surface" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex items-center gap-6 px-4 py-3.5">
          <div className="skeleton h-4 w-28" />
          <div className="skeleton h-4 w-36" />
          <div className="skeleton h-4 flex-1" />
          <div className="skeleton h-4 w-16" />
        </div>
      ))}
    </div>
  );
}

/** A grouped table: rounded surface, hairline rows, scrolls sideways on phones. */
export function Table({ children, minWidth = "40rem" }: { children: ReactNode; minWidth?: string }) {
  return (
    <div className="relative overflow-x-auto rounded-[14px] bg-surface">
      <table className="w-full" style={{ minWidth }}>
        {children}
      </table>
    </div>
  );
}

export const th =
  "border-b border-separator px-4 py-2.5 text-left text-[12px] font-medium uppercase tracking-wide text-label-2";
export const td = "px-4 py-3 align-middle text-[14px]";
export const tr = "border-b border-separator last:border-0 transition-colors duration-150 hover:bg-surface-2";
