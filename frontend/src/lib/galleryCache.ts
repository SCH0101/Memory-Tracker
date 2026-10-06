export interface GalleryRequest {
  path: string;
  init: RequestInit | undefined;
}


export function galleryRequest(
  refresh = false,
  now = Date.now(),
  bypassBrowserCache = false,
): GalleryRequest {
  if (!refresh && !bypassBrowserCache) {
    return { path: "/api/charts/by-category", init: undefined };
  }
  const refreshParam = refresh ? "refresh=true&" : "";
  return {
    path: `/api/charts/by-category?${refreshParam}_=${now}`,
    init: { cache: "no-store" },
  };
}
