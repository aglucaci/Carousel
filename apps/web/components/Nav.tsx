"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

const LINKS = [
  { href: "/", label: "For You" },
  { href: "/whats-new", label: "What's New" },
  { href: "/trends", label: "Trends" },
  { href: "/radars", label: "Radars" },
  { href: "/library", label: "Library" },
];

export function Nav() {
  const path = usePathname();
  const [health, setHealth] = useState<"ok" | "degraded" | "down" | undefined>();
  useEffect(() => {
    fetch("/api/v1/health", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject()))
      .then((h) => setHealth(h.status === "ok" ? "ok" : "degraded"))
      .catch(() => setHealth("down"));
  }, []);
  const active = (href: string) => (href === "/" ? path === "/" : path.startsWith(href));
  return (
    <header className="border-b border-border bg-surface">
      <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3 sm:px-6">
        <Link href="/" className="font-serif text-lg font-semibold tracking-[0.18em]">
          CAROUSEL
        </Link>
        <nav aria-label="Main" className="flex gap-1">
          {LINKS.map((l) => (
            <Link
              key={l.href}
              href={l.href}
              aria-current={active(l.href) ? "page" : undefined}
              className={`rounded-md px-3 py-1.5 text-sm ${
                active(l.href) ? "bg-accent-soft font-medium text-accent" : "text-muted hover:text-text"
              }`}
            >
              {l.label}
            </Link>
          ))}
        </nav>
        {health && health !== "ok" && (
          <span role="status" className="ml-auto rounded-md bg-warn-soft px-2 py-1 text-xs text-warn">
            {health === "down" ? "API unreachable" : "API degraded (database or queue)"}
          </span>
        )}
      </div>
    </header>
  );
}
