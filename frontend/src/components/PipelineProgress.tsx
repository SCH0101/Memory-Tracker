import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

const STEP_LABELS: Record<string, string> = {
  queued: "Queued",
  extracting: "Extracting charts from PDF...",
  quality_check: "Checking chart quality...",
  tagging: "Tagging charts with LLM...",
  categorizing: "Categorizing charts...",
  done: "Complete",
  error: "Error",
};

interface Props {
  jobId: string;
  onDone: () => void;
}

export function PipelineProgress({ jobId, onDone }: Props) {
  const isEsRagJob = jobId.startsWith("esrag:");
  const { data: job } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.getJob(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (status === "done" || status === "error") return false;
      return 2000;
    },
  });

  useEffect(() => {
    if (job?.status === "done") {
      const t = setTimeout(onDone, 1500);
      return () => clearTimeout(t);
    }
  }, [job?.status, onDone]);

  if (!job) return null;

  const isDone = job.status === "done";
  const isError = job.status === "error";
  const statusLabel = isEsRagJob
    ? job.status === "extracting"
      ? "Indexing PDF in ES RAG..."
      : job.status === "done"
      ? "Indexed in ES RAG"
      : STEP_LABELS[job.status] || job.status
    : STEP_LABELS[job.status] || job.status;
  const pct =
    job.total_charts > 0
      ? Math.round((job.processed_charts / job.total_charts) * 100)
      : 0;

  return (
    <div
      className={`rounded-lg border px-4 py-3 text-sm ${
        isError
          ? "border-destructive/50 bg-destructive/5"
          : isDone
          ? "border-green-500/50 bg-green-50"
          : "border-primary/20 bg-primary/5"
      }`}
    >
      <div className="flex items-center justify-between mb-1">
        <span className="font-medium">{job.filename}</span>
        <span className="text-muted-foreground">
          {statusLabel}
        </span>
      </div>
      {!isDone && !isError && (
        <div className="h-1.5 rounded-full bg-muted overflow-hidden">
          <div
            className="h-full rounded-full bg-primary transition-all duration-500"
            style={{ width: `${Math.max(pct, 5)}%` }}
          />
        </div>
      )}
      {isDone && (
        <p className="text-green-700 text-xs">
          {isEsRagJob
            ? "PDF indexed by ES RAG"
            : `${job.processed_charts} chart${job.processed_charts !== 1 ? "s" : ""} extracted and categorized`}
        </p>
      )}
      {isError && (
        <p className="text-destructive text-xs">{job.error_message}</p>
      )}
    </div>
  );
}
