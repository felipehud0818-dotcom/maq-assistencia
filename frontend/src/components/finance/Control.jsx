import { useEffect, useState } from "react";
import { api, errMsg } from "@/components/finance/api";
import { brl } from "@/lib/format";
import { Card, SectionTitle, Empty, ExportButton, SimpleSelect } from "@/components/finance/common";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

const ENTITY = { transaction: "Lançamento", os_launch: "Lançamento de OS", payout: "Repasse", reconciliation: "Conciliação", settings: "Configuração" };
const ACTION = { created: "criou", updated: "alterou", deleted: "excluiu", replaced: "relançou", links: "mudou OS vinculadas", reconciled: "conferiu",
  unreconciled: "desfez conferência", reversed: "estornou", linked: "conciliou", created_tx: "criou pelo extrato", ignored: "ignorou" };
const LABEL = { amount: "Valor", date: "Data", status: "Status", description: "Descrição", category_id: "Categoria", payment_method: "Forma",
  type: "Tipo", os_ids: "OS", reconciled: "Conferido", fee_since: "Taxas a partir de", technician_id: "Técnico", bank_tx_id: "Movimento" };

const show = (k, v) => (v == null || v === "" ? "—" : k === "amount" ? brl(v) : Array.isArray(v) ? v.join(", ") : typeof v === "boolean" ? (v ? "Sim" : "Não") : String(v));

const diffOf = (b, a) => {
  if (Array.isArray(b) || Array.isArray(a)) return [`${(b || []).length} → ${(a || []).length} lançamento(s)`];
  const keys = [...new Set([...Object.keys(b || {}), ...Object.keys(a || {})])];
  const out = keys.filter((k) => JSON.stringify((b || {})[k]) !== JSON.stringify((a || {})[k]) && LABEL[k])
    .map((k) => `${LABEL[k]}: ${show(k, (b || {})[k])} → ${show(k, (a || {})[k])}`);
  return out.length ? out : (b && !a ? [`${b.description || ""} ${b.amount != null ? brl(b.amount) : ""}`] : a && !b ? [`${a.description || ""} ${a.amount != null ? brl(a.amount) : ""}`] : []);
};

const FeeSince = () => {
  const [val, setVal] = useState("");
  const [saved, setSaved] = useState(null);
  useEffect(() => { api.get("/finance/settings").then((r) => { setVal(r.data.fee_since || ""); setSaved(r.data.fee_since || ""); }); }, []);
  const save = async (v) => {
    try { const r = await api.put("/finance/settings", { fee_since: v || null }); setSaved(r.data.fee_since || ""); setVal(r.data.fee_since || ""); toast.success("Regra das taxas atualizada. Os lançamentos são recalculados em segundo plano (1–2 min)."); }
    catch (e) { toast.error(errMsg(e)); }
  };
  return (
    <Card className="p-5" data-testid="fee-since-card">
      <h3 className="text-base font-semibold text-slate-900">Taxas de visita a receber</h3>
      <p className="text-sm text-slate-500 mt-1">Só OSs cadastradas a partir desta data geram taxa “a receber”. Taxas já pagas continuam entrando como recebidas, de qualquer data.</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Input type="date" value={val} onChange={(e) => setVal(e.target.value)} className="w-44" data-testid="fee-since-input" />
        <Button onClick={() => save(val)} disabled={val === saved} data-testid="fee-since-save">Salvar</Button>
        {saved && <Button variant="outline" onClick={() => save("")} data-testid="fee-since-clear">Considerar todas</Button>}
        <span className="text-xs text-slate-500" data-testid="fee-since-current">{saved ? `Atual: a partir de ${saved.split("-").reverse().join("/")}` : "Atual: todas as OSs"}</span>
      </div>
    </Card>
  );
};

export default function Control() {
  const [rows, setRows] = useState(null);
  const [entity, setEntity] = useState("all");
  useEffect(() => {
    api.get("/finance/audit", { params: entity === "all" ? {} : { entity } }).then((r) => setRows(r.data)).catch((e) => toast.error(errMsg(e)));
  }, [entity]);
  return (
    <>
      <SectionTitle title="Controle" subtitle="Regras do financeiro e histórico de todas as alterações (quem, quando, antes e depois).">
        <SimpleSelect value={entity} onChange={setEntity} testId="audit-entity-select" options={[{ value: "all", label: "Tudo" }, ...Object.entries(ENTITY).map(([value, label]) => ({ value, label }))]} />
        <ExportButton filename="auditoria-financeiro" sheets={() => ({ Auditoria: (rows || []).map((r) => ({ Quando: new Date(r.created_at).toLocaleString("pt-BR"), Quem: r.actor_name, "Ação": ACTION[r.action] || r.action, Item: ENTITY[r.entity] || r.entity, ID: r.entity_id, "Mudanças": diffOf(r.before, r.after).join(" | "), Motivo: r.reason })) })} />
      </SectionTitle>
      <FeeSince />
      <Card className="overflow-x-auto" data-testid="audit-table">
        {!rows ? <div className="p-6 text-sm text-slate-500">Carregando…</div> : rows.length === 0 ? <Empty text="Nenhuma alteração registrada ainda" /> : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-4 py-3 text-left">Quando</th><th className="px-4 py-3 text-left">Quem</th><th className="px-4 py-3 text-left">O quê</th><th className="px-4 py-3 text-left">Mudanças</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.id} className="border-t border-slate-100 align-top" data-testid={`audit-row-${r.id}`}>
                <td className="px-4 py-2.5 whitespace-nowrap text-slate-600">{new Date(r.created_at).toLocaleString("pt-BR")}</td>
                <td className="px-4 py-2.5">{r.actor_name}</td>
                <td className="px-4 py-2.5"><span className="font-medium">{ACTION[r.action] || r.action}</span> <span className="text-slate-500">{ENTITY[r.entity] || r.entity}</span>{r.reason && <div className="text-xs text-amber-700">{r.reason}</div>}</td>
                <td className="px-4 py-2.5 text-xs text-slate-600">{diffOf(r.before, r.after).map((d, i) => <div key={i}>{d}</div>)}</td>
              </tr>
            ))}</tbody>
          </table>
        )}
      </Card>
    </>
  );
}
