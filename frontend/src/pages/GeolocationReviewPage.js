
import React, { useCallback, useEffect, useState } from 'react';
import axios from 'axios';
import DashboardLayout from '../components/DashboardLayout';
import { useAuth } from '../context/AuthContext';
import { Button } from '../components/ui/button';
import { Badge } from '../components/ui/badge';
import { toast } from 'sonner';
import { RefreshCw, MapPin, AlertTriangle } from 'lucide-react';

const API = `${process.env.REACT_APP_BACKEND_URL || ''}/api`;

const statusLabel = {
  pending: 'Pendente',
  review: 'Revisar',
  blocked: 'Bloqueado',
  incomplete: 'Incompleto',
  error: 'Erro',
  not_found: 'Não encontrado'
};

const statusClass = {
  pending: 'bg-gray-100 text-gray-700',
  review: 'bg-amber-100 text-amber-800',
  blocked: 'bg-red-100 text-red-800',
  incomplete: 'bg-gray-100 text-gray-700',
  error: 'bg-red-100 text-red-800',
  not_found: 'bg-red-100 text-red-800'
};

export default function GeolocationReviewPage() {
  const { getAuthHeader } = useAuth();
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [reconciling, setReconciling] = useState(false);
  const [filter, setFilter] = useState('all');

  const loadPending = useCallback(async () => {
    setLoading(true);

    try {
      const { data } = await axios.get(
        `${API}/routes/geocode/pending?limit=5000`,
        getAuthHeader()
      );

      setItems(data.results || []);
    } catch (e) {
      toast.error(
        e.response?.data?.detail || 'Erro ao carregar pendências.'
      );
    } finally {
      setLoading(false);
    }
  }, [getAuthHeader]);

  useEffect(() => {
    loadPending();
  }, [loadPending]);

  const reconcile = async () => {
    setReconciling(true);

    try {
      const { data } = await axios.post(
        `${API}/routes/geocode/reconcile?clear_legacy=false&limit=5000`,
        {},
        getAuthHeader()
      );

      toast.success(
        `Análise concluída: ${data.scanned} OS verificadas.`
      );

      await loadPending();
    } catch (e) {
      toast.error(
        e.response?.data?.detail || 'Erro na reconciliação.'
      );
    } finally {
      setReconciling(false);
    }
  };

  const visibleItems = items.filter(item =>
    filter === 'all' || item.status === filter
  );

  const counts = items.reduce((acc, item) => {
    acc[item.status] = (acc[item.status] || 0) + 1;
    return acc;
  }, {});

  return (
    <DashboardLayout>
      <div className="p-4 md:p-6 space-y-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">
              Qualidade dos endereços
            </h1>
            <p className="text-sm text-gray-500 mt-1">
              OSs sem localização aprovada ou que precisam de revisão.
            </p>
          </div>

          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={loadPending}
              disabled={loading}
            >
              <RefreshCw className="w-4 h-4 mr-2" />
              Atualizar
            </Button>

            <Button
              onClick={reconcile}
              disabled={reconciling}
            >
              <MapPin className="w-4 h-4 mr-2" />
              {reconciling ? 'Analisando...' : 'Reconciliar'}
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {[
            ['review', 'Revisar'],
            ['blocked', 'Bloqueadas'],
            ['incomplete', 'Incompletas'],
            ['pending', 'Pendentes']
          ].map(([key, label]) => (
            <div key={key} className="border rounded-lg p-4">
              <p className="text-sm text-gray-500">{label}</p>
              <p className="text-2xl font-bold">
                {counts[key] || 0}
              </p>
            </div>
          ))}
        </div>

        <div className="flex flex-wrap gap-2">
          {[
            ['all', 'Todas'],
            ['review', 'Revisar'],
            ['blocked', 'Bloqueadas'],
            ['incomplete', 'Incompletas'],
            ['pending', 'Pendentes']
          ].map(([key, label]) => (
            <Button
              key={key}
              size="sm"
              variant={filter === key ? 'default' : 'outline'}
              onClick={() => setFilter(key)}
            >
              {label}
            </Button>
          ))}
        </div>

        {loading ? (
          <p className="text-sm text-gray-500">Carregando...</p>
        ) : visibleItems.length === 0 ? (
          <div className="border rounded-lg p-8 text-center">
            <MapPin className="w-8 h-8 mx-auto mb-2 text-gray-400" />
            <p className="font-medium">Nenhuma pendência encontrada</p>
          </div>
        ) : (
          <div className="border rounded-lg divide-y">
            {visibleItems.map(item => (
              <div
                key={item.os_number}
                className="p-4 flex flex-col md:flex-row md:items-center gap-3"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center gap-2 mb-1">
                    <strong>OS {item.os_number}</strong>
                    <Badge className={statusClass[item.status] || ''}>
                      {statusLabel[item.status] || item.status}
                    </Badge>
                  </div>

                  <p className="text-sm text-gray-600 break-words">
                    {item.address || 'Endereço não informado'}
                  </p>

                  {item.reasons?.length > 0 && (
                    <p className="text-xs text-amber-700 mt-2">
                      Motivos: {item.reasons.join(', ')}
                    </p>
                  )}

                  {item.candidate && (
                    <p className="text-xs text-gray-500 mt-2">
                      Candidato: {item.candidate.formatted_address ||
                        `${item.candidate.lat}, ${item.candidate.lng}`}
                    </p>
                  )}
                </div>

                <div className="flex items-center gap-2 text-xs text-gray-500">
                  <AlertTriangle className="w-4 h-4" />
                  Revisão necessária
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </DashboardLayout>
  );
}
