import { useAppContext } from '../../state/AppContext';
import { DEFAULT_WAVELENGTH_RANGE } from '../../state/types';
import { collectionLabel } from '../../constants/sources';
import { vocabLabel } from '../../format/vocab';

export default function ActiveFilters() {
  const { state, dispatch } = useAppContext();

  const wavelengthFiltered =
    state.wavelengthRange[0] !== DEFAULT_WAVELENGTH_RANGE[0] ||
    state.wavelengthRange[1] !== DEFAULT_WAVELENGTH_RANGE[1];

  const hasFilters =
    state.searchText ||
    state.selectedCategories.length > 0 ||
    state.selectedSources.length > 0 ||
    state.selectedQuantity ||
    state.selectedGeometries.length > 0 ||
    state.hideSimulated ||
    wavelengthFiltered ||
    state.selectedSensor;

  if (!hasFilters) return null;

  return (
    <div className="flex flex-wrap gap-1.5 items-center">
      <span className="text-xs text-gray-400">Active:</span>
      {state.searchText && (
        <Tag
          label={`"${state.searchText}"`}
          onRemove={() => dispatch({ type: 'SET_SEARCH_TEXT', text: '' })}
        />
      )}
      {state.selectedSources.map((s) => (
        <Tag
          key={`src-${s}`}
          label={collectionLabel(s, state.collections.get(s)?.footer.collection.title)}
          onRemove={() => dispatch({ type: 'TOGGLE_SOURCE', source: s })}
        />
      ))}
      {state.selectedCategories.map((c) => (
        <Tag
          key={c}
          label={c}
          onRemove={() => dispatch({ type: 'TOGGLE_CATEGORY', category: c })}
        />
      ))}
      {state.selectedQuantity && (
        <Tag
          label={state.selectedQuantity}
          onRemove={() => dispatch({ type: 'SET_QUANTITY', quantity: null })}
        />
      )}
      {state.selectedGeometries.map((g) => (
        <Tag key={`geo-${g}`} label={vocabLabel(g)} onRemove={() => dispatch({ type: 'TOGGLE_GEOMETRY', geometry: g })} />
      ))}
      {state.hideSimulated && (
        <Tag label="no simulated" onRemove={() => dispatch({ type: 'SET_HIDE_SIMULATED', hide: false })} />
      )}
      {wavelengthFiltered && (
        <Tag
          label={`${state.wavelengthRange[0]}–${state.wavelengthRange[1]} μm`}
          onRemove={() =>
            dispatch({ type: 'SET_WAVELENGTH_RANGE', range: DEFAULT_WAVELENGTH_RANGE })
          }
        />
      )}
      {state.selectedSensor && (
        <Tag
          label={state.selectedSensor.name}
          onRemove={() => dispatch({ type: 'SET_SENSOR', sensor: null })}
        />
      )}
      <button
        onClick={() => dispatch({ type: 'CLEAR_FILTERS' })}
        className="text-xs text-gray-400 hover:text-red-500 underline decoration-dotted"
      >
        Clear all
      </button>
    </div>
  );
}

function Tag({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs bg-indigo-50 text-indigo-700 rounded-full">
      {label}
      <button onClick={onRemove} className="hover:text-indigo-900">
        <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </span>
  );
}
