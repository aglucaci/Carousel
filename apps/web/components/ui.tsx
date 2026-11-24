import Link from "next/link";
import type { ComponentProps, ReactNode } from "react";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-accent text-accent-text hover:opacity-90 border-transparent",
  secondary: "bg-surface text-text border-border hover:bg-surface-2",
  ghost: "bg-transparent text-muted border-transparent hover:text-text hover:bg-surface-2",
  danger: "bg-surface text-bad border-border hover:bg-bad-soft",
};

export function Button({
  variant = "secondary",
  className = "",
  ...props
}: ComponentProps<"button"> & { variant?: Variant }) {
  return (
    <button
      {...props}
      className={`inline-flex items-center justify-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${VARIANTS[variant]} ${className}`}
    />
  );
}

export function ButtonLink({ href, children, variant = "secondary" }: { href: string; children: ReactNode; variant?: Variant }) {
  return (
    <Link
      href={href}
      className={`inline-flex items-center justify-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium transition ${VARIANTS[variant]}`}
    >
      {children}
    </Link>
  );
}

type Tone = "neutral" | "accent" | "good" | "warn" | "bad";
const TONES: Record<Tone, string> = {
  neutral: "bg-surface-2 text-muted",
  accent: "bg-accent-soft text-accent",
  good: "bg-good-soft text-good",
  warn: "bg-warn-soft text-warn",
  bad: "bg-bad-soft text-bad",
};

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-border bg-surface p-4 ${className}`}>{children}</div>;
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="font-serif text-2xl font-semibold">{title}</h1>
        {subtitle && <div className="mt-1 text-sm text-muted">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error?: Error }) {
  if (!error) return null;
  return (
    <div role="alert" className="mb-4 rounded-md border border-bad/30 bg-bad-soft px-3 py-2 text-sm text-bad">
      {error.message}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-border px-6 py-12 text-center">
      <p className="font-medium">{title}</p>
      {children && <div className="mt-2 text-sm text-muted">{children}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" className="py-8 text-center text-sm text-muted">
      {label}
    </p>
  );
}

/** A 0–1 value as a small bar; null means the signal was inactive for this run. */
export function Meter({ label, value }: { label: string; value?: number | null }) {
  const pct = value == null ? 0 : Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs" title={value == null ? `${label}: inactive` : `${label}: ${value.toFixed(2)}`}>
      <span className="w-24 shrink-0 text-muted">{label}</span>
      <span className="h-1.5 w-20 overflow-hidden rounded-full bg-surface-2" aria-hidden>
        <span className="block h-full rounded-full bg-accent" style={{ width: `${pct}%` }} />
      </span>
      <span className="w-8 tabular-nums text-muted">{value == null ? "—" : value.toFixed(2)}</span>
    </div>
  );
}

export const FLAGS: Record<string, { label: string; tone: Tone; help: string }> = {
  low_relevance: {
    label: "Low relevance",
    tone: "warn",
    help: "Well below this run's top relevance; shown only after every better-matching paper.",
  },
  novelty_insufficient_history: {
    label: "Novelty unknown",
    tone: "neutral",
    help: "Too few earlier papers to judge novelty yet; it gets no novelty credit.",
  },
  correction_or_notice: {
    label: "Correction or notice",
    tone: "warn",
    help: "An erratum, retraction notice, or similar notice rather than a research paper.",
  },
  retracted: { label: "Retracted", tone: "bad", help: "A source marks this paper as retracted." },
  corrected: {
    label: "Corrected",
    tone: "warn",
    help: "A correction, erratum, or partial retraction has been published for this paper (Crossref; may come from Retraction Watch).",
  },
  expression_of_concern: {
    label: "Expression of concern",
    tone: "bad",
    help: "The publisher or Retraction Watch has issued an expression of concern about this paper.",
  },
  published_version: {
    label: "Since published",
    tone: "accent",
    help: "This preprint has a journal publication. Both are kept as separate papers; see Other versions.",
  },
  has_preprint: {
    label: "Has preprint",
    tone: "neutral",
    help: "An earlier preprint of this article exists. Both are kept as separate papers; see Other versions.",
  },
};

export function Flag({ flag }: { flag: string }) {
  const f = FLAGS[flag] ?? { label: flag.replaceAll("_", " "), tone: "neutral" as Tone, help: flag };
  return (
    <Badge tone={f.tone} title={f.help}>
      {f.label}
    </Badge>
  );
}

export const SOURCE_NAMES: Record<string, string> = {
  pubmed: "PubMed",
  openalex: "OpenAlex",
  europepmc: "Europe PMC",
  crossref: "Crossref",
  biorxiv: "bioRxiv",
  medrxiv: "medRxiv",
  arxiv: "arXiv",
};

export function sourceName(s: string) {
  return SOURCE_NAMES[s] ?? s;
}

export function fmtDate(iso?: string | null) {
  if (!iso) return "";
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00Z` : iso);
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export function fmtDateTime(iso?: string | null) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** One line on what a source returned, including what it did not. */
export function coverageLine(c: { source: string; status: string; role?: string; retrieved?: number; total_available?: number | null; truncation_reason?: string | null; note?: string; error?: string }) {
  const name = sourceName(c.source);
  if (c.status === "no_connector") return `${name}: not searched (no connector yet)`;
  if (c.status === "error") return `${name}: failed (${c.error ?? "error"})`;
  if (c.role === "verification")
    return `${name}: ${c.retrieved ?? 0} of ${(c.total_available ?? 0).toLocaleString()} DOIs verified${
      c.truncation_reason ? ` (stopped: ${c.truncation_reason})` : ""
    }`;
  const of = c.total_available != null ? ` of ${c.total_available.toLocaleString()}` : "";
  const why =
    c.truncation_reason === "no_api_key"
      ? " (first page only: no API key)"
      : c.truncation_reason === "max_results"
        ? " (scan limit)"
        : c.truncation_reason
          ? ` (${c.truncation_reason})`
          : "";
  return `${name}: ${c.retrieved ?? 0}${of} retrieved${why}`;
}
