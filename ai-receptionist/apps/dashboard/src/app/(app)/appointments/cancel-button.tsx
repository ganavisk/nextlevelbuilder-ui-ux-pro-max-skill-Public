"use client";

import { useState, useTransition } from "react";
import { btn } from "@/components/ui";
import { cancelAppointment } from "./actions";

export function CancelButton({ id, name }: { id: string; name: string }) {
  const [pending, start] = useTransition();
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        disabled={pending}
        className={`${btn.base} ${btn.danger}`}
        onClick={() => {
          if (!confirm(`Cancel the appointment for ${name}? This removes it from the calendar.`)) return;
          start(async () => setError((await cancelAppointment(id)).error));
        }}
      >
        {pending ? "Cancelling..." : "Cancel"}
      </button>
      {error && (
        <span role="alert" className="max-w-[24ch] text-right text-[12px] text-critical">
          {error}
        </span>
      )}
    </div>
  );
}
