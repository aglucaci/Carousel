"use client";

import type { Arrival, Features, PaperSummary } from "@/lib/api";
import { Badge, Button, Flag, Meter, fmtDate } from "./ui";

export function authorsLine(authors?: string[]) {
  if (!authors?.length) return "";
  return authors.length > 3 ? `${authors.slice(0, 3).join(", ")}, et al.` : authors.join(", ");
}

export function PaperTitle({ paper, as: Tag = "h2" }: { paper: PaperSummary; as?: "h2" | "h3" }) {
  const title = paper.title || "Untitled";
  return (
    <Tag className="font-serif text-[1.05rem] font-semibold leading-snug">
      {paper.url ? (
        <a href={paper.url} target="_blank" rel="noreferrer" className="hover:text-accent hover:underline">
          {title}
        </a>
      ) : (
        title
      )}
    </Tag>
  );
}

export function PaperMeta({ paper }: { paper: PaperSummary }) {
  const parts = [paper.venue, fmtDate(paper.earliest_public_date)].filter(Boolean);
  return (
    <div className="mt-1 space-y-0.5 text-sm text-muted">
      {paper.authors?.length ? <p>{authorsLine(paper.authors)}</p> : null}
      {parts.length ? <p>{parts.join(" · ")}</p> : null}
    </div>
  );
}

export function PaperCard({
  rank,
  paper,
  features,
  flags,
  arrival,
  saved,
  dismissed,
  busy,
  onSave,
  onDismiss,
  onWhy,
}: {
  rank: number;
  paper: PaperSummary;
  features: Features;
  flags: string[];
  arrival?: Arrival | null;
  saved: boolean;
  dismissed: boolean;
  busy?: boolean;
  onSave: () => void;
  onDismiss: () => void;
  onWhy: () => void;
}) {
  if (dismissed) {
    return (
      <li data-testid="paper-card" data-dismissed="true" className="flex items-center justify-between gap-3 rounded-lg border border-dashed border-border px-4 py-2 text-sm text-muted">
        <span className="min-w-0 truncate">
          Dismissed: <span className="italic">{paper.title}</span>. It won&apos;t be recommended again.
        </span>
        <Button variant="ghost" onClick={onDismiss} disabled={busy}>
          Undo
        </Button>
      </li>
    );
  }
  return (
    <li data-testid="paper-card" className="rounded-lg border border-border bg-surface p-4">
      <div className="flex gap-3">
        <span className="w-6 shrink-0 pt-0.5 text-right font-mono text-sm text-muted" aria-label={`Rank ${rank}`}>
          {rank}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-start gap-2">
            <div className="min-w-0 flex-1">
              <PaperTitle paper={paper} />
            </div>
          </div>
          <PaperMeta paper={paper} />
          {((arrival && arrival !== "seen_before") || flags.length > 0) && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {arrival === "newly_published" && (
                <Badge tone="accent" title="Published after the previous scan's window ended">
                  Newly published
                </Badge>
              )}
              {arrival === "newly_observed" && (
                <Badge title="Published before the previous scan, but this radar sees it for the first time (for example, indexed late)">
                  Newly observed
                </Badge>
              )}
              {flags.map((f) => (
                <Flag key={f} flag={f} />
              ))}
            </div>
          )}
          <div className="mt-3 flex flex-wrap items-end justify-between gap-3">
            <div className="space-y-1">
              <Meter label="Relevance" value={features.relevance} />
              <Meter label="Novelty" value={features.novelty} />
              <Meter label="Method match" value={features.method_match} />
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="ghost" onClick={onWhy}>
                Why this paper?
              </Button>
              <Button onClick={onDismiss} disabled={busy}>
                Dismiss
              </Button>
              <Button variant={saved ? "primary" : "secondary"} aria-pressed={saved} onClick={onSave} disabled={busy}>
                {saved ? "Saved" : "Save"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </li>
  );
}
