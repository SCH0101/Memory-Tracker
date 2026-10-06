import { useQuery, useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";

const galleryQueryKey = ["charts", "by-category"] as const;

export async function refreshGalleryAfterLocalChange(qc: QueryClient) {
  await qc.invalidateQueries({ queryKey: ["charts"], refetchType: "none" });
  return qc.fetchQuery({
    queryKey: galleryQueryKey,
    queryFn: () => api.getChartsByCategory(false, true),
    staleTime: 0,
  });
}

export function useChartsByCategory() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: galleryQueryKey,
    queryFn: () => api.getChartsByCategory(),
    staleTime: 300_000,
    gcTime: 1_800_000,
    refetchOnWindowFocus: false,
  });
  const refreshFromSource = () => qc.fetchQuery({
    queryKey: galleryQueryKey,
    queryFn: () => api.getChartsByCategory(true),
    staleTime: 0,
  });
  return { ...query, refreshFromSource };
}

export function useCategories() {
  return useQuery({
    queryKey: ["categories"],
    queryFn: api.getCategories,
  });
}

export function useBrokers() {
  return useQuery({
    queryKey: ["brokers"],
    queryFn: api.getBrokers,
  });
}

export function useUploadPdf() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (files: File[]) => api.uploadPdfs(files),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["charts"] });
      qc.invalidateQueries({ queryKey: ["categories"] });
    },
  });
}

export function useUpdateChart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: number } & Record<string, unknown>) =>
      api.updateChart(id, body),
    onSuccess: async () => {
      await Promise.all([
        refreshGalleryAfterLocalChange(qc),
        qc.invalidateQueries({ queryKey: ["categories"] }),
      ]);
    },
  });
}

export function useDeleteChart() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => api.deleteChart(id),
    onSuccess: async () => {
      await Promise.all([
        refreshGalleryAfterLocalChange(qc),
        qc.invalidateQueries({ queryKey: ["categories"] }),
      ]);
    },
  });
}
