import { useMemo } from 'react';
import { useAppContext } from '../../state/AppContext';
import { GEOMETRIES, vocabLabel } from '../../format/vocab';
import { countBy } from '../../lib/data/search';

/** Measurement geometry (vocabulary `geometry`), and hiding simulated spectra. */
export default function GeometryFilter() {
  const { state, dispatch } = useAppContext();
  const counts = useMemo(() => countBy(state.records, (r) => r.geometry), [state.records]);
  const simulated = useMemo(() => state.records.filter((r) => r.simulated).length, [state.records]);
  // Values present in the data first, then the rest of the vocabulary.
  const values = Object.keys(GEOMETRIES).filter((g) => (counts.get(g) ?? 0) > 0 || state.selectedGeometries.includes(g));

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <h3 id="filter-geometry" className="text-xs font-medium text-gray-600 uppercase tracking-wider">Geometry</h3>
        <button
          onClick={() => dispatch({ type: 'SET_VIEW', view: 'format', page: 'reference', section: 'geometry' })}
          className="text-[10px] text-indigo-500 hover:text-indigo-700"
        >
          What is this?
        </button>
      </div>
      <p className="mt-0.5 text-[10px] text-gray-500">Numbers count all loaded spectra, before filtering.</p>
      <div className="flex flex-wrap gap-1.5 mt-1.5" role="group" aria-labelledby="filter-geometry">
        {values.map((g) => {
          const active = state.selectedGeometries.includes(g);
          return (
            <button
              key={g}
              onClick={() => dispatch({ type: 'TOGGLE_GEOMETRY', geometry: g })}
              title={GEOMETRIES[g]}
              aria-pressed={active}
              className={`px-2 py-0.5 text-xs rounded-full border transition-all
                ${active
                  ? 'bg-indigo-100 text-indigo-700 border-indigo-200 ring-1 ring-offset-1 ring-indigo-400'
                  : 'bg-white text-gray-500 border-gray-200 hover:bg-gray-50'}`}
            >
              {vocabLabel(g)}
              <span className="ml-1 text-[10px] text-gray-500" aria-label={`${(counts.get(g) ?? 0).toLocaleString()} spectra in all`}>
                {(counts.get(g) ?? 0).toLocaleString()}
              </span>
            </button>
          );
        })}
      </div>
      {values.length === 1 && values[0] === 'unknown' && (
        <p className="mt-1 text-[10px] text-gray-500">The loaded collections do not state their geometry yet.</p>
      )}
      {simulated > 0 && (
        <label className="mt-2 flex items-center gap-2 text-xs text-gray-600 cursor-pointer">
          <input
            type="checkbox"
            checked={state.hideSimulated}
            onChange={(e) => dispatch({ type: 'SET_HIDE_SIMULATED', hide: e.target.checked })}
            className="w-3.5 h-3.5 rounded text-indigo-600"
          />
          Hide simulated spectra <span className="text-gray-400">({simulated.toLocaleString()})</span>
        </label>
      )}
    </div>
  );
}
