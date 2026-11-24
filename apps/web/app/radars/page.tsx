"use client";

import Link from "next/link";
import { Badge, ButtonLink, Card, Empty, ErrorNote, Loading, PageHeader, fmtDateTime, sourceName } from "@/components/ui";
import { client, useApi } from "@/lib/api";

export default function RadarsPage() {
  const { data, error, loading } = useApi(() => client.radars(), []);
  return (
    <>
      <PageHeader
        title="Radars"
        subtitle="Each radar watches the literature on one topic."
        actions={
          <ButtonLink href="/radars/new" variant="primary">
            New radar
          </ButtonLink>
        }
      />
      <ErrorNote error={error} />
      {loading && !data ? (
        <Loading />
      ) : !data?.length ? (
        <Empty title="No radars yet">Create one to start getting recommendations.</Empty>
      ) : (
        <ul className="grid gap-3 sm:grid-cols-2">
          {data.map((r) => (
            <li key={r.radar_id}>
              <Card className="h-full">
                <div className="flex items-start justify-between gap-2">
                  <h2 className="font-serif text-lg font-semibold">
                    <Link href={`/radars/${encodeURIComponent(r.radar_id)}`} className="hover:text-accent hover:underline">
                      {r.title}
                    </Link>
                  </h2>
                  <span className="flex shrink-0 gap-1">
                    {r.schedule && <Badge tone={r.state === "active" ? "good" : "neutral"}>{r.schedule}</Badge>}
                    {r.state !== "active" && <Badge tone="warn">{r.state}</Badge>}
                  </span>
                </div>
                {r.description && <p className="mt-1 text-sm text-muted">{r.description}</p>}
                <p className="mt-2 text-sm text-muted">
                  {(r.config?.sources ?? []).map(sourceName).join(", ")}
                  {r.config?.mode ? ` · ${r.config.mode}` : ""}
                </p>
                <p className="mt-1 text-sm">
                  {r.latest_run ? (
                    <>
                      {r.n_runs} {r.n_runs === 1 ? "scan" : "scans"}, latest {fmtDateTime(r.latest_run.executed_at)} ·{" "}
                      <Link href={`/?radar=${encodeURIComponent(r.radar_id)}`} className="text-accent hover:underline">
                        {r.latest_run.n_items} recommended
                      </Link>
                    </>
                  ) : (
                    <span className="text-muted">Not scanned yet</span>
                  )}
                </p>
              </Card>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
