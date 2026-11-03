import type { Metadata } from "next";
import { apiFetch } from "@/lib/api";
import { readSession } from "@/lib/session";
import type { AgentConfig, Integration, Tenant } from "@/lib/types";
import { Badge, btn, Notice, PageHeader, Section } from "@/components/ui";
import { AgentForm, TenantForm } from "./forms";

export const metadata: Metadata = { title: "Settings" };

const ERRORS: Record<string, string> = {
  oauth_denied: "Access was declined, so nothing was connected. Approve the request to connect.",
  oauth_callback_invalid: "The connection response looked wrong. Start the connection again.",
  forbidden: "Only admins can connect integrations.",
  integration_error:
    "This integration is not set up on the server yet. Ask your administrator to add its credentials.",
  connect_failed: "Could not start the connection. Try again in a minute.",
};

const VERTICAL_LABEL: Record<string, string> = {
  dental: "Dental practice",
  legal: "Law firm",
  auto_repair: "Auto repair shop",
};

const PROVIDER_LABEL: Record<string, string> = {
  google_calendar: "Google Calendar",
  hubspot: "HubSpot",
};

export default async function SettingsPage({
  searchParams,
}: {
  searchParams: Promise<{ connected?: string; error?: string }>;
}) {
  const sp = await searchParams;
  const [tenant, agent, integrations, session] = await Promise.all([
    apiFetch<Tenant>("/tenants/me"),
    apiFetch<AgentConfig>("/tenants/me/agent-config"),
    apiFetch<Integration[]>("/integrations"),
    readSession(),
  ]);
  const readOnly = session?.role === "viewer";

  return (
    <>
      <PageHeader title="Settings" description={[tenant.name, VERTICAL_LABEL[tenant.vertical]].filter(Boolean).join(" · ")} />
      {sp.connected && (
        <Notice tone="good">{PROVIDER_LABEL[sp.connected] ?? sp.connected} is connected.</Notice>
      )}
      {sp.error && <Notice tone="bad">{ERRORS[sp.error] ?? "Something went wrong while connecting."}</Notice>}
      {readOnly && <Notice tone="info">You have view-only access. Ask an admin to change settings.</Notice>}

      <Section title="Integrations" flush>
        <ul>
          {integrations.map((i) => (
            <li
              key={i.provider}
              className="flex items-center justify-between gap-4 border-b border-separator px-4 py-3 last:border-0"
            >
              <div className="flex items-center gap-3">
                <span className="text-[15px]">{PROVIDER_LABEL[i.provider] ?? i.provider}</span>
                <Badge value={i.status} />
              </div>
              {!readOnly && (
                <a
                  href={`/api/integrations/${i.provider}/connect`}
                  className={`${btn.base} ${i.connected ? btn.gray : btn.filled}`}
                >
                  {i.connected ? "Reconnect" : "Connect"}
                </a>
              )}
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Business">
        <TenantForm timezone={tenant.timezone} readOnly={readOnly} />
      </Section>

      <Section
        title={
          <>
            Receptionist <span className="num normal-case tracking-normal">· version {agent.version}</span>
          </>
        }
      >
        <AgentForm agent={agent} readOnly={readOnly} />
      </Section>
    </>
  );
}
