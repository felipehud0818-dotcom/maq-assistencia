import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ResponsiveContainer, ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, CartesianGrid, Legend, ReferenceLine } from "recharts";
import { Wallet, ArrowDownToLine, ArrowUpFromLine, TrendingUp, AlertOctagon, AlertTriangle, Info, ChevronRight, RefreshCw } from "lucide-react";
import { api, errMsg } from "@/components/finance/api";
import { brl } from "@/lib/format";
import { Card } from "@/components/finance/common";

const dm = (iso) => `${iso.slice(8, 10)}/${iso.slice(5, 7)}`;

const Stat = ({ id, Icon, label, value, tone, lines }) => (
  <Card className="p-5 flex flex-col gap-2" data-testid={`cc-kpi-${id}`}>
    <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-slate-500"><Icon size={14} />{label}</div>
    <div className={`text-2xl font-bold font-mono-num ${tone}`} data-testid={`cc-kpi-value-${id}`}>{value}</div>
    <div className="space-y-0.5 text-xs text-slate-500">{lines.filter(Boolean).map((l, i) => <div key={i}>{l}</div>)}</div>
  </Card>
);

const LEVEL = {
  critico: { Icon: AlertOctagon, cls: "border-l-rose-600 bg-rose-50/60", ic: "text-rose-600", tag: "Crítico" },
  atencao: { Icon: AlertTriangle, cls: "border-l-amber-500 bg-amber-50/60", ic: "text-amber-600", tag: "Atenção" },
  info: { Icon: Info, cls: "border-l-blue-500 bg-blue-50/50", ic: "text-blue-600", tag: "Informativo" },
};

