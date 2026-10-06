import { ChartCard } from "./ChartCard";
import type { ChartRecord } from "../lib/api";

interface Props {
  charts: ChartRecord[];
  onSelect: (chart: ChartRecord) => void;
}

export function ChartGrid({ charts, onSelect }: Props) {
  return (
    <div className="flex gap-3 overflow-x-auto pb-2 scrollbar-thin">
      {charts.map((chart) => (
        <ChartCard key={chart.id} chart={chart} onClick={() => onSelect(chart)} />
      ))}
    </div>
  );
}
