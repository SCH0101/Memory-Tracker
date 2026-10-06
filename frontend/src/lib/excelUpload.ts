import { api, type ExcelIngestResult } from "./api";

function brokerPromptText(filename: string, knownBrokers: string[]): string {
  const examples = knownBrokers.slice(0, 10).join(", ");
  return [
    `Broker name for "${filename}"?`,
    examples ? `Known examples: ${examples}` : "Use the broker name shown in Equity Swarm.",
  ].join("\n");
}

export async function uploadExcelWithBrokerPrompt(file: File): Promise<ExcelIngestResult> {
  const resolved = await api.resolveExcelBroker(file.name);
  let broker = resolved.broker;
  if (resolved.needs_broker) {
    const answer = window.prompt(brokerPromptText(file.name, resolved.known_brokers || []), "");
    broker = (answer || "").trim();
    if (!broker) {
      return {
        ok: false,
        status: "needs_broker",
        needs_broker: true,
        filename: file.name,
        message: "Upload cancelled: broker name is required.",
      };
    }
  }
  return api.uploadExcel(file, { broker });
}