const Attention = ({ items }) => {
  const navigate = useNavigate();
  return (
    <Card className="p-5" data-testid="cc-attention">
      <h3 className="text-base font-semibold text-slate-900">Central de atenção</h3>
      <p className="text-xs text-slate-500 mb-3">O que precisa de você hoje</p>
      {items.length === 0 ? <div className="py-6 text-center text-sm text-emerald-700">Tudo em dia.</div> : (
        <ul className="space-y-2 max-h-[300px] overflow-y-auto pr-1">
          {items.map((a, i) => {
            const L = LEVEL[a.level];
            return (
              <li key={i}>
                <button onClick={() => navigate(`/financial/${a.tab}`)} className={`w-full text-left flex items-start gap-3 rounded-md border-l-4 px-3 py-2.5 transition-colors hover:bg-white ${L.cls}`} data-testid={`cc-attention-item-${i}`}>
                  <L.Icon size={16} className={`mt-0.5 shrink-0 ${L.ic}`} />
                  <span className="min-w-0 flex-1">
                    <span className="block text-[10px] font-semibold uppercase tracking-wide text-slate-500">{L.tag}</span>
                    <span className="block text-sm font-medium text-slate-900">{a.title}</span>
                    <span className="block text-xs text-slate-600">{a.detail}</span>
                  </span>
                  <ChevronRight size={14} className="mt-1 text-slate-400" />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
};

const CashFlow = ({ weeks, today }) => {
  const data = weeks.map((w) => ({
    semana: dm(w.start), Entradas: w.entradas, "Saídas": w.saidas,
    "Entradas previstas": w.entradas_prev, "Saídas previstas": w.saidas_prev, "Saldo projetado": w.saldo_projetado ?? null,
  }));
  const cur = weeks.find((w) => w.start <= today && today <= w.end);
  return (
    <Card className="p-5 lg:col-span-2" data-testid="cc-cashflow-chart">
      <h3 className="text-base font-semibold text-slate-900">Fluxo de caixa semanal</h3>
      <p className="text-xs text-slate-500 mb-3">Realizado no período + previsto nas próximas 4 semanas · linha = saldo projetado a partir do caixa de hoje</p>
      <div className="h-72">
        <ResponsiveContainer>
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" vertical={false} stroke="#E2E8F0" />
            <XAxis dataKey="semana" fontSize={11} tickLine={false} />
            <YAxis fontSize={11} tickLine={false} axisLine={false} width={70} tickFormatter={(v) => `R$${Math.round(v / 100) / 10}k`} />
            <Tooltip formatter={(v) => brl(v)} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            {cur && <ReferenceLine x={dm(cur.start)} stroke="#0F172A" strokeDasharray="4 4" label={{ value: "hoje", fontSize: 10, position: "top" }} />}
            <Bar dataKey="Entradas" stackId="e" fill="#16A34A" />
            <Bar dataKey="Entradas previstas" stackId="e" fill="#86EFAC" radius={[3, 3, 0, 0]} />
            <Bar dataKey="Saídas" stackId="s" fill="#DC2626" />
            <Bar dataKey="Saídas previstas" stackId="s" fill="#FCA5A5" radius={[3, 3, 0, 0]} />
            <Line type="monotone" dataKey="Saldo projetado" stroke="#1D4ED8" strokeWidth={2} dot={{ r: 2 }} connectNulls={false} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </Card>
  );
};

const Aging = ({ rows }) => {
  const max = Math.max(1, ...rows.map((r) => r.total));
  return (
    <Card className="p-5" data-testid="cc-aging">
      <h3 className="text-base font-semibold text-slate-900">A receber por atraso</h3>
      <p className="text-xs text-slate-500 mb-3">Valores em aberto por faixa de vencimento</p>
      <div className="space-y-2.5">
        {rows.map((r, i) => (
          <div key={r.key} data-testid={`cc-aging-${r.key}`}>
            <div className="flex justify-between text-xs"><span className="text-slate-700">{r.label} <span className="text-slate-400">· {r.count}</span></span><span className="font-mono-num font-semibold">{brl(r.total)}</span></div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-slate-100">
              <div className={`h-full rounded-full ${i === 0 ? "bg-emerald-500" : i < 3 ? "bg-amber-500" : "bg-rose-600"}`} style={{ width: `${(r.total / max) * 100}%` }} />
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
};

const Skeleton = () => (
  <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4" data-testid="cc-loading">
    {[0, 1, 2, 3].map((i) => <div key={i} className="h-32 animate-pulse rounded-xl bg-slate-100" />)}
  </div>
);

export default function CommandCenter({ period }) {
  const [d, setD] = useState(null);
  const [err, setErr] = useState("");
  const load = () => {
    setErr("");
    api.get("/finance/command-center", { params: { start: period.start, end: period.end } })
      .then((r) => setD(r.data)).catch((e) => setErr(errMsg(e)));
  };
  useEffect(load, [period.start, period.end]); // eslint-disable-line react-hooks/exhaustive-deps

  if (err) return (
    <Card className="p-5 flex items-center justify-between" data-testid="cc-error">
      <span className="text-sm text-rose-700">Não foi possível carregar o painel: {err}</span>
      <button onClick={load} className="flex items-center gap-1 text-sm font-medium text-slate-700 hover:text-slate-900" data-testid="cc-retry-btn"><RefreshCw size={14} />Tentar de novo</button>
    </Card>
  );
  if (!d) return <Skeleton />;
  const { caixa, receber, pagar, lucro } = d;
  return (
    <div className="space-y-4" data-testid="command-center">
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <Stat id="caixa" Icon={Wallet} label="Saldo disponível" value={brl(caixa.valor)} tone={caixa.valor >= 0 ? "text-slate-900" : "text-rose-700"}
          lines={[caixa.fonte === "bancos" ? `Posição bancária · ${caixa.contas} conta(s)` : "Realizado acumulado (sem banco conectado)"]} />
        <Stat id="receber" Icon={ArrowDownToLine} label="A receber" value={brl(receber.total)} tone="text-emerald-700"
          lines={[`${receber.count} em aberto · ${brl(receber.prox7)} em 7 dias`, receber.atrasado ? <span className="text-rose-700">{brl(receber.atrasado)} vencido</span> : null]} />
        {pagar ? (
          <Stat id="pagar" Icon={ArrowUpFromLine} label="A pagar" value={brl(pagar.total)} tone="text-rose-700"
            lines={[`${pagar.count} em aberto · ${brl(pagar.prox7)} em 7 dias`, pagar.atrasado ? <span className="text-rose-700">{brl(pagar.atrasado)} vencido</span> : null]} />
        ) : <div className="hidden lg:block" />}
        <Stat id="lucro" Icon={TrendingUp} label="Lucro operacional" value={brl(lucro.valor)} tone={lucro.valor >= 0 ? "text-blue-700" : "text-rose-700"}
          lines={[`Recebido ${brl(lucro.receita)} − pago ${brl(lucro.despesa)}`, lucro.margem_pct != null ? `Margem ${lucro.margem_pct}% no período` : null]} />
      </div>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <CashFlow weeks={d.weeks} today={d.today} />
        <Attention items={d.attention} />
      </div>
      <Aging rows={d.aging} />
    </div>
  );
}
