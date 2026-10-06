import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useUploadPdf } from "../hooks/useCharts";
import { uploadExcelWithBrokerPrompt } from "../lib/excelUpload";

interface Props {
  onUploadComplete: (jobIds: string[]) => void;
}

export function UploadZone({ onUploadComplete }: Props) {
  const fileRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const [dragging, setDragging] = useState(false);
  const [excelBusy, setExcelBusy] = useState(false);
  const [message, setMessage] = useState("");
  const upload = useUploadPdf();
  const busy = upload.isPending || excelBusy;

  const handleFiles = useCallback(
    async (files: File[]) => {
      const pdfs = files.filter((f) => f.name.toLowerCase().endsWith(".pdf"));
      const excels = files.filter((f) => /\.(xlsx|xlsm)$/i.test(f.name));
      const unsupported = files.filter((f) => !/\.(pdf|xlsx|xlsm)$/i.test(f.name));
      if (pdfs.length === 0 && excels.length === 0) {
        if (unsupported.length) setMessage("Only PDF, XLSX, and XLSM files are supported.");
        return;
      }
      setMessage("");
      try {
        if (pdfs.length > 0) {
          const results = await upload.mutateAsync(pdfs);
          onUploadComplete(results.map((r) => r.job_id));
        }
        if (excels.length > 0) {
          setExcelBusy(true);
          const notes: string[] = [];
          for (const file of excels) {
            const res = await uploadExcelWithBrokerPrompt(file);
            if (res.needs_broker) {
              notes.push(res.message || `Broker needed for ${file.name}`);
            } else if (res.skipped) {
              notes.push(`Already ingested ${file.name}`);
            } else if (res.status === "needs_template") {
              notes.push(`Template needed for ${file.name}`);
            } else {
              notes.push(`Excel ingested for ${res.broker || file.name}`);
            }
          }
          setMessage(notes.join(" | "));
          qc.invalidateQueries({ queryKey: ["dashboard"] });
          qc.invalidateQueries({ queryKey: ["model"] });
        }
        if (unsupported.length) {
          setMessage((prev) =>
            [prev, `Skipped unsupported: ${unsupported.map((f) => f.name).join(", ")}`].filter(Boolean).join(" | "),
          );
        }
      } catch (err) {
        setMessage(`Upload failed: ${String(err)}`);
      } finally {
        setExcelBusy(false);
      }
    },
    [onUploadComplete, qc, upload]
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      const files = Array.from(e.dataTransfer.files);
      handleFiles(files);
    },
    [handleFiles]
  );

  return (
    <div
      className={`relative flex items-center gap-3 rounded-lg border-2 border-dashed px-4 py-2 transition-colors cursor-pointer ${
        dragging
          ? "border-primary bg-primary/5"
          : "border-muted-foreground/25 hover:border-primary/50"
      }`}
      onDragOver={(e) => {
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={onDrop}
      onClick={() => fileRef.current?.click()}
    >
      <input
        ref={fileRef}
        type="file"
        accept=".pdf,.xlsx,.xlsm"
        multiple
        className="hidden"
        onChange={(e) => {
          if (e.target.files) handleFiles(Array.from(e.target.files));
          e.target.value = "";
        }}
      />
      <svg
        className="h-5 w-5 text-muted-foreground"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={2}
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M12 16V4m0 0l-4 4m4-4l4 4M4 20h16"
        />
      </svg>
      <span className="text-sm font-medium">
        {busy ? "Uploading..." : "Upload File"}
      </span>
      {upload.isError && (
        <span className="text-xs text-destructive">Upload failed</span>
      )}
      {message && !busy && (
        <span className="max-w-[320px] truncate text-xs text-muted-foreground">{message}</span>
      )}
    </div>
  );
}
