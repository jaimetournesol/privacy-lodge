import { useEffect, useRef, useState } from 'react';
import type { SurfaceComponent } from '../../shared/protocol';
import type { EmitFn } from './index';

interface Field {
  key: string;
  label?: string;
  kind?: 'text' | 'textarea' | 'number' | 'select' | 'checkbox' | 'slider';
  options?: string[];
  min?: number;
  max?: number;
  step?: number;
  value?: unknown;
  placeholder?: string;
}

export function FormPanel({ comp, emit }: { comp: SurfaceComponent; emit: EmitFn }) {
  const p = comp.props as { fields?: Field[]; submitLabel?: string };
  const fields = p.fields ?? [];
  const defString = JSON.stringify(fields.map((f) => ({ ...f, value: undefined })));
  const lastDef = useRef('');
  const [values, setValues] = useState<Record<string, unknown>>({});
  const debounce = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Re-seed local values only when the field DEFINITION changes — not when the
  // hub echoes our own live-value sync back as a props update.
  useEffect(() => {
    if (defString !== lastDef.current) {
      lastDef.current = defString;
      const seed: Record<string, unknown> = {};
      for (const f of fields) seed[f.key] = f.value ?? (f.kind === 'checkbox' ? false : f.kind === 'slider' ? f.min ?? 0 : '');
      setValues(seed);
    }
  }, [defString]); // eslint-disable-line react-hooks/exhaustive-deps

  const setValue = (key: string, v: unknown) => {
    setValues((prev) => {
      const next = { ...prev, [key]: v };
      clearTimeout(debounce.current);
      debounce.current = setTimeout(() => emit(comp.id, 'change', { values: next }), 400);
      return next;
    });
  };

  return (
    <form
      className="form"
      onSubmit={(e) => {
        e.preventDefault();
        clearTimeout(debounce.current);
        emit(comp.id, 'submit', { values });
      }}
    >
      {fields.map((f) => (
        <label key={f.key} className={`field field-${f.kind ?? 'text'}`}>
          {f.kind !== 'checkbox' && <span className="field-label">{f.label ?? f.key}</span>}
          {renderInput(f, values[f.key], (v) => setValue(f.key, v))}
          {f.kind === 'checkbox' && <span className="field-label">{f.label ?? f.key}</span>}
        </label>
      ))}
      <div className="form-actions">
        <button type="submit" className="primary-btn">
          {p.submitLabel ?? 'Submit'}
        </button>
      </div>
    </form>
  );
}

function renderInput(f: Field, value: unknown, set: (v: unknown) => void) {
  switch (f.kind) {
    case 'textarea':
      return <textarea value={String(value ?? '')} placeholder={f.placeholder} rows={4} onChange={(e) => set(e.target.value)} />;
    case 'number':
      return <input type="number" value={String(value ?? '')} min={f.min} max={f.max} step={f.step} onChange={(e) => set(e.target.value === '' ? '' : Number(e.target.value))} />;
    case 'select':
      return (
        <select value={String(value ?? '')} onChange={(e) => set(e.target.value)}>
          <option value="" disabled hidden>
            {f.placeholder ?? 'choose…'}
          </option>
          {(f.options ?? []).map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </select>
      );
    case 'checkbox':
      return <input type="checkbox" checked={Boolean(value)} onChange={(e) => set(e.target.checked)} />;
    case 'slider':
      return (
        <span className="slider-row">
          <input type="range" min={f.min ?? 0} max={f.max ?? 100} step={f.step ?? 1} value={Number(value ?? f.min ?? 0)} onChange={(e) => set(Number(e.target.value))} />
          <span className="mono slider-val">{String(value ?? f.min ?? 0)}</span>
        </span>
      );
    default:
      return <input type="text" value={String(value ?? '')} placeholder={f.placeholder} onChange={(e) => set(e.target.value)} />;
  }
}
