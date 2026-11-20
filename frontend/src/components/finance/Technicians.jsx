import { useEffect, useState, useCallback } from "react";
import { HandCoins, Save, ListChecks } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { api, errMsg } from "@/components/finance/api";
import { brl, fmtDate, METHODS } from "@/lib/format";
import { useAuth } from "@/context/AuthContext";
import { useRef_ } from "@/components/finance/RefData";
import { Card, SectionTitle, PeriodFilter, initialPeriod, SimpleSelect, Empty, ExportButton } from "@/components/finance/common";
import { exportXlsx } from "@/lib/exportXlsx";
import { toast } from "sonner";

const osSheet = (list) => list.map((o) => ({ OS: o.os_number, Cliente: o.customer_name, Bairro: o.neighborhood, Cidade: o.city,
  Roteiro: o.route_name, Data: fmtDate(o.route_date), "Situação": o.concluida ? "Concluída" : "Pendente", "Conclusão": o.conclusion }));

const RoutedOsDialog = ({ row, period, onClose }) => {
  const [list, setList] = useState(null);
  useEffect(() => {
    if (!row) { setList(null); return; }
    api.get(`/finance/technicians/${row.technician_id}/os`, { params: { start: period.start, end: period.end } })
      .then((r) => setList(r.data)).catch((e) => { toast.error(errMsg(e)); setList([]); });
  }, [row, period.start, period.end]);
  return (
    <Dialog open={!!row} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="bg-white max-w-3xl max-h-[85vh] overflow-y-auto" data-testid="tech-os-dialog">
        <DialogHeader><DialogTitle>OSs dos roteiros · {row?.name}</DialogTitle></DialogHeader>
        <div className="flex items-center justify-between gap-2">
          <p className="text-sm text-slate-500">Período {fmtDate(period.start)} a {fmtDate(period.end)} · {list?.length ?? "…"} OS · {list ? list.filter((o) => o.concluida).length : "…"} concluídas</p>
          <Button variant="outline" size="sm" disabled={!list?.length} onClick={() => exportXlsx(`os-${row.name}-${period.start}-a-${period.end}`, { OSs: osSheet(list) })} data-testid="tech-os-export-btn">Exportar Excel</Button>
        </div>
        {!list ? <div className="py-8 text-center text-sm text-slate-500">Carregando…</div> : list.length === 0 ? <Empty text="Nenhuma OS em roteiros no período" /> : (
          <table className="w-full text-sm" data-testid="tech-os-table">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-3 py-2 text-left">OS</th><th className="px-3 py-2 text-left">Cliente</th><th className="px-3 py-2 text-left">Roteiro</th><th className="px-3 py-2 text-left">Situação</th></tr></thead>
            <tbody>
              {list.map((o) => (
                <tr key={o.os_number} className="border-t border-slate-100" data-testid={`tech-os-row-${o.os_number}`}>
                  <td className="px-3 py-2 font-medium whitespace-nowrap">#{o.os_number}</td>
                  <td className="px-3 py-2">{o.customer_name}<div className="text-xs text-slate-500">{[o.neighborhood, o.city].filter(Boolean).join(" · ")}</div></td>
                  <td className="px-3 py-2 text-xs">{o.route_name}<div className="text-slate-500">{fmtDate(o.route_date)}</div></td>
                  <td className="px-3 py-2"><span className={`text-xs px-2 py-0.5 rounded-full border ${o.concluida ? "bg-emerald-50 text-emerald-700 border-emerald-200" : "bg-amber-50 text-amber-700 border-amber-200"}`}>{o.concluida ? "Concluída" : "Pendente"}</span>{o.conclusion && <div className="text-xs text-slate-500 mt-0.5">{o.conclusion}</div>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </DialogContent>
    </Dialog>
  );
};

const PayoutDialog = ({ row, period, onClose, onSaved }) => {
  const [amount, setAmount] = useState("");
  const [method, setMethod] = useState("pix");
  useEffect(() => { if (row) setAmount(row.a_repassar); }, [row]);
  const save = async () => {
    try {
      await api.post("/finance/payouts", { technician_id: row.technician_id, amount: Number(amount), period_start: period.start, period_end: period.end, payment_method: method });
      toast.success("Repasse registrado e lançado como saída");
      onSaved();
      onClose();
    } catch (e) { toast.error(errMsg(e)); }
  };
  return (
    <Dialog open={!!row} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="bg-white max-w-sm" data-testid="payout-dialog">
        <DialogHeader><DialogTitle>Repasse para {row?.name}</DialogTitle></DialogHeader>
        <p className="text-sm text-slate-500">Período {fmtDate(period.start)} a {fmtDate(period.end)}. Comissão {brl(row?.comissao)}, já repassado {brl(row?.repassado)}.</p>
        <div className="space-y-1.5"><Label className="text-xs">Valor do repasse (R$)</Label><Input type="number" step="0.01" value={amount} onChange={(e) => setAmount(e.target.value)} data-testid="payout-amount" /></div>
        <div className="space-y-1.5"><Label className="text-xs">Forma</Label><SimpleSelect className="w-full" value={method} onChange={setMethod} testId="payout-method" options={Object.entries(METHODS).map(([value, label]) => ({ value, label }))} /></div>
        <DialogFooter><Button variant="outline" onClick={onClose}>Cancelar</Button><Button className="bg-blue-600 hover:bg-blue-700" onClick={save} data-testid="payout-submit-btn">Confirmar repasse</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
};

const TechSettings = () => {
  const { techs, reload } = useRef_();
  const [edit, setEdit] = useState({});
  useEffect(() => { setEdit(Object.fromEntries(techs.map((t) => [t.id, { commission_pct: t.commission_pct, finance_access: t.finance_access }]))); }, [techs]);
  const save = async (id) => {
    try { await api.put(`/finance/technicians/${id}/settings`, { commission_pct: Number(edit[id].commission_pct), finance_access: edit[id].finance_access }); toast.success("Cadastro atualizado"); reload(); } catch (e) { toast.error(errMsg(e)); }
  };
  return (
    <Card className="p-5" data-testid="tech-settings">
      <h3 className="text-base font-semibold text-slate-900">Cadastro financeiro dos técnicos</h3>
      <p className="text-xs text-slate-500 mb-3">Comissão = % sobre o valor de serviço recebido. O acesso define se o técnico vê os próprios dados no Financeiro ou nada.</p>
      <div className="space-y-2">
        {techs.length === 0 && <Empty text="Nenhum técnico cadastrado em Usuários" />}
        {techs.map((t) => edit[t.id] && (
          <div key={t.id} className="flex flex-wrap items-center gap-3 border border-slate-100 rounded-lg px-3 py-2">
            <span className="font-medium w-40">{t.name}</span>
            <div className="flex items-center gap-1"><Input type="number" className="w-20 h-9" value={edit[t.id].commission_pct} onChange={(e) => setEdit({ ...edit, [t.id]: { ...edit[t.id], commission_pct: e.target.value } })} data-testid={`tech-commission-${t.id}`} /><span className="text-sm text-slate-500">%</span></div>
            <SimpleSelect className="w-56 h-9" value={edit[t.id].finance_access} onChange={(v) => setEdit({ ...edit, [t.id]: { ...edit[t.id], finance_access: v } })} testId={`tech-access-${t.id}`}
              options={[{ value: "own", label: "Vê só os dados dele" }, { value: "none", label: "Sem acesso ao financeiro" }]} />
            <Button size="sm" variant="outline" onClick={() => save(t.id)} data-testid={`tech-save-${t.id}`}><Save size={14} className="mr-1" />Salvar</Button>
          </div>
        ))}
      </div>
    </Card>
  );
};

export default function Technicians() {
  const { isAdmin } = useAuth();
  const [period, setPeriod] = useState(initialPeriod());
  const [rows, setRows] = useState([]);
  const [payouts, setPayouts] = useState([]);
  const [payRow, setPayRow] = useState(null);
  const [osRow, setOsRow] = useState(null);

  const load = useCallback(() => {
    api.get("/finance/technicians", { params: { start: period.start, end: period.end } }).then((r) => setRows(r.data)).catch((e) => toast.error(errMsg(e)));
    api.get("/finance/payouts").then((r) => setPayouts(r.data));
  }, [period.start, period.end]);
  useEffect(() => { load(); }, [load]);

  return (
    <>
      <SectionTitle title="Técnicos" subtitle="Fechamento do período: valor gerado, comissão, repasses e saldo a repassar.">
        <PeriodFilter period={period} setPeriod={setPeriod} />
        <ExportButton filename={`tecnicos-${period.start}-a-${period.end}`} sheets={() => ({
          Fechamento: rows.map((r) => ({ "Técnico": r.name, "OS (roteiros)": r.os_routed, "OS concluídas": r.os_routed_done, "OS com lançamento": r.os_count,
            Gerado: r.gerado, Recebido: r.recebido, "Serviço recebido": r.servico_pago, "Comissão %": r.commission_pct, "Comissão": r.comissao, Repassado: r.repassado, "A repassar": r.a_repassar })),
          Repasses: payouts.map((p) => ({ Data: fmtDate(p.paid_at), "Técnico": p.technician_name, "Início": fmtDate(p.period_start), Fim: fmtDate(p.period_end), Valor: p.amount })),
        })} />
      </SectionTitle>
      <Card className="overflow-x-auto">
        {rows.length === 0 ? <Empty text="Nenhum técnico" /> : (
          <table className="w-full text-sm" data-testid="tech-closing-table">
            <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
              <tr><th className="px-4 py-3 text-left">Técnico</th><th className="px-4 py-3 text-right">OS (roteiros)</th><th className="px-4 py-3 text-right">Gerado</th><th className="px-4 py-3 text-right">Recebido</th><th className="px-4 py-3 text-right">Serviço recebido</th><th className="px-4 py-3 text-right">Comissão</th><th className="px-4 py-3 text-right">Repassado</th><th className="px-4 py-3 text-right">A repassar</th>{isAdmin && <th />}</tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.technician_id} className="border-t border-slate-100" data-testid={`tech-row-${r.technician_id}`}>
                  <td className="px-4 py-3 font-medium">{r.name}<div className="text-xs text-slate-500">{r.commission_pct}% do serviço</div></td>
                  <td className="px-4 py-3 text-right">
                    <button onClick={() => setOsRow(r)} className="inline-flex items-center gap-1 font-semibold text-blue-700 hover:underline" title="Ver OSs atendidas nos roteiros" data-testid={`tech-os-count-${r.technician_id}`}>
                      <ListChecks size={14} />{r.os_routed}
                    </button>
                    <div className="text-[11px] text-slate-500">{r.os_routed_done} concluídas</div>
                  </td>
                  <td className="px-4 py-3 text-right font-mono-num">{brl(r.gerado)}</td>
                  <td className="px-4 py-3 text-right font-mono-num">{brl(r.recebido)}</td>
                  <td className="px-4 py-3 text-right font-mono-num">{brl(r.servico_pago)}</td>
                  <td className="px-4 py-3 text-right font-mono-num font-semibold">{brl(r.comissao)}</td>
                  <td className="px-4 py-3 text-right font-mono-num">{brl(r.repassado)}</td>
                  <td className="px-4 py-3 text-right font-mono-num font-bold text-blue-700" data-testid={`tech-pending-${r.technician_id}`}>{brl(r.a_repassar)}</td>
                  {isAdmin && <td className="px-3 py-3 text-right"><Button size="sm" className="bg-blue-600 hover:bg-blue-700" disabled={r.a_repassar <= 0} onClick={() => setPayRow(r)} data-testid={`technician-payout-btn-${r.technician_id}`}><HandCoins size={14} className="mr-1" />Repassar</Button></td>}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      <Card className="p-5" data-testid="payout-history">
        <h3 className="text-base font-semibold text-slate-900 mb-3">Histórico de repasses</h3>
        {payouts.length === 0 ? <Empty text="Nenhum repasse registrado" /> : (
          <div className="divide-y divide-slate-100 text-sm">
            {payouts.map((p) => (
              <div key={p.id} className={`flex items-center justify-between gap-2 py-2 ${p.reversed ? "opacity-50 line-through" : ""}`} data-testid={`payout-row-${p.id}`}>
                <span>{fmtDate(p.paid_at)} · {p.technician_name} · período {fmtDate(p.period_start)} a {fmtDate(p.period_end)}{p.reversed ? " · estornado" : ""}</span>
                <span className="flex items-center gap-3">
                  <span className="font-mono-num font-semibold">{brl(p.amount)}</span>
                  {isAdmin && !p.reversed && <button className="text-xs text-rose-700 hover:underline no-underline" data-testid={`payout-reverse-${p.id}`}
                    onClick={async () => { if (!window.confirm("Estornar este repasse? A saída correspondente será removida e o valor volta para 'a repassar'.")) return;
                      try { await api.post(`/finance/payouts/${p.id}/reverse`); toast.success("Repasse estornado"); load(); } catch (e) { toast.error(errMsg(e)); } }}>Estornar</button>}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>
      {isAdmin && <TechSettings />}
      <PayoutDialog row={payRow} period={period} onClose={() => setPayRow(null)} onSaved={load} />
      <RoutedOsDialog row={osRow} period={period} onClose={() => setOsRow(null)} />
    </>
  );
}
