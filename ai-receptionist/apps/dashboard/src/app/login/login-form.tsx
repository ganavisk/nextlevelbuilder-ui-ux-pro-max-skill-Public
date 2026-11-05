"use client";

import { useActionState } from "react";
import { login, type LoginState } from "./actions";
import { btn, Notice } from "@/components/ui";

const initial: LoginState = { error: null, tenant_slug: "", email: "" };

function Field({
  label,
  name,
  type = "text",
  autoComplete,
  hint,
  defaultValue,
}: {
  label: string;
  name: string;
  type?: string;
  autoComplete?: string;
  hint?: string;
  defaultValue?: string;
}) {
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={name} className="text-[13px] font-medium text-label-2">
        {label}
      </label>
      <input
        id={name}
        name={name}
        type={type}
        autoComplete={autoComplete}
        defaultValue={defaultValue}
        required
        className="w-full rounded-[10px] border border-separator bg-surface-2 px-3 py-2.5 text-[15px] transition duration-150 focus:border-tint focus:outline-none focus:ring-3 focus:ring-tint/25"
      />
      {hint && <p className="text-[13px] text-label-2">{hint}</p>}
    </div>
  );
}

export function LoginForm() {
  const [state, action, pending] = useActionState(login, initial);
  return (
    <form action={action} className="flex flex-col gap-5">
      {state.error && <Notice tone="bad">{state.error}</Notice>}
      <Field
        label="Business ID"
        name="tenant_slug"
        defaultValue={state.tenant_slug}
        autoComplete="organization"
        hint="The short ID your onboarding contact gave you."
      />
      <Field
        label="Email"
        name="email"
        type="email"
        defaultValue={state.email}
        autoComplete="username"
      />
      <Field label="Password" name="password" type="password" autoComplete="current-password" />
      <button type="submit" disabled={pending} className={`${btn.base} ${btn.filled} h-10 w-full text-[15px]`}>
        {pending ? "Signing in..." : "Sign in"}
      </button>
    </form>
  );
}
