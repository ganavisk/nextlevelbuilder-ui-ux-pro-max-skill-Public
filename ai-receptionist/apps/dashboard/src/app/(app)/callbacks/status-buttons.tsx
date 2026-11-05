"use client";

import { useState, useTransition } from "react";
import { btn } from "@/components/ui";
import { setCallbackStatus } from "./actions";

const NEXT: Record<string, { label: string; to: "contacted" | "resolved" | "open" }[]> = {
  open: [
    { label: "Mark contacted", to: "contacted" },
    { label: "Resolve", to: "resolved" },
  ],
  contacted: [{ label: "Resolve", to: "resolved" }],
  resolved: [{ label: "Reopen", to: "open" }],
};

export function StatusButtons({ id, status }: { id: string; status: string }) {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex gap-2">
        {(NEXT[status] ?? []).map((n) => (
          <button
            key={n.to}
            type="button"
            disabled={pending}
            className={`${btn.base} ${n.to === "resolved" ? btn.primary : btn.secondary}`}
            onClick={() => start(async () => setError((await setCallbackStatus(id, n.to)).error))}
          >
            {n.label}
          </button>
        ))}
      </div>
      {error && (
        <span role="alert" className="text-[12px] text-critical">
          {error}
        </span>
      )}
    </div>
  );
}
