import React, { useEffect, useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { LayoutDashboard, ArrowDownToLine, ArrowUpFromLine, Tags, Repeat, Target, Users, Landmark, Bot, Lock, Loader2, FileBarChart, ShieldCheck } from 'lucide-react';
import DashboardLayout from '../components/DashboardLayout';
import { api, errMsg, getUnlock, setUnlock, clearUnlock } from '../components/finance/api';
import { Input } from '../components/ui/input';
import { Button } from '../components/ui/button';
import { RefDataProvider } from '../components/finance/RefData';
import Overview from '../components/finance/Overview';
import Transactions from '../components/finance/Transactions';
import Categories from '../components/finance/Categories';
import Recurring from '../components/finance/Recurring';
import Budgets from '../components/finance/Budgets';
import Technicians from '../components/finance/Technicians';
import Banks from '../components/finance/Banks';
import Assistants from '../components/finance/Assistants';
import Reports from '../components/finance/Reports';
import Control from '../components/finance/Control';

const TABS = [
  { id: 'visao-geral', label: 'Visão Geral', Icon: LayoutDashboard, tech: true, el: () => <Overview /> },
  { id: 'entradas', label: 'Entradas', Icon: ArrowDownToLine, tech: true, el: () => <Transactions type="entrada" /> },
  { id: 'saidas', label: 'Saídas', Icon: ArrowUpFromLine, tech: true, el: () => <Transactions type="saida" /> },
  { id: 'dre', label: 'DRE e Rentabilidade', Icon: FileBarChart, el: () => <Reports /> },
  { id: 'categorias', label: 'Categorias', Icon: Tags, el: () => <Categories /> },
  { id: 'despesas-fixas', label: 'Despesas Fixas', Icon: Repeat, el: () => <Recurring /> },
  { id: 'metas-limites', label: 'Metas e Limites', Icon: Target, el: () => <Budgets /> },
  { id: 'tecnicos', label: 'Técnicos', Icon: Users, tech: true, el: () => <Technicians /> },
  { id: 'bancos', label: 'Bancos', Icon: Landmark, el: () => <Banks /> },
  { id: 'assistentes', label: 'Assistentes', Icon: Bot, el: () => <Assistants /> },
  { id: 'controle', label: 'Controle', Icon: ShieldCheck, el: () => <Control /> },
];

function Blocked() {
  return (
    <div className="max-w-xl mx-auto mt-20 p-8 text-center border border-slate-200 rounded-xl bg-white" data-testid="finance-blocked">
      <Lock className="mx-auto text-slate-400 mb-3" />
      <h1 className="text-xl font-semibold text-slate-900">Sem acesso ao Financeiro</h1>
      <p className="text-sm text-slate-500 mt-2">O seu cadastro não libera esta página. Fale com o administrador.</p>
    </div>
  );
}

function LockScreen({ onUnlocked }) {
  const [pwd, setPwd] = useState('');
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setErr('');
    try {
      const r = await api.post('/finance/unlock', { password: pwd });
      setUnlock(r.data);
      onUnlocked();
    } catch (ex) { setErr(errMsg(ex)); setPwd(''); }
    setBusy(false);
  };
  return (
    <form onSubmit={submit} className="max-w-sm mx-auto mt-20 p-8 text-center border border-slate-200 rounded-xl bg-white space-y-4" data-testid="finance-lock-screen">
      <Lock className="mx-auto text-slate-500" />
      <div>
        <h1 className="text-xl font-semibold text-slate-900">Financeiro protegido</h1>
        <p className="text-sm text-slate-500 mt-1">Digite a senha do financeiro para continuar.</p>
      </div>
      <Input type="password" inputMode="numeric" autoFocus value={pwd} onChange={(e) => setPwd(e.target.value)} placeholder="Senha" className="text-center tracking-[0.4em]" data-testid="finance-lock-input" />
      {err && <p className="text-sm text-red-600" data-testid="finance-lock-error">{err}</p>}
      <Button type="submit" disabled={!pwd || busy} className="w-full" data-testid="finance-lock-submit">
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Desbloquear'}
      </Button>
    </form>
  );
}

export default function FinancialPage() {
  const { tab = 'visao-geral' } = useParams();
  const navigate = useNavigate();
  const [me, setMe] = useState(null);
  const [unlocked, setUnlocked] = useState(() => !!getUnlock());

  const [meErr, setMeErr] = useState('');
  const loadMe = () => {
    setMeErr('');
    api.get('/finance/me').then((r) => setMe(r.data)).catch((e) => {
      // Só "sem acesso" quando o servidor disser isso; falha de rede vira erro com nova tentativa.
      if (e?.response?.status === 403) setMe({ has_finance: false }); else setMeErr(errMsg(e));
    });
  };

  useEffect(loadMe, []);

  useEffect(() => {
    const relock = () => setUnlocked(false);
    window.addEventListener('finance-locked', relock);
    const t = setInterval(() => { if (!getUnlock()) setUnlocked(false); }, 60000);
    return () => {
      window.removeEventListener('finance-locked', relock);
      clearInterval(t);
      // Saiu do Financeiro (não só trocou de aba): exige a senha de novo.
      setTimeout(() => { if (!window.location.pathname.startsWith('/financial')) clearUnlock(); }, 0);
    };
  }, []);

  if (meErr) return <DashboardLayout><div className="max-w-md mx-auto mt-20 p-6 text-center border border-slate-200 rounded-xl bg-white space-y-3" data-testid="finance-load-error"><p className="text-sm text-rose-700">Não foi possível carregar o Financeiro: {meErr}</p><Button onClick={loadMe} data-testid="finance-retry-btn">Tentar de novo</Button></div></DashboardLayout>;
  if (!me) return <DashboardLayout><div className="py-20 text-center text-slate-500"><Loader2 className="h-5 w-5 animate-spin inline mr-2" />Carregando…</div></DashboardLayout>;
  if (!me.has_finance) return <DashboardLayout><Blocked /></DashboardLayout>;
  if (!unlocked) return <DashboardLayout><LockScreen onUnlocked={() => setUnlocked(true)} /></DashboardLayout>;

  const tabs = TABS.filter((t) => me.is_admin || t.tech);
  const current = tabs.find((t) => t.id === tab) || tabs[0];

  return (
    <DashboardLayout>
      <RefDataProvider>
        <div className="space-y-6 text-slate-900" data-testid="financial-page">
          <div className="border-b border-slate-200">
            <div className="flex items-center justify-between gap-3">
              <h1 className="text-2xl sm:text-3xl font-bold tracking-tight text-slate-900">Financeiro</h1>
              <Button variant="outline" size="sm" onClick={clearUnlock} data-testid="finance-lock-btn"><Lock size={14} className="mr-1" />Bloquear</Button>
            </div>
            <p className="text-sm text-slate-500 mt-1">Quanto entrou, quanto saiu, quanto cada OS rendeu e quanto falta receber.</p>
            <div className="flex gap-1 mt-4 overflow-x-auto -mb-px" data-testid="finance-tabs">
              {tabs.map(({ id, label, Icon }) => (
                <button key={id} onClick={() => navigate(`/financial/${id}`)} data-testid={`tab-nav-${id}`}
                  className={`flex items-center gap-2 px-3 py-2.5 text-sm font-medium whitespace-nowrap border-b-2 transition-colors ${current.id === id ? 'border-blue-600 text-blue-700' : 'border-transparent text-slate-500 hover:text-slate-900'}`}>
                  <Icon size={15} />{label}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-6 fade-up" key={current.id}>
            {current.el()}
          </div>
        </div>
      </RefDataProvider>
    </DashboardLayout>
  );
}
