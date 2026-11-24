"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { PaperCard } from "@/components/PaperCard";
import { WhyDrawer } from "@/components/WhyDrawer";
import { ButtonLink, Empty, ErrorNote, Loading, PageHeader, coverageLine, fmtDate, fmtDateTime } from "@/components/ui";
import { client, useApi, type FeedbackAction, type RankedItem } from "@/lib/api";

function Feed({ radarId, runId }: { radarId: string; runId?: string }) {
  const { data, error, loading, setData } = useApi(() => client.papers(radarId, runId), [radarId, runId]);
  const delta = useApi(() => (runId ? Promise.resolve(undefined) : client.delta(radarId).catch(() => undefined)), [radarId, runId]);
  const [why, setWhy] = useState<string | undefined>();
  const [busy, setBusy] = useState<string | undefined>();
  const [actionError, setActionError] = useState<Error | undefined>();

  const entered = useMemo(
    () => (delta.data?.previous_run_id ? new Set(delta.data.entered) : new Set<string>()),
    [delta.data],
  );

  async function toggle(item: RankedItem, action: FeedbackAction) {
    const on = action === "save" ? item.saved : item.dismissed;
    setBusy(item.paper_id);
    setActionError(undefined);
    try {
      const { state } = await client.feedback(radarId, item.paper_id, action, on);
      setData((d) =>
        d && {
          ...d,
          items: d.items.map((it) =>
            it.paper_id === item.paper_id
              ? {
                  ...it,
                  saved: state.save.includes(it.paper_id),
                  dismissed: state.dismiss.includes(it.paper_id),
                }
              : it,
          ),
        },
      );
    } catch (e) {
      setActionError(e as Error);
    } finally {
      setBusy(undefined);
    }
  }

  if (error) return <ErrorNote error={error} />;
  if (loading && !data) return <Loading />;
  if (!data) return null;
  const run = data.run;
  const partial = run.coverage.filter((c) => c.status !== "ok" || c.truncated);
  return (
    <>
      <div className="mb-4 rounded-lg border border-border bg-surface px-4 py-3 text-sm">
        <p>
          {runId ? "Earlier run" : "Latest run"} from <strong>{fmtDateTime(run.executed_at)}</strong>, covering papers up to{" "}
          {fmtDate(run.as_of)} · {run.n_candidates.toLocaleString()} candidates → {run.n_items} recommended · {run.mode} mode
          {!runId && delta.data?.previous_run_id && (
            <>
              {" "}
              · <strong>{delta.data.entered.length}</strong> new since the previous run
            </>
          )}
        </p>
        {partial.length > 0 && (
          <ul className="mt-1 text-xs text-muted">
            {partial.map((c) => (
              <li key={c.source}>{coverageLine(c)}</li>
            ))}
          </ul>
        )}
        {runId && (
          <p className="mt-1">
            <Link href={`/?radar=${encodeURIComponent(radarId)}`} className="text-accent hover:underline">
              Back to the latest run
            </Link>
          </p>
        )}
      </div>
      <ErrorNote error={actionError} />
      {data.items.length === 0 ? (
        <Empty title="This run recommended nothing">
          The sources returned no papers in the window that passed the radar&apos;s filters.{" "}
          <Link className="text-accent hover:underline" href={`/radars/${encodeURIComponent(radarId)}`}>
            Check the radar
          </Link>
          .
        </Empty>
      ) : (
        <ol className="space-y-3" aria-label="Recommended papers">
          {data.items.map((it) => (
            <PaperCard
              key={it.paper_id}
              rank={it.rank}
              paper={it.paper}
              features={it.features}
              flags={it.flags}
              isNew={entered.has(it.paper_id)}
              saved={it.saved}
              dismissed={it.dismissed}
              busy={busy === it.paper_id}
              onSave={() => toggle(it, "save")}
              onDismiss={() => toggle(it, "dismiss")}
              onWhy={() => setWhy(it.paper_id)}
            />
          ))}
        </ol>
      )}
      <p className="mt-6 text-xs text-muted">
        Saving and dismissing apply from the radar&apos;s next scan: dismissed papers aren&apos;t recommended again, and saved
        papers move to your Library.
      </p>
      {why && <WhyDrawer paperId={why} radarId={radarId} runId={run.run_id} onClose={() => setWhy(undefined)} />}
    </>
  );
}

function ForYou() {
  const params = useSearchParams();
  const router = useRouter();
  const { data: radars, error, loading } = useApi(() => client.radars(), []);
  const withRuns = useMemo(
    () =>
      (radars ?? [])
        .filter((r) => r.n_runs > 0)
        .sort((a, b) => (b.latest_run?.executed_at ?? "").localeCompare(a.latest_run?.executed_at ?? "")),
    [radars],
  );
  const selected = params.get("radar") ?? withRuns[0]?.radar_id;
  const runId = params.get("run") ?? undefined;
  const current = radars?.find((r) => r.radar_id === selected);

  if (error) return <ErrorNote error={error} />;
  if (loading && !radars) return <Loading />;
  if (!radars?.length) {
    return (
      <>
        <PageHeader title="For You" />
        <Empty title="No radars yet">
          <p>A radar watches the literature on a topic and ranks what it finds for you.</p>
          <div className="mt-4">
            <ButtonLink href="/radars/new" variant="primary">
              Create a radar
            </ButtonLink>
          </div>
        </Empty>
      </>
    );
  }
  if (!withRuns.length || !current) {
    return (
      <>
        <PageHeader title="For You" />
        <Empty title="No scans yet">
          Run a scan from{" "}
          <Link href="/radars" className="text-accent hover:underline">
            Radars
          </Link>{" "}
          to get recommendations.
        </Empty>
      </>
    );
  }
  return (
    <>
      <PageHeader
        title="For You"
        subtitle={current.description || current.config?.research_question || undefined}
        actions={<ButtonLink href={`/radars/${encodeURIComponent(current.radar_id)}`}>Radar and history</ButtonLink>}
      />
      {withRuns.length > 1 && (
        <div role="tablist" aria-label="Radars" className="mb-4 flex flex-wrap gap-2">
          {withRuns.map((r) => (
            <button
              key={r.radar_id}
              role="tab"
              aria-selected={r.radar_id === selected}
              onClick={() => router.push(`/?radar=${encodeURIComponent(r.radar_id)}`)}
              className={`rounded-full border px-3 py-1 text-sm ${
                r.radar_id === selected ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:text-text"
              }`}
            >
              {r.title}
            </button>
          ))}
        </div>
      )}
      {withRuns.length === 1 && <h2 className="mb-3 font-medium">{current.title}</h2>}
      <Feed key={`${selected}:${runId ?? ""}`} radarId={current.radar_id} runId={runId} />
    </>
  );
}

export default function Page() {
  return (
    <Suspense fallback={<Loading />}>
      <ForYou />
    </Suspense>
  );
}
