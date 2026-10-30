import { useMemo } from 'react';
import { useAppContext } from '../../state/AppContext';
import { QUANTITIES, capitalise } from '../../format/vocab';
import { countBy } from '../../lib/data/search';

/** What the values measure (vocabulary `quantity`). */
export default function QuantityFilter() {
  const { state, dispatch } = useAppContext();
  const active = state.selectedQuantity;
  const sensorForcesReflectance = !!state.selectedSensor;
  const counts = useMemo(() => countBy(state.records, (r) => r.quantity), [state.records]);

  return (
    <div>
      <h3 id="filter-quantity" className="text-xs font-medium text-gray-600 uppercase tracking-wider">Quantity</h3>
      <p id="filter-counts-note" className="mt-0.5 text-[10px] text-gray-500">
        Numbers count all loaded spectra, before filtering.
      </p>
      <div className="flex flex-wrap gap-1.5 mt-1.5" role="group" aria-labelledby="filter-quantity" aria-describedby="filter-counts-note">
        {Object.keys(QUANTITIES).map((q) => {
          const isActive = active === q;
          const n = counts.get(q) ?? 0;
          const disabled = (sensorForcesReflectance && q !== 'reflectance') || (n === 0 && !isActive);
          return (
            <button
              key={q}
              disabled={disabled}
              onClick={() => dispatch({ type: 'SET_QUANTITY', quantity: isActive ? null : q })}
              aria-pressed={isActive}
              className={`px-2 py-0.5 text-xs rounded-full border transition-all
                ${isActive
                  ? 'bg-indigo-100 text-indigo-700 border-indigo-200 ring-1 ring-offset-1 ring-indigo-400'
                  : 'bg-white text-gray-500 border-gray-200 hover:bg-gray-50'}
                ${disabled ? 'opacity-40 cursor-not-allowed' : ''}`}
              title={sensorForcesReflectance && q !== 'reflectance' ? 'Sensor selection limits results to reflectance' : QUANTITIES[q]}
            >
              {capitalise(q)}
              {n > 0 && (
                <span className="ml-1 text-[10px] text-gray-500" aria-label={`${n.toLocaleString()} spectra in all`}>
                  {n.toLocaleString()}
                </span>
              )}
            </button>
          );
        })}
      </div>
      {sensorForcesReflectance && (
        <p className="mt-1 text-[10px] text-amber-600">Sensor selected — results are limited to reflectance spectra.</p>
      )}
    </div>
  );
}
