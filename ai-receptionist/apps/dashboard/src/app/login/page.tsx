import type { Metadata } from "next";
import { Phone } from "@phosphor-icons/react/dist/ssr";
import { LoginForm } from "./login-form";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <main className="grid min-h-[100dvh] place-items-center px-4 py-12">
      <div className="flex w-full max-w-[380px] flex-col items-center gap-6">
        <span aria-hidden className="grid size-16 place-items-center rounded-[18px] bg-tint text-white shadow-sm">
          <Phone size={32} weight="fill" />
        </span>
        <div className="text-center">
          <h1 className="text-[28px] font-bold leading-[34px] tracking-[-0.02em]">Sign in</h1>
          <p className="mt-1.5 text-[15px] text-label-2">
            Review calls, work the callback queue and test your receptionist.
          </p>
        </div>
        <div className="w-full rounded-[20px] bg-surface p-6 shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
          <LoginForm />
        </div>
      </div>
    </main>
  );
}
