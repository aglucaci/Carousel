"use client";

import { useMemo, useRef } from "react";
import type { ExploreGraph, ExploreLink, ExploreNode } from "@/lib/api";
import { fmtDate } from "./ui";

const W = 640;
const H = 560;
const CX = W / 2;
const CY = H / 2;
const R = 200;
const S = 10; // node half-size

const ROLE_NAMES: Record<string, string> = {
  seed: "seed paper",
  saved: "saved",
  relevant: "marked relevant",
  recommended: "recommended",
};

export function roleText(n: ExploreNode) {
  return n.roles.map((r) => (r === "recommended" && n.rank != null ? `recommended #${n.rank}` : ROLE_NAMES[r] ?? r)).join(", ");
}

export function shortTitle(n?: ExploreNode, len = 70) {
  const t = n?.title ?? n?.paper_id ?? "";
  return t.length > len ? `${t.slice(0, len - 1)}…` : t;
}

/** Seeds are squares, saved or relevant papers diamonds, the run's other papers circles; hollow when no references were read. */
function Shape({ n, x, y }: { n: ExploreNode; x: number; y: number }) {
  const common = {
    stroke: "currentColor",
    strokeWidth: 1.5,
    strokeDasharray: n.citation_data ? undefined : "2 2",
    fill: n.citation_data ? "var(--color-surface, white)" : "none",
  };
  if (n.roles.includes("seed")) return <rect x={x - S} y={y - S} width={2 * S} height={2 * S} {...common} />;
  if (n.roles.includes("saved") || n.roles.includes("relevant"))
    return <path d={`M${x},${y - S - 2} L${x + S + 2},${y} L${x},${y + S + 2} L${x - S - 2},${y} Z`} {...common} />;
  return <circle cx={x} cy={y} r={S} {...common} />;
}

/** A link in words, from ``of``'s side. */
export function describe(l: ExploreLink, of: string, other?: ExploreNode) {
  const who = `“${shortTitle(other)}” (${other ? roleText(other) : "?"})`;
  if (l.kind === "shared_references") return `shares ${l.shared} references with ${who}`;
  const when = l.observed_at ? `; link first seen ${fmtDate(l.observed_at)}` : "";
  return l.source === of ? `cites ${who}${when}` : `is cited by ${who}${when}`;
}

/**
 * The graph: papers on a circle, grouped by topic (labelled), citations as arrows (citing → cited) and shared
 * references as dashed lines. Shapes and line styles carry the meaning, so it reads without colour. Each paper is a
 * button: Tab or the arrow keys move between papers, Enter or Space selects one and shows how it is connected.
 */
