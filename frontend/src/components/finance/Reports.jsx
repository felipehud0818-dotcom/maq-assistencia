import { Fragment, useEffect, useState } from "react";
import { api, errMsg } from "@/components/finance/api";
import { brl, fmtDate } from "@/lib/format";
import { Card, SectionTitle, PeriodFilter, initialPeriod, SimpleSelect, Empty, ExportButton } from "@/components/finance/common";
import { toast } from "sonner";

const tone = (v) => (v < 0 ? "text-rose-700" : "text-slate-900");
const delta = (cur, prev) => (prev ? `${(((cur - prev) / Math.abs(prev)) * 100).toFixed(1)}%` : "—");

const DreTable = ({ data }) => {
  const [open, setOpen] = useState({});
  return (
    <Card className="overflow-x-auto" data-testid="dre-table">
      <table className="w-full text-sm">
        <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr><th className="px-4 py-3 text-left">Linha</th><th className="px-4 py-3 text-right">Período</th><th className="px-4 py-3 text-right">% receita</th><th className="px-4 py-3 text-right">Anterior</th><th className="px-4 py-3 text-right">Variação</th></tr>
        </thead>
        <tbody>
          {data.lines.map((l) => (
            <Fragment key={l.key}>
              <tr className={`border-t border-slate-100 ${l.strong ? "bg-slate-50 font-semibold" : ""} ${l.children?.length ? "cursor-pointer hover:bg-slate-50" : ""}`}
                onClick={() => l.children?.length && setOpen({ ...open, [l.key]: !open[l.key] })} data-testid={`dre-line-${l.key}`}>
                <td className={`px-4 py-3 ${l.level ? "pl-8 text-slate-700" : ""}`}>{l.children?.length ? (open[l.key] ? "▾ " : "▸ ") : ""}{l.label}</td>
                <td className={`px-4 py-3 text-right font-mono-num ${tone(l.value)}`}>{brl(l.value)}</td>
                <td className="px-4 py-3 text-right text-slate-500">{l.pct != null ? `${l.pct}%` : "—"}</td>
                <td className="px-4 py-3 text-right font-mono-num text-slate-500">{brl(l.previous)}</td>
                <td className="px-4 py-3 text-right text-slate-500">{delta(l.value, l.previous)}</td>
              </tr>
              {open[l.key] && l.children.map((c) => (
                <tr key={`${l.key}-${c.name}`} className="text-xs text-slate-500">
                  <td className="px-4 py-1.5 pl-12">{c.name}</td><td className="px-4 py-1.5 text-right font-mono-num">{brl(c.total)}</td><td colSpan={3} />
                </tr>
              ))}
            </Fragment>
          ))}
        </tbody>
      </table>
    </Card>
  );
};

