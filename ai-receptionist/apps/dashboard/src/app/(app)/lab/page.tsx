import type { Metadata } from "next";
import { apiFetch } from "@/lib/api";
import { readSession } from "@/lib/session";
import type { LabStatus } from "@/lib/types";
import { Notice, PageHeader } from "@/components/ui";
import { LabClient } from "./lab-client";

export const metadata: Metadata = { title: "Test Lab" };

export default async function LabPage() {
  const session = await readSession();
  if (session?.role === "viewer") {
    return (
      <>
        <PageHeader title="Test Lab" />
        <Notice tone="info">The Test Lab books real appointments, so it needs an admin account.</Notice>
      </>
    );
  }
  const status = await apiFetch<LabStatus>("/test-lab/status");
  return (
    <>
      <PageHeader
        title="Test Lab"
        description="Rehearse calls with your real receptionist, run any tool directly, and watch the schedule fill in live. Test calls book real appointments but send no texts or emails, and stay out of analytics."
      />
      <LabClient status={status} />
    </>
  );
}