export function LiteratureGraph({
  graph,
  selected,
  onSelect,
}: {
  graph: ExploreGraph;
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const refs = useRef<(SVGGElement | null)[]>([]);
  const layout = useMemo(() => {
    // one empty slot between topic groups, so groups read as arcs
    const slots: (ExploreNode | null)[] = [];
    graph.nodes.forEach((n, i) => {
      if (i > 0 && (graph.nodes[i - 1].topic_id ?? null) !== (n.topic_id ?? null)) slots.push(null);
      slots.push(n);
    });
    const pos: Record<string, { x: number; y: number; a: number }> = {};
    slots.forEach((n, i) => {
      if (!n) return;
      const a = (2 * Math.PI * i) / slots.length - Math.PI / 2;
      pos[n.paper_id] = { x: CX + R * Math.cos(a), y: CY + R * Math.sin(a), a };
    });
    const groups = graph.topics.map((t) => {
      const members = graph.nodes.filter((n) => (n.topic_id ?? null) === t.topic_id).map((n) => pos[n.paper_id].a);
      const a = members.reduce((s, v) => s + v, 0) / Math.max(1, members.length);
      return { ...t, x: CX + (R + 44) * Math.cos(a), y: CY + (R + 44) * Math.sin(a), a };
    });
    return { pos, groups };
  }, [graph]);
  const touching = (l: ExploreLink) => selected != null && (l.source === selected || l.target === selected);
  const neighbours = new Set(graph.links.filter(touching).flatMap((l) => [l.source, l.target]));
  const summary = `${graph.nodes.length} papers and ${graph.stats.citation_links ?? 0} citation links, ${graph.stats.shared_reference_links ?? 0} shared-reference links, in ${graph.topics.length} topic groups.`;

  const focus = (i: number) => refs.current[(i + graph.nodes.length) % graph.nodes.length]?.focus();
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="group" aria-label={summary} data-testid="graph">
      <title>{summary}</title>
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX={10} refY={5} markerWidth={7} markerHeight={7} orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="currentColor" />
        </marker>
      </defs>
      {layout.groups.map((g) => (
        <text
          key={g.topic_id ?? "none"}
          x={g.x}
          y={g.y}
          fontSize={12}
          textAnchor={Math.cos(g.a) > 0.3 ? "start" : Math.cos(g.a) < -0.3 ? "end" : "middle"}
          fill="currentColor"
          opacity={0.75}
          data-testid="graph-topic"
        >
          {g.label ?? "no topic"} ({g.papers})
        </text>
      ))}
      {graph.links.map((l) => {
        const a = layout.pos[l.source];
        const b = layout.pos[l.target];
        if (!a || !b) return null;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.hypot(dx, dy) || 1;
        const end = l.kind === "citation" ? { x: b.x - (dx / d) * (S + 3), y: b.y - (dy / d) * (S + 3) } : b;
        const on = touching(l);
        return (
          <line
            key={`${l.kind}:${l.source}:${l.target}`}
            data-testid="graph-link"
            data-kind={l.kind}
            data-source={l.source}
            data-target={l.target}
            x1={a.x}
            y1={a.y}
            x2={end.x}
            y2={end.y}
            stroke="currentColor"
            strokeWidth={on ? 2.5 : 1}
            strokeDasharray={l.kind === "shared_references" ? "5 4" : undefined}
            opacity={selected == null ? 0.45 : on ? 1 : 0.1}
            markerEnd={l.kind === "citation" ? "url(#arrow)" : undefined}
          />
        );
      })}
      {graph.nodes.map((n, i) => {
        const p = layout.pos[n.paper_id];
        const dim = selected != null && n.paper_id !== selected && !neighbours.has(n.paper_id);
        const label = `${shortTitle(n, 120)}: ${roleText(n)}; ${n.topic_label ? `topic ${n.topic_label}` : "no topic"}; ${n.links} link${n.links === 1 ? "" : "s"}${n.citation_data ? "" : "; references not read"}`;
        return (
          <g
            key={n.paper_id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            role="button"
            tabIndex={0}
            aria-label={label}
            aria-pressed={n.paper_id === selected}
            data-testid="graph-node"
            data-paper={n.paper_id}
            className="cursor-pointer outline-none [&:focus-visible>.ring]:opacity-100"
            opacity={dim ? 0.35 : 1}
            onClick={() => onSelect(n.paper_id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                onSelect(n.paper_id);
              } else if (e.key === "ArrowRight" || e.key === "ArrowDown") {
                e.preventDefault();
                focus(i + 1);
              } else if (e.key === "ArrowLeft" || e.key === "ArrowUp") {
                e.preventDefault();
                focus(i - 1);
              }
            }}
          >
            <title>{label}</title>
            <circle className="ring" cx={p.x} cy={p.y} r={S + 6} fill="none" stroke="currentColor" strokeWidth={1.5} opacity={0} />
            {n.paper_id === selected && <circle cx={p.x} cy={p.y} r={S + 6} fill="none" stroke="currentColor" strokeWidth={2} />}
            <Shape n={n} x={p.x} y={p.y} />
            {n.rank != null && (
              <text x={p.x} y={p.y + 3.5} fontSize={10} textAnchor="middle" fill="currentColor">
                {n.rank}
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}
