import type { ResearchEvent } from "@/lib/api";

/** What's New, grouped as the spec lists the change log (each group names the event types behind it). */
export const GROUPS: { key: string; title: string; help: string; types: ResearchEvent["type"][] }[] = [
  {
    key: "findings",
    title: "New findings",
    help: "Papers this radar had never seen, published in its window, with what they report (each finding quotes the paper).",
    types: ["new_publication"],
  },
  {
    key: "methods",
    title: "Emerging methods",
    help: "One of your radar's methods appearing in a topic's recent papers for the first time.",
    types: ["new_methodological_connection"],
  },
  {
    key: "growth",
    title: "Topic growth",
    help: "Topics whose share of your field rose beyond chance, after checks for indexing and counting artifacts.",
    types: ["topic_acceleration"],
  },
  {
    key: "connections",
    title: "New connections",
    help: "How your field's topics divide changed: topics joining, splitting, appearing, or fading.",
    types: ["research_landscape_shift"],
  },
  {
    key: "updates",
    title: "Publication updates",
    help: "Corrections, retractions, and new versions of papers this radar has shown you.",
    types: ["publication_revision"],
  },
];

export const KIND: Record<string, { label: string; tone: "accent" | "warn" | "bad" | "neutral" }> = {
  new_publication: { label: "New publication", tone: "accent" },
  topic_acceleration: { label: "Growing", tone: "accent" },
  new_methodological_connection: { label: "New method", tone: "accent" },
  split: { label: "Topic split", tone: "neutral" },
  merge: { label: "Topics joined", tone: "neutral" },
  new: { label: "New topic", tone: "neutral" },
  retired: { label: "Topic faded", tone: "neutral" },
  retraction: { label: "Retracted", tone: "bad" },
  removal: { label: "Removed", tone: "bad" },
  withdrawal: { label: "Withdrawn", tone: "bad" },
  expression_of_concern: { label: "Expression of concern", tone: "bad" },
  correction: { label: "Corrected", tone: "warn" },
  erratum: { label: "Erratum", tone: "warn" },
  partial_retraction: { label: "Partially retracted", tone: "bad" },
  new_version: { label: "New version linked", tone: "neutral" },
};

export function kindOf(e: ResearchEvent) {
  const k =
    e.type === "new_publication" || e.type === "topic_acceleration" || e.type === "new_methodological_connection"
      ? e.type
      : (e.kind ?? "revision");
  return KIND[k] ?? { label: k.replaceAll("_", " "), tone: "warn" as const };
}

export const WHO: Record<string, string> = { "retraction-watch": "Retraction Watch" };
