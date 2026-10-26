import { Cpu, ExternalLink, Github, Layers, Network } from "lucide-react";
import LoopDiagram from "../components/LoopDiagram";
import { Badge, Card, Loading, PageHeader } from "../components/ui";
import { usePoll } from "../hooks";

const TOOLS = [
  ["Grafana", "http://localhost:3000", "Dashboards for orders-service"],
  ["Prometheus", "http://localhost:9090", "Metrics and alert rules"],
  ["Alertmanager", "http://localhost:9093", "Routes alerts to DevNexa"],
  ["Jaeger", "http://localhost:16686", "Distributed traces"],
  ["API docs", "/docs", "Swagger UI for the DevNexa API"],
];

const STACK = [
  ["Backend / API", "Python, FastAPI, Uvicorn"], ["Agent orchestration", "LangGraph state graphs"],
  ["AI / NLP", "LLM (Anthropic / OpenAI-compatible / local) + offline evidence scorer, embeddings, RAG"],
  ["Database", "PostgreSQL"], ["Vector retrieval", "pgvector (cosine)"], ["Frontend", "React, Vite, Tailwind CSS, Recharts"],
  ["CI", "GitHub Actions + webhooks"], ["Containers", "Docker, Docker Compose"],
  ["Observability", "OpenTelemetry, Prometheus, Alertmanager, Grafana, Loki, Jaeger"], ["Testing", "pytest, Swagger UI"],
];

export default function System() {
  const { data: s } = usePoll("/api/system", 0);
  return (
    <>
      <PageHeader eyebrow="Platform" title="Architecture & configuration"
        description="How this DevNexa instance is wired: the five architecture layers, the runtime configuration, and the observability tools around it." />
      <Card title="Layered architecture" icon={Layers}><LoopDiagram /></Card>
      <div className="mt-6 grid gap-6 xl:grid-cols-3">
        <Card title="Runtime" icon={Cpu}>
          {!s ? <Loading /> : (
            <dl className="space-y-2.5 text-sm">
              {[["Version", s.version], ["Environment", s.environment_mode], ["Analyzer", s.analyzer], ["Database", s.database],
                ["Vector store", s.vector_store], ["Embedder", `${s.embedder} (${s.embedding_dim}d)`], ["RAG top-k", s.rag_top_k],
                ["Max re-analysis", s.max_reanalysis_attempts], ["GitHub webhook secret", s.integrations.github_webhook ? "configured" : "not set"]].map(([k, v]) => (
                <div key={k} className="flex justify-between gap-4"><dt className="text-slate-400">{k}</dt><dd className="text-right font-mono text-xs text-slate-200">{v}</dd></div>
              ))}
            </dl>
          )}
        </Card>
        <Card title="Observability tools" subtitle="Available when running the docker-compose stack" icon={Network}>
          <ul className="space-y-2">
            {TOOLS.map(([n, u, d]) => (
              <li key={n}><a href={u} target="_blank" rel="noreferrer" className="flex items-center justify-between rounded-lg px-2 py-1.5 hover:bg-white/[0.04]">
                <span><span className="block text-sm text-slate-200">{n}</span><span className="text-xs text-slate-500">{d}</span></span>
                <ExternalLink className="h-3.5 w-3.5 text-slate-500" /></a></li>
            ))}
          </ul>
        </Card>
        <Card title="Connect GitHub" icon={Github}>
          <ol className="list-decimal space-y-2 pl-4 text-sm text-slate-300">
            <li>Set <code className="text-indigo-300">DEVNEXA_GITHUB_WEBHOOK_SECRET</code> on the backend.</li>
            <li>Add a repository webhook to <code className="text-indigo-300">/api/webhooks/github</code> (push events, same secret).</li>
            <li>Or add <code className="text-indigo-300">DEVNEXA_URL</code> and <code className="text-indigo-300">DEVNEXA_TOKEN</code> secrets so the bundled GitHub Actions workflow reports real build/test stages and asks DevNexa which tests to run first.</li>
          </ol>
        </Card>
      </div>
      <Card className="mt-6" title="Technology stack" bodyClass="">
        <table className="w-full"><tbody className="divide-y divide-white/[0.04]">
          {STACK.map(([k, v]) => <tr key={k}><td className="table-cell w-56 text-slate-400">{k}</td><td className="table-cell text-slate-200">{v.split(", ").map((x) => <Badge key={x} className="mr-1 mb-1">{x}</Badge>)}</td></tr>)}
        </tbody></table>
      </Card>
    </>
  );
}
