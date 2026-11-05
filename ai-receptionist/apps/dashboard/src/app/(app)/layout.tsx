import { redirect } from "next/navigation";
import { Sidebar } from "@/components/sidebar";
import { readSession } from "@/lib/session";

export default async function AppLayout({ children }: { children: React.ReactNode }) {
  const session = await readSession();
  if (!session) redirect("/login");
  return (
    <div className="flex min-h-[100dvh] flex-col md:flex-row">
      <Sidebar
        tenantName={session.tenantName}
        userName={session.userName}
        canTest={session.role !== "viewer"}
      />
      <main className="min-w-0 flex-1 px-4 pb-16 pt-6 md:px-8 md:pt-10">
        <div className="mx-auto flex max-w-[1200px] flex-col gap-7">{children}</div>
      </main>
    </div>
  );
}
