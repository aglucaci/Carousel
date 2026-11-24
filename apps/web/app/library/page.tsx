"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { PaperMeta, PaperTitle } from "@/components/PaperCard";
import { WhyDrawer } from "@/components/WhyDrawer";
import { Button, Empty, ErrorNote, Loading, PageHeader, fmtDate } from "@/components/ui";
import { client, useApi, type LibraryItem } from "@/lib/api";

export default function LibraryPage() {
  const { data, error, loading, setData } = useApi(() => client.library(), []);
  const [radar, setRadar] = useState("");
  const [why, setWhy] = useState<LibraryItem | undefined>();
  const [actionError, setActionError] = useState<Error | undefined>();

  const radars = useMemo(() => {
    const m = new Map<string, string>();
    (data ?? []).forEach((x) => m.set(x.radar_id, x.radar_title ?? x.radar_id));
    return [...m.entries()];
  }, [data]);
  const shown = (data ?? []).filter((x) => !radar || x.radar_id === radar);

  async function unsave(item: LibraryItem) {
    setActionError(undefined);
    try {
      await client.feedback(item.radar_id, item.paper_id, "save", true);
      setData((d) => d?.filter((x) => !(x.radar_id === item.radar_id && x.paper_id === item.paper_id)));
    } catch (e) {
      setActionError(e as Error);
    }
  }

  return (
    <>
      <PageHeader
        title="Library"
        subtitle="Papers you saved. They stay here across scans."
        actions={
          radars.length > 1 && (
            <label className="text-sm">
              <span className="sr-only">Radar</span>
              <select
                value={radar}
                onChange={(e) => setRadar(e.target.value)}
                className="rounded-md border border-border bg-surface px-2 py-1.5"
              >
                <option value="">All radars</option>
                {radars.map(([id, title]) => (
                  <option key={id} value={id}>
                    {title}
                  </option>
                ))}
              </select>
            </label>
          )
        }
      />
      <ErrorNote error={error ?? actionError} />
      {loading && !data ? (
        <Loading />
      ) : shown.length === 0 ? (
        <Empty title="Nothing saved yet">
          Save papers from{" "}
          <Link href="/" className="text-accent hover:underline">
            For You
          </Link>{" "}
          to keep them here.
        </Empty>
      ) : (
        <ul className="space-y-3" aria-label="Saved papers">
          {shown.map((x) => (
            <li key={`${x.radar_id}:${x.paper_id}`} data-testid="library-item" className="rounded-lg border border-border bg-surface p-4">
              <PaperTitle paper={x.paper} />
              <PaperMeta paper={x.paper} />
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs text-muted">
                  Saved from {x.radar_title ?? x.radar_id} on {fmtDate(x.saved_at)}
                  {x.paper.doi && (
                    <>
                      {" "}
                      · DOI{" "}
                      <a className="hover:underline" href={`https://doi.org/${x.paper.doi}`} target="_blank" rel="noreferrer">
                        {x.paper.doi}
                      </a>
                    </>
                  )}
                </p>
                <div className="flex gap-2">
                  <Button variant="ghost" onClick={() => setWhy(x)}>
                    Why this paper?
                  </Button>
                  <Button onClick={() => unsave(x)}>Remove</Button>
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {why && <WhyDrawer paperId={why.paper_id} radarId={why.radar_id} onClose={() => setWhy(undefined)} />}
    </>
  );
}
