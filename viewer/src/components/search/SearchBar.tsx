import { useAppContext } from '../../state/AppContext';

export default function SearchBar() {
  const { state, dispatch } = useAppContext();

  return (
    <div className="relative">
      <svg
        className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400"
        fill="none" viewBox="0 0 24 24" stroke="currentColor"
      >
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
          d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
      </svg>
      <input
        type="text"
        placeholder="Search materials, keywords, formulas..."
        value={state.searchText}
        onChange={(e) => dispatch({ type: 'SET_SEARCH_TEXT', text: e.target.value })}
        className="w-full pl-10 pr-4 py-2.5 text-sm bg-white border border-gray-200 rounded-lg
                   focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent
                   placeholder-gray-400"
      />
      {state.searchText && (
        <button
          onClick={() => dispatch({ type: 'SET_SEARCH_TEXT', text: '' })}
          className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
        >
          <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      )}
    </div>
  );
}
