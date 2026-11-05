"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  ChartBar,
  PhoneCall,
  CalendarCheck,
  PhoneIncoming,
  ArrowsClockwise,
  GearSix,
  Flask,
  SignOut,
} from "@phosphor-icons/react";

const NAV = [
  { href: "/", label: "Overview", icon: ChartBar },
  { href: "/calls", label: "Calls", icon: PhoneCall },
  { href: "/appointments", label: "Appointments", icon: CalendarCheck },
  { href: "/callbacks", label: "Callbacks", icon: PhoneIncoming },
  { href: "/crm", label: "CRM sync", icon: ArrowsClockwise },
  { href: "/settings", label: "Settings", icon: GearSix },
];

const LAB = { href: "/lab", label: "Test Lab", icon: Flask };

export function Sidebar({
  tenantName,
  userName,
  canTest,
}: {
  tenantName: string;
  userName: string;
  canTest: boolean;
}) {
  const pathname = usePathname();
  const items = canTest ? [...NAV.slice(0, 5), LAB, NAV[5]!] : NAV;
  return (
    <aside className="material sticky top-0 z-20 flex w-full shrink-0 flex-col border-b border-separator md:h-[100dvh] md:w-60 md:border-b-0 md:border-r">
      <div className="flex items-center gap-2.5 px-4 pb-2 pt-4 md:pt-6">
        <span
          aria-hidden
          className="grid size-8 shrink-0 place-items-center rounded-[9px] bg-tint text-[13px] font-semibold text-white"
        >
          {tenantName.slice(0, 1)}
        </span>
        <div className="min-w-0">
          <p className="truncate text-[14px] font-semibold leading-5">{tenantName}</p>
          <p className="text-[12px] leading-4 text-label-2">AI receptionist</p>
        </div>
      </div>
      <nav
        aria-label="Main"
        className="flex gap-0.5 overflow-x-auto px-2 pb-2 md:mt-2 md:flex-1 md:flex-col md:overflow-visible"
      >
        {items.map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={`flex items-center gap-2.5 whitespace-nowrap rounded-[8px] px-2.5 py-1.5 text-[14px] transition duration-150 ease-apple active:scale-[0.98] ${
                active ? "bg-tint font-medium text-white" : "text-label hover:bg-fill"
              }`}
            >
              <Icon size={20} weight={active ? "fill" : "regular"} className={active ? "" : "text-tint-text"} />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="hidden items-center justify-between border-t border-separator px-4 py-3 md:flex">
        <span className="truncate text-[13px] text-label-2">{userName}</span>
        <form action="/api/auth/logout" method="post">
          <button
            type="submit"
            aria-label="Sign out"
            className="grid size-8 place-items-center rounded-[8px] text-label-2 transition hover:bg-fill hover:text-label active:scale-95"
          >
            <SignOut size={18} />
          </button>
        </form>
      </div>
    </aside>
  );
}