const Profit = ({ data }) => (
  <>
    <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
      {[["OS com valores", data.summary.os, false], ["Receita", data.summary.receita, true], ["Margem", data.summary.margem, true],
        ["Ticket médio", data.summary.ticket, true], ["OS deficitárias", data.summary.deficitarias, false]].map(([l, v, money]) => (
        <Card key={l} className="p-4" data-testid={`profit-kpi-${l}`}><div className="text-xs uppercase tracking-wide text-slate-500">{l}</div><div className="mt-1 text-xl font-bold font-mono-num">{money ? brl(v) : v}</div></Card>
      ))}
    </div>
    <Card className="overflow-x-auto" data-testid="profit-tech-table">
      <div className="px-4 pt-4 font-semibold text-slate-900">Por técnico</div>
      <table className="w-full text-sm mt-2">
        <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-4 py-2 text-left">Técnico</th><th className="px-4 py-2 text-right">OS</th><th className="px-4 py-2 text-right">Receita</th><th className="px-4 py-2 text-right">Ticket</th><th className="px-4 py-2 text-right">Margem</th><th className="px-4 py-2 text-right">%</th></tr></thead>
        <tbody>{data.technicians.map((t) => (
          <tr key={t.technician} className="border-t border-slate-100"><td className="px-4 py-2">{t.technician}</td><td className="px-4 py-2 text-right">{t.os}</td><td className="px-4 py-2 text-right font-mono-num">{brl(t.receita)}</td><td className="px-4 py-2 text-right font-mono-num">{brl(t.ticket)}</td><td className={`px-4 py-2 text-right font-mono-num ${tone(t.margem)}`}>{brl(t.margem)}</td><td className="px-4 py-2 text-right">{t.margem_pct ?? "—"}%</td></tr>
        ))}</tbody>
      </table>
    </Card>
    <Card className="overflow-x-auto" data-testid="profit-os-table">
      <div className="px-4 pt-4 font-semibold text-slate-900">Por OS <span className="text-xs font-normal text-slate-500">· menor margem primeiro</span></div>
      {data.os.length === 0 ? <Empty text="Nenhuma OS com lançamentos no período" /> : (
        <table className="w-full text-sm mt-2">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-4 py-2 text-left">OS</th><th className="px-4 py-2 text-left">Técnico</th><th className="px-4 py-2 text-right">Receita</th><th className="px-4 py-2 text-right">Custos</th><th className="px-4 py-2 text-right">Comissão</th><th className="px-4 py-2 text-right">Margem</th></tr></thead>
          <tbody>{data.os.slice(0, 200).map((o) => (
            <tr key={o.os_number} className="border-t border-slate-100" data-testid={`profit-os-${o.os_number}`}><td className="px-4 py-2 font-medium">#{o.os_number}</td><td className="px-4 py-2">{o.technician || "—"}</td><td className="px-4 py-2 text-right font-mono-num">{brl(o.receita)}</td><td className="px-4 py-2 text-right font-mono-num">{brl(o.custos)}</td><td className="px-4 py-2 text-right font-mono-num">{brl(o.comissao)}</td><td className={`px-4 py-2 text-right font-mono-num ${tone(o.margem)}`}>{brl(o.margem)} <span className="text-xs text-slate-400">{o.margem_pct != null ? `${o.margem_pct}%` : ""}</span></td></tr>
          ))}</tbody>
        </table>
      )}
    </Card>
  </>
);

export default function Reports() {
  const [period, setPeriod] = useState(initialPeriod());
  const [regime, setRegime] = useState("caixa");
  const [dre, setDre] = useState(null);
  const [profit, setProfit] = useState(null);
  useEffect(() => {
    const params = { start: period.start, end: period.end };
    api.get("/finance/dre", { params: { ...params, regime } }).then((r) => setDre(r.data)).catch((e) => toast.error(errMsg(e)));
    api.get("/finance/profitability", { params }).then((r) => setProfit(r.data)).catch((e) => toast.error(errMsg(e)));
  }, [period.start, period.end, regime]);
  return (
    <>
      <SectionTitle title="DRE e Rentabilidade" subtitle="DRE gerencial (não substitui a demonstração contábil oficial) e margem por OS e por técnico.">
        <PeriodFilter period={period} setPeriod={setPeriod} />
        <SimpleSelect value={regime} onChange={setRegime} testId="dre-regime-select" options={[{ value: "caixa", label: "Regime de caixa (pago)" }, { value: "competencia", label: "Competência (tudo pela data)" }]} />
        <ExportButton filename={`dre-${period.start}-a-${period.end}`} sheets={() => ({
          DRE: (dre?.lines || []).map((l) => ({ Linha: l.label, "Período": l.value, "% receita": l.pct, Anterior: l.previous })),
          "Por técnico": (profit?.technicians || []).map((t) => ({ "Técnico": t.technician, OS: t.os, Receita: t.receita, Ticket: t.ticket, Margem: t.margem, "%": t.margem_pct })),
          "Por OS": (profit?.os || []).map((o) => ({ OS: o.os_number, "Técnico": o.technician, Receita: o.receita, Custos: o.custos, "Comissão": o.comissao, Margem: o.margem, "%": o.margem_pct })),
        })} />
      </SectionTitle>
      {dre ? <><p className="text-xs text-slate-500">Comparando com {fmtDate(dre.previous_range.start)} a {fmtDate(dre.previous_range.end)} · clique numa linha para ver as categorias</p><DreTable data={dre} /></> : <div className="h-40 animate-pulse rounded-xl bg-slate-100" />}
      {profit ? <Profit data={profit} /> : <div className="h-40 animate-pulse rounded-xl bg-slate-100" />}
    </>
  );
}
