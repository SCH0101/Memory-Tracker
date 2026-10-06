import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

/**
 * Tracks whether new chunks have landed in the shared fact pool since the
 * user last viewed this tab. Persists the baseline to localStorage so a tab
 * switch / hard refresh doesn't reset the comparison.
 *
 * Returns:
 *   - corpusPoints: current total chunks in Qdrant (across all tickers)
 *   - baselinePoints: the value the user has "seen" / refreshed against
 *   - newChunks: max(0, corpusPoints - baselinePoints)
 *   - acknowledge(): call when the user clicks Refresh — snapshots the
 *     current count into localStorage so the banner goes away
 */
export function useCorpusBaseline(tabKey: string) {
  const storageKey = `corpusBaseline:${tabKey}`;
  const { data } = useQuery({
    queryKey: ["corpusStats"],
    queryFn: api.corpusStats,
    refetchInterval: 60_000,
    staleTime: 30_000,
  });
  const corpusPoints = data?.points ?? 0;

  const [baseline, setBaseline] = useState<number>(() => {
    try {
      const stored = localStorage.getItem(storageKey);
      return stored ? Number(stored) : 0;
    } catch {
      return 0;
    }
  });

  // On first arrival of real corpus data, if we have no baseline yet, seed
  // it to the current count (so we don't flash "N new" on a fresh install).
  useEffect(() => {
    if (corpusPoints > 0 && baseline === 0) {
      setBaseline(corpusPoints);
      try {
        localStorage.setItem(storageKey, String(corpusPoints));
      } catch {
        /* localStorage unavailable — banner just won't persist */
      }
    }
  }, [corpusPoints, baseline, storageKey]);

  const acknowledge = () => {
    setBaseline(corpusPoints);
    try {
      localStorage.setItem(storageKey, String(corpusPoints));
    } catch {
      /* ignore */
    }
  };

  const newChunks = Math.max(0, corpusPoints - baseline);
  return { corpusPoints, baselinePoints: baseline, newChunks, acknowledge };
}
