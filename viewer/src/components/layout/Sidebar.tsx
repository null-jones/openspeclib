import SearchBar from '../search/SearchBar';
import CategoryFilter from '../search/CategoryFilter';
import SensorSelector from '../search/SensorSelector';
import ActiveFilters from '../search/ActiveFilters';

export default function Sidebar() {
  return (
    <aside className="w-80 flex-shrink-0 bg-white border-r border-gray-200 p-4 space-y-5 overflow-y-auto">
      <div>
        <h2 className="text-xs font-medium text-gray-500 uppercase tracking-wider mb-2">
          Search
        </h2>
        <SearchBar />
      </div>

      <SensorSelector />
      <CategoryFilter />

      <ActiveFilters />

      {/* Help text */}
      <div className="mt-6 p-3 bg-gray-50 rounded-lg border border-gray-100">
        <h3 className="text-xs font-semibold text-gray-600 mb-1">Quick Guide</h3>
        <ul className="text-xs text-gray-500 space-y-1">
          <li>Search by material name, keyword, or formula</li>
          <li>Filter by category to narrow results</li>
          <li>Select a sensor to filter for compatible spectra and enable downsampling</li>
          <li>Click table rows to add spectra to your library</li>
          <li>Export your library as CSV</li>
        </ul>
      </div>
    </aside>
  );
}
