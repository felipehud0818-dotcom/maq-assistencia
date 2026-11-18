import React, { useState } from 'react';
import { Clock, MapPin, Route as RouteIcon, AlertTriangle, GripVertical, User } from 'lucide-react';

const fmtMin = (m) => (m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}` : `${m} min`);

// Move uma OS de um quadro para outro (ou reordena no mesmo), inserindo antes de `beforeOs`.
export const moveOrderBetween = (options, fromKey, osNumber, toKey, beforeOs) => {
  const src = options.find((o) => o.key === fromKey);
  const item = src?.orders.find((o) => o.os_number === osNumber);
  if (!item) return options;
  return options.map((opt) => {
    let orders = opt.key === fromKey ? opt.orders.filter((o) => o.os_number !== osNumber) : opt.orders;
    if (opt.key === toKey) {
      const idx = beforeOs ? orders.findIndex((o) => o.os_number === beforeOs) : -1;
      orders = idx >= 0 ? [...orders.slice(0, idx), item, ...orders.slice(idx)] : [...orders, item];
    }
    const changed = opt.key === fromKey || opt.key === toKey;
    return changed ? { ...opt, orders, edited: true } : opt;
  });
};

const OrderCard = ({ o, i, colKey, drag, setDrag, onDropAt }) => (
  <li
    draggable
    onDragStart={(e) => { e.dataTransfer.effectAllowed = 'move'; setDrag({ from: colKey, os: o.os_number }); }}
    onDragEnd={() => setDrag(null)}
    onDragOver={(e) => { e.preventDefault(); e.stopPropagation(); }}
    onDrop={(e) => { e.preventDefault(); e.stopPropagation(); onDropAt(colKey, o.os_number); }}
    className={`cursor-grab rounded-md border border-gray-100 bg-gray-50 p-2 text-xs transition-opacity active:cursor-grabbing ${drag?.os === o.os_number ? 'opacity-40' : ''}`}
    data-testid={`kanban-os-${o.os_number}`}
  >
    <div className="flex items-center justify-between gap-2">
      <span className="flex items-center gap-1.5 font-semibold text-gray-900">
        <GripVertical className="h-3 w-3 text-gray-300" />
        <span className="grid h-4 w-4 place-items-center rounded-full bg-gray-900 text-[9px] text-white">{i + 1}</span>
        OS {o.os_number}
        {o.priority && <AlertTriangle className="h-3 w-3 text-red-600" />}
      </span>
      {o._eta && <span className="flex items-center gap-0.5 text-[10px] text-gray-500"><Clock className="h-3 w-3" />{o._eta}</span>}
    </div>
    <div className="mt-0.5 truncate text-gray-700">{o.customer_name}</div>
    <div className="flex items-center gap-1 truncate text-[10px] text-gray-400">
      <MapPin className="h-3 w-3 shrink-0" />{[o.neighborhood, o.city].filter(Boolean).join(' · ')}
      {o._leg != null && <span className="ml-auto shrink-0">+{o._leg} min</span>}
    </div>
  </li>
);

const KanbanColumn = ({ opt, checked, onToggle, onApply, technicians, tech, onTech, drag, setDrag, onDropAt, maxStops }) => {
  const over = drag && drag.from !== opt.key;
  return (
    <div
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => { e.preventDefault(); onDropAt(opt.key, null); }}
      className={`flex min-w-[260px] flex-1 snap-start flex-col rounded-lg border-2 bg-white transition-colors ${checked ? 'border-blue-600 ring-2 ring-blue-200' : over ? 'border-dashed border-blue-300' : 'border-blue-100'}`}
      data-testid={`kanban-col-${opt.key}`}
    >
      <div className="border-b border-gray-100 p-3">
        <div className="flex items-start gap-2">
          <input type="checkbox" checked={checked} onChange={() => onToggle(opt.key)} className="mt-0.5 h-4 w-4 rounded" data-testid={`proximity-check-${opt.key}`} />
          <div className="min-w-0 flex-1">
            <div className="text-sm font-semibold text-gray-900" data-testid={`kanban-name-${opt.key}`}>{opt.name}</div>
            <div className="truncate text-[11px] text-gray-500">{opt.meta.cities.join(' · ')}</div>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap gap-1 text-[10px]">
          <span className={`rounded px-1.5 py-0.5 ${opt.orders.length > maxStops ? 'bg-red-50 text-red-700' : 'bg-blue-50 text-blue-700'}`}>{opt.orders.length} OS</span>
          {!opt.edited && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-700">{opt.meta.distance_km} km</span>}
          {!opt.edited && <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-700">{fmtMin(opt.meta.travel_minutes)} rodando</span>}
          {!opt.edited && <span className="rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700">término ~{opt.meta.end_time}</span>}
          {opt.edited && <span className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700">editado · tempo recalculado ao criar</span>}
        </div>
        {opt.meta.neighborhoods.length > 0 && (
          <div className="mt-1.5 truncate text-[10px] text-gray-400">Bairros: {opt.meta.neighborhoods.join(', ')}</div>
        )}
        <label className="mt-2 flex items-center gap-1.5 text-[11px] text-gray-600">
          <User className="h-3.5 w-3.5 shrink-0" />
          <select value={tech || ''} onChange={(e) => onTech(opt.key, e.target.value)} className="h-7 flex-1 rounded border border-gray-200 bg-white px-1 text-xs" data-testid={`kanban-tech-${opt.key}`}>
            <option value="">Vincular técnico...</option>
            {technicians.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
      </div>
      <ol className="max-h-72 min-h-[60px] flex-1 space-y-1.5 overflow-y-auto p-2" data-testid={`kanban-list-${opt.key}`}>
        {opt.orders.map((o, i) => (
          <OrderCard key={o.os_number} o={o} i={i} colKey={opt.key} drag={drag} setDrag={setDrag} onDropAt={onDropAt} />
        ))}
        {opt.orders.length === 0 && <li className="py-4 text-center text-[11px] text-gray-400">Arraste OSs para cá</li>}
      </ol>
      <div className="border-t border-gray-100 p-2">
        <button type="button" onClick={() => onApply(opt)} disabled={!opt.orders.length} className="flex w-full items-center justify-center gap-1 rounded-md bg-gray-900 py-1.5 text-xs font-medium text-white transition-colors hover:bg-gray-700 disabled:opacity-40" data-testid={`proximity-apply-${opt.key}`}>
          <RouteIcon className="h-3.5 w-3.5" />Usar este roteiro
        </button>
        {!opt.edited && <div className="mt-1 text-center text-[9px] text-gray-400">tempo por {opt.meta.source}</div>}
      </div>
    </div>
  );
};

export const RouteSuggestionsKanban = ({ options, onChange, selected, onToggle, onApply, technicians = [], techByKey = {}, onTech, maxStops = 9 }) => {
  const [drag, setDrag] = useState(null);
  const onDropAt = (toKey, beforeOs) => {
    if (!drag || drag.os === beforeOs) { setDrag(null); return; }
    onChange(moveOrderBetween(options, drag.from, drag.os, toKey, beforeOs));
    setDrag(null);
  };
  return (
    <div className="flex snap-x gap-2 overflow-x-auto pb-1" data-testid="route-kanban">
      {options.map((opt) => (
        <KanbanColumn key={opt.key} opt={opt} checked={selected.has(opt.key)} onToggle={onToggle} onApply={onApply}
          technicians={technicians} tech={techByKey[opt.key]} onTech={onTech} drag={drag} setDrag={setDrag} onDropAt={onDropAt} maxStops={maxStops} />
      ))}
    </div>
  );
};
