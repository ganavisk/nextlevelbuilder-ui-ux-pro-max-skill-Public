"use client";

import { ErrorState, btn } from "@/components/ui";

export default function Error({ error, reset }: { error: Error; reset: () => void }) {
  return (
    <div className="flex flex-col items-start gap-4">
      <ErrorState message={error.message || "Something went wrong."} />
      <button onClick={reset} className={`${btn.base} ${btn.secondary}`}>
        Try again
      </button>
    </div>
  );
}
