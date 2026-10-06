"""tools/pdf_chart_extractor.py — chart-region cropper for sellside PDFs.

Extracts chart/graph regions from PDFs as JPG images using PyMuPDF.
Copied from equity swarm and adapted for Memory Chart project.

Approach (pure crop-and-save, no chart re-rendering):
  1. Collect bounding boxes of vector drawing paths via page.get_drawings().
  2. Collect bounding boxes of embedded raster images.
  3. Cluster nearby/overlapping boxes into regions.
  4. Filter clusters too small or too sparse to be a chart.
  5. Classify each region (line_chart / bar_chart / table / image / etc.).
  6. Rasterize surviving regions at chosen DPI and save as JPG.

Public API:
    extract_charts_from_pdf(pdf_path, output_dir, ...) → list[ExtractedChart]
    extract_charts_from_bytes(pdf_bytes, output_dir, ...) → list[ExtractedChart]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF


REGION_TYPES = ("line_chart", "bar_chart", "scatter_chart", "table",
                "chart", "image", "logo")


def _looks_like_logo(cluster_rect: fitz.Rect, page_rect: fitz.Rect,
                     path_count: int) -> bool:
    w, h = cluster_rect.width, cluster_rect.height
    if w <= 0 or h <= 0:
        return False
    if path_count >= 5:
        return False
    is_small = (w < 180 and h < 120)
    aspect = w / h
    is_extreme_aspect = (aspect > 4.5 or aspect < 0.25)
    page_h = page_rect.height or 792
    in_header = cluster_rect.y0 < page_rect.y0 + page_h * 0.10
    in_footer = cluster_rect.y1 > page_rect.y0 + page_h * 0.90
    in_band = (in_header or in_footer) and (w < 250 or h < 150)
    return is_small or is_extreme_aspect or in_band


@dataclass
class ExtractedChart:
    page: int
    index: int
    bbox: fitz.Rect
    path: Path
    path_count: int
    has_image: bool
    region_type: str


def _classify_region(
    all_drawings: list[dict],
    cluster_rect: fitz.Rect,
    has_image: bool,
) -> str:
    if has_image:
        hit_count = 0
        for d in all_drawings:
            r = d.get("rect")
            if r and cluster_rect.intersects(fitz.Rect(r)):
                hit_count += 1
        if hit_count < 10:
            return "image"

    h_lines = 0
    v_lines = 0
    diag_lines = 0
    curves = 0
    filled_rects = 0
    stroked_rects = 0
    total_items = 0
    h_line_ys: list[float] = []
    v_line_xs: list[float] = []
    filled_rect_widths: list[float] = []

    for d in all_drawings:
        r = d.get("rect")
        if not r:
            continue
        drect = fitz.Rect(r)
        if not cluster_rect.intersects(drect):
            continue
        has_fill = d.get("fill") is not None
        for item in d.get("items", []):
            total_items += 1
            op = item[0]
            if op == "l":
                p1, p2 = item[1], item[2]
                dx = abs(p2.x - p1.x)
                dy = abs(p2.y - p1.y)
                if dy < 2.0:
                    h_lines += 1
                    h_line_ys.append(round(p1.y, 1))
                elif dx < 2.0:
                    v_lines += 1
                    v_line_xs.append(round(p1.x, 1))
                else:
                    diag_lines += 1
            elif op == "c":
                curves += 1
            elif op == "re":
                rect_item = fitz.Rect(item[1])
                if has_fill and rect_item.width > 2 and rect_item.height > 2:
                    filled_rects += 1
                    filled_rect_widths.append(rect_item.width)
                else:
                    stroked_rects += 1
            elif op == "qu":
                if has_fill:
                    filled_rects += 1
                else:
                    stroked_rects += 1

    if total_items == 0:
        return "image" if has_image else "chart"

    hv_lines = h_lines + v_lines
    unique_h_ys = len(set(h_line_ys))
    unique_v_xs = len(set(v_line_xs))
    cluster_w = cluster_rect.width

    wide_fills = 0
    narrow_fills = 0
    for w in filled_rect_widths:
        if w > cluster_w * 0.3:
            wide_fills += 1
        else:
            narrow_fills += 1

    rect_dominant = (stroked_rects >= 15 and curves <= 2 and diag_lines <= 1
                     and narrow_fills <= 2)
    line_grid = (hv_lines >= 15 and unique_h_ys >= 5 and unique_v_xs >= 3
                 and diag_lines <= 1 and curves <= 2 and narrow_fills <= 2)
    if rect_dominant or line_grid:
        return "table"

    if narrow_fills >= 3:
        if filled_rect_widths:
            bar_widths = [w for w in filled_rect_widths if w <= cluster_w * 0.3]
            if bar_widths:
                avg_w = sum(bar_widths) / len(bar_widths)
                similar = sum(1 for w in bar_widths if abs(w - avg_w) < avg_w * 0.5)
                if similar / len(bar_widths) > 0.5:
                    return "bar_chart"
        if narrow_fills / max(total_items, 1) > 0.08:
            return "bar_chart"

    if curves >= 4 and curves > diag_lines and curves > h_lines:
        return "scatter_chart"

    if diag_lines >= 2 or (curves >= 2 and hv_lines >= 1):
        return "line_chart"

    if total_items >= 6:
        return "chart"

    return "image" if has_image else "chart"


def _cluster_boxes(
    boxes: list[tuple[fitz.Rect, bool]],
    *,
    gap: float,
) -> list[tuple[fitz.Rect, int, bool]]:
    remaining = list(boxes)
    clusters: list[tuple[fitz.Rect, int, bool]] = []

    while remaining:
        seed_rect, seed_is_img = remaining.pop(0)
        merged = fitz.Rect(seed_rect)
        count = 1
        has_img = seed_is_img
        changed = True
        while changed:
            changed = False
            next_remaining = []
            for rect, is_img in remaining:
                inflated = fitz.Rect(
                    merged.x0 - gap, merged.y0 - gap,
                    merged.x1 + gap, merged.y1 + gap,
                )
                if inflated.intersects(rect):
                    merged |= rect
                    count += 1
                    has_img = has_img or is_img
                    changed = True
                else:
                    next_remaining.append((rect, is_img))
            remaining = next_remaining
        clusters.append((merged, count, has_img))

    # Pass 2: merge overlapping clusters
    merged_again = True
    while merged_again:
        merged_again = False
        new_clusters = []
        used = set()
        for i, (r1, c1, img1) in enumerate(clusters):
            if i in used:
                continue
            for j in range(i + 1, len(clusters)):
                if j in used:
                    continue
                r2, c2, img2 = clusters[j]
                if r1.intersects(r2):
                    r1 = r1 | r2
                    c1 += c2
                    img1 = img1 or img2
                    used.add(j)
                    merged_again = True
            new_clusters.append((r1, c1, img1))
            used.add(i)
        clusters = new_clusters

    return clusters


def _extract_from_doc(
    doc: fitz.Document,
    output_dir: Path,
    *,
    source_stem: str,
    dpi: int = 200,
    margin_pts: float = 10.0,
    upper_extra_pixels: int = 24,
    cluster_gap_pts: float = 28.0,
    min_paths: int = 10,
    min_width_pts: float = 65.0,
    min_height_pts: float = 50.0,
    jpg_quality: int = 90,
    include_embedded_images: bool = True,
) -> list[ExtractedChart]:
    results: list[ExtractedChart] = []

    for page_index, page in enumerate(doc):
        page_num = page_index + 1
        page_rect = page.rect
        all_drawings = page.get_drawings()
        boxes: list[tuple[fitz.Rect, bool]] = []

        for d in all_drawings:
            r = d.get("rect")
            if not r:
                continue
            rect = fitz.Rect(r)
            if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                continue
            boxes.append((rect, False))

        if include_embedded_images:
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    for bbox in page.get_image_rects(xref):
                        rect = fitz.Rect(bbox)
                        if rect.is_empty:
                            continue
                        boxes.append((rect, True))
                except Exception:
                    continue

        if not boxes:
            continue

        clusters = _cluster_boxes(boxes, gap=cluster_gap_pts)

        kept = 0
        for cluster_rect, path_count, has_image in clusters:
            if cluster_rect.width < min_width_pts or cluster_rect.height < min_height_pts:
                continue
            if not has_image and path_count < min_paths:
                continue

            region_type = _classify_region(all_drawings, cluster_rect, has_image)
            if region_type in ("table", "image", "scatter_chart", "logo"):
                continue

            if _looks_like_logo(cluster_rect, page_rect, path_count):
                continue

            upper_extra_pts = upper_extra_pixels * 72.0 / float(dpi or 72)
            expanded = fitz.Rect(
                max(cluster_rect.x0 - margin_pts, page_rect.x0),
                max(cluster_rect.y0 - margin_pts - upper_extra_pts, page_rect.y0),
                min(cluster_rect.x1 + margin_pts, page_rect.x1),
                min(cluster_rect.y1 + margin_pts, page_rect.y1),
            )

            kept += 1
            zoom = dpi / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, clip=expanded, alpha=False)

            safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", source_stem)[:60] or "doc"
            out_path = output_dir / f"{safe_stem}_p{page_num:03d}_{region_type}_{kept:02d}.jpg"
            pix.save(str(out_path), jpg_quality=jpg_quality)

            results.append(ExtractedChart(
                page=page_num,
                index=kept,
                bbox=cluster_rect,
                path=out_path,
                path_count=path_count,
                has_image=has_image,
                region_type=region_type,
            ))

    return results


def extract_charts_from_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    dpi: int = 200,
    margin_pts: float = 10.0,
    upper_extra_pixels: int = 24,
    cluster_gap_pts: float = 28.0,
    min_paths: int = 10,
    min_width_pts: float = 65.0,
    min_height_pts: float = 50.0,
    jpg_quality: int = 90,
    include_embedded_images: bool = True,
) -> list[ExtractedChart]:
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    try:
        return _extract_from_doc(
            doc, output_dir,
            source_stem=pdf_path.stem,
            dpi=dpi, margin_pts=margin_pts,
            upper_extra_pixels=upper_extra_pixels,
            cluster_gap_pts=cluster_gap_pts,
            min_paths=min_paths,
            min_width_pts=min_width_pts,
            min_height_pts=min_height_pts,
            jpg_quality=jpg_quality,
            include_embedded_images=include_embedded_images,
        )
    finally:
        doc.close()


def extract_charts_from_bytes(
    pdf_bytes: bytes,
    output_dir: str | Path,
    *,
    source_name: str = "doc",
    dpi: int = 200,
    margin_pts: float = 10.0,
    upper_extra_pixels: int = 24,
    cluster_gap_pts: float = 28.0,
    min_paths: int = 10,
    min_width_pts: float = 65.0,
    min_height_pts: float = 50.0,
    jpg_quality: int = 90,
    include_embedded_images: bool = True,
) -> list[ExtractedChart]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return _extract_from_doc(
            doc, output_dir,
            source_stem=source_name,
            dpi=dpi, margin_pts=margin_pts,
            upper_extra_pixels=upper_extra_pixels,
            cluster_gap_pts=cluster_gap_pts,
            min_paths=min_paths,
            min_width_pts=min_width_pts,
            min_height_pts=min_height_pts,
            jpg_quality=jpg_quality,
            include_embedded_images=include_embedded_images,
        )
    finally:
        doc.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract charts from a PDF")
    parser.add_argument("pdf", help="Path to the input PDF")
    parser.add_argument("-o", "--output", default="./chart_output",
                        help="Output directory for JPGs")
    parser.add_argument("--dpi", type=int, default=200)
    args = parser.parse_args()

    charts = extract_charts_from_pdf(args.pdf, args.output, dpi=args.dpi)
    for c in charts:
        print(f"  Page {c.page}, {c.region_type}: {c.path}")
    print(f"Extracted {len(charts)} chart(s).")
