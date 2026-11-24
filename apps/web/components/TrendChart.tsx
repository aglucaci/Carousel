"use client";

import type { TrendTopic } from "@/lib/api";

const W = 560;
const H = 170;
const PAD = { l: 44, r: 12, t: 10, b: 26 };

function pct(x: number, digits = 1) {
  return `${(100 * x).toFixed(digits)}%`;
}

/**
 * A topic's share of its field per week: observed shares (filled circles; hollow while a week is provisional),
 * the fitted baseline (dashed line, crosses) inside its 95% band, and the provisional weeks shaded. Shapes and line
 * styles carry the meaning, so it reads without colour; the same numbers are in the table view.
 */
export function TrendChart({ topic }: { topic: TrendTopic }) {
  const weeks = topic.weekly;
  if (weeks.length < 2) return <p className="text-sm text-muted">Not enough weeks to draw.</p>;
  const base: Record<string, { share: number; low: number; high: number }> = Object.fromEntries(
    (topic.baseline_share ?? []).map((b) => [b.week, b]),
  );
  const ymax = Math.max(0.0001, ...weeks.map((w) => w.share ?? 0), ...(topic.baseline_share ?? []).map((b) => b.high)) * 1.1;
  const x = (i: number) => PAD.l + (i * (W - PAD.l - PAD.r)) / (weeks.length - 1);
  const y = (v: number) => H - PAD.b - (v / ymax) * (H - PAD.t - PAD.b);
  const bandIdx = weeks.map((w, i) => [w, i] as const).filter(([w]) => base[w.week]);
  const band = bandIdx.length
    ? [...bandIdx.map(([w, i]) => `${x(i)},${y(base[w.week].high)}`), ...[...bandIdx].reverse().map(([w, i]) => `${x(i)},${y(base[w.week].low)}`)].join(" ")
    : "";
  const line = bandIdx.map(([w, i], k) => `${k ? "L" : "M"}${x(i)},${y(base[w.week].share)}`).join(" ");
  const observed = weeks.map((w, i) => `${i ? "L" : "M"}${x(i)},${y(w.share ?? 0)}`).join(" ");
  const firstProv = weeks.findIndex((w) => w.provisional);
  const latest = weeks[weeks.length - 1];
  const summary = `${topic.label ?? topic.topic_id}: share ${pct(weeks[0].share ?? 0)} in the week of ${weeks[0].week}, ${pct(latest.share ?? 0)} in the week of ${latest.week}${topic.baseline_share?.length ? `; baseline ${pct(topic.baseline_share[0].share)}` : ""}.`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={summary} data-testid="trend-chart">
      <title>{summary}</title>
      {firstProv >= 0 && (
        <g data-testid="provisional-region">
          <rect x={x(firstProv) - 6} y={PAD.t} width={W - PAD.r - x(firstProv) + 6} height={H - PAD.t - PAD.b} fill="currentColor" opacity={0.06} />
          <text x={W - PAD.r - 2} y={PAD.t + 10} fontSize={10} textAnchor="end" fill="currentColor" opacity={0.7}>
            provisional →
          </text>
        </g>
      )}
      {band && <polygon points={band} fill="currentColor" opacity={0.12} data-testid="baseline-band" />}
      {line && <path d={line} fill="none" stroke="currentColor" strokeDasharray="5 4" strokeWidth={1.5} opacity={0.7} />}
      {bandIdx.map(([w, i]) => (
        <g key={`b${w.week}`} data-testid="baseline-point" data-week={w.week} data-share={base[w.week].share}>
          <path d={`M${x(i) - 3},${y(base[w.week].share) - 3} l6,6 m0,-6 l-6,6`} stroke="currentColor" strokeWidth={1} opacity={0.6} />
        </g>
      ))}
      <path d={observed} fill="none" stroke="var(--color-accent, currentColor)" strokeWidth={2} />
      {weeks.map((w, i) => (
        <circle
          key={w.week}
          data-testid="share-point"
          data-week={w.week}
          data-share={w.share ?? ""}
          data-provisional={w.provisional ? "true" : "false"}
          cx={x(i)}
          cy={y(w.share ?? 0)}
          r={3.5}
          fill={w.provisional ? "var(--color-surface, white)" : "var(--color-accent, currentColor)"}
          stroke="var(--color-accent, currentColor)"
          strokeWidth={1.5}
        >
          <title>{`${w.week}: ${w.count} of ${w.denominator} (${pct(w.share ?? 0)})${w.provisional ? ", provisional" : ""}`}</title>
        </circle>
      ))}
      <line x1={PAD.l} x2={W - PAD.r} y1={H - PAD.b} y2={H - PAD.b} stroke="currentColor" opacity={0.3} />
      <text x={PAD.l - 6} y={y(0) + 3} fontSize={10} textAnchor="end" fill="currentColor" opacity={0.7}>0%</text>
      <text x={PAD.l - 6} y={y(ymax / 1.1) + 3} fontSize={10} textAnchor="end" fill="currentColor" opacity={0.7}>
        {pct(ymax / 1.1, 0)}
      </text>
      <text x={x(0)} y={H - 8} fontSize={10} fill="currentColor" opacity={0.7}>{weeks[0].week}</text>
      <text x={x(weeks.length - 1)} y={H - 8} fontSize={10} textAnchor="end" fill="currentColor" opacity={0.7}>
        {latest.week}
      </text>
    </svg>
  );
}
