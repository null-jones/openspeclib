import { useState } from 'react';
import { useAppContext } from '../../state/AppContext';
import { DEFAULT_WAVELENGTH_RANGE } from '../../state/types';

/** Common spectral windows, in micrometers. */
const PRESETS: { label: string; range: [number, number] }[] = [
  { label: 'VNIR', range: [0.4, 1.0] },
  { label: 'SWIR', range: [1.0, 2.5] },
  { label: 'MWIR', range: [3, 5] },
  { label: 'TIR', range: [8, 14] },
];

export default function WavelengthRangeFilter() {
  const { state, dispatch } = useAppContext();
  const [min, max] = state.wavelengthRange;
  // Local text state so partially-typed numbers don't thrash the filter,
  // reset during render whenever the committed range changes elsewhere
  // (presets, ActiveFilters removal, URL hydration).
  const [minText, setMinText] = useState(String(min));
  const [maxText, setMaxText] = useState(String(max));
  const [prevRange, setPrevRange] = useState<[number, number]>([min, max]);
  if (prevRange[0] !== min || prevRange[1] !== max) {
    setPrevRange([min, max]);
    setMinText(String(min));
    setMaxText(String(max));
  }

  const commit = (loText: string, hiText: string) => {
    const lo = parseFloat(loText);
    const hi = parseFloat(hiText);
    if (Number.isFinite(lo) && Number.isFinite(hi) && lo > 0 && lo < hi) {
      dispatch({ type: 'SET_WAVELENGTH_RANGE', range: [lo, hi] });
    }
  };

  const isDefault =
    min === DEFAULT_WAVELENGTH_RANGE[0] && max === DEFAULT_WAVELENGTH_RANGE[1];

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <h3 id="filter-wavelength" className="text-xs font-medium text-gray-600 uppercase tracking-wider">
          Wavelength Range <span className="normal-case">(μm)</span>
        </h3>
        {!isDefault && (
          <button
            onClick={() =>
              dispatch({ type: 'SET_WAVELENGTH_RANGE', range: DEFAULT_WAVELENGTH_RANGE })
            }
            className="text-[10px] text-indigo-500 hover:text-indigo-700"
          >
            Reset
          </button>
        )}
      </div>
      <div className="flex items-center gap-2 mt-1.5">
        <input
          type="number"
          min={0}
          step={0.1}
          value={minText}
          onChange={(e) => setMinText(e.target.value)}
          onBlur={() => commit(minText, maxText)}
          onKeyDown={(e) => e.key === 'Enter' && commit(minText, maxText)}
          aria-label="Minimum wavelength (micrometers)"
          className="w-full px-2 py-1.5 text-xs bg-white border border-gray-200 rounded-md
                     focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        <span className="text-xs text-gray-400">–</span>
        <input
          type="number"
          min={0}
          step={0.1}
          value={maxText}
          onChange={(e) => setMaxText(e.target.value)}
          onBlur={() => commit(minText, maxText)}
          onKeyDown={(e) => e.key === 'Enter' && commit(minText, maxText)}
          aria-label="Maximum wavelength (micrometers)"
          className="w-full px-2 py-1.5 text-xs bg-white border border-gray-200 rounded-md
                     focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
      </div>
      <div className="flex flex-wrap gap-1.5 mt-1.5" role="group" aria-labelledby="filter-wavelength">
        {PRESETS.map((p) => {
          const active = min === p.range[0] && max === p.range[1];
          return (
            <button
              key={p.label}
              onClick={() => dispatch({ type: 'SET_WAVELENGTH_RANGE', range: p.range })}
              aria-pressed={active}
              className={`px-2 py-0.5 text-[10px] rounded-full border transition-all
                ${active
                  ? 'bg-indigo-100 text-indigo-700 border-indigo-200'
                  : 'bg-white text-gray-500 border-gray-200 hover:bg-gray-50'}`}
              title={`${p.range[0]}–${p.range[1]} μm`}
            >
              {p.label}
            </button>
          );
        })}
      </div>
      <p className="mt-1 text-[10px] text-gray-500">
        Keeps spectra whose coverage overlaps this range.
      </p>
    </div>
  );
}
