// Which genus a precise-observation cluster badge shows (issue #449): the most common genus among
// the cluster's pins. A tie goes to the genus with more pins loaded overall (the more observed
// genus in this destination), then to the alphabetically first name, so a badge never flickers
// between equals. Child clusters re-vote as the cluster splits on zoom, so badges sharpen from
// "mostly Trametes" toward each pin's own genus as you zoom in.

import type { PreciseObservation } from "../api/types";

export interface ClusterVote {
  taxonId: number;
  name: string;
  icon: PreciseObservation["icon"];
  /** Pins of the winning genus in this cluster. */
  count: number;
}

export function voteGenus(
  observations: readonly PreciseObservation[],
  overall: ReadonlyMap<number, number>,
): ClusterVote | null {
  const tally = new Map<number, { obs: PreciseObservation; count: number }>();
  for (const obs of observations) {
    const entry = tally.get(obs.taxon_id);
    if (entry) entry.count += 1;
    else tally.set(obs.taxon_id, { obs, count: 1 });
  }
  let best: { obs: PreciseObservation; count: number } | null = null;
  for (const entry of tally.values()) {
    if (!best || beats(entry, best, overall)) best = entry;
  }
  return best
    ? { taxonId: best.obs.taxon_id, name: best.obs.name, icon: best.obs.icon, count: best.count }
    : null;
}

function beats(
  candidate: { obs: PreciseObservation; count: number },
  current: { obs: PreciseObservation; count: number },
  overall: ReadonlyMap<number, number>,
): boolean {
  if (candidate.count !== current.count) return candidate.count > current.count;
  const candidateOverall = overall.get(candidate.obs.taxon_id) ?? 0;
  const currentOverall = overall.get(current.obs.taxon_id) ?? 0;
  if (candidateOverall !== currentOverall) return candidateOverall > currentOverall;
  return candidate.obs.name.localeCompare(current.obs.name) < 0;
}

/** Pins per genus across everything loaded - the tie-break for `voteGenus`. */
export function countByGenus(observations: Iterable<PreciseObservation>): Map<number, number> {
  const counts = new Map<number, number>();
  for (const obs of observations) counts.set(obs.taxon_id, (counts.get(obs.taxon_id) ?? 0) + 1);
  return counts;
}
