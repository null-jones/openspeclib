import { useAppContext } from '../../state/AppContext';

export default function ActiveFilters() {
  const { state, dispatch } = useAppContext();

  const hasFilters =
    state.searchText ||
    state.selectedCategories.length > 0 ||
    state.selectedSources.length > 0 ||
    state.selectedTechnique ||
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
          label={s}
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
      {state.selectedTechnique && (
        <Tag
          label={state.selectedTechnique}
          onRemove={() => dispatch({ type: 'SET_TECHNIQUE', technique: null })}
        />
      )}
      {state.selectedSensor && (
        <Tag
          label={state.selectedSensor.name}
          onRemove={() => dispatch({ type: 'SET_SENSOR', sensor: null })}
        />
      )}
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
