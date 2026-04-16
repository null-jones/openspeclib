import { useAppContext } from '../../state/AppContext';

const CATEGORIES = [
  { value: 'mineral', label: 'Mineral', color: 'bg-blue-100 text-blue-700 border-blue-200' },
  { value: 'rock', label: 'Rock', color: 'bg-amber-100 text-amber-700 border-amber-200' },
  { value: 'soil', label: 'Soil', color: 'bg-yellow-100 text-yellow-700 border-yellow-200' },
  { value: 'vegetation', label: 'Vegetation', color: 'bg-green-100 text-green-700 border-green-200' },
  { value: 'npv', label: 'NPV', color: 'bg-lime-100 text-lime-700 border-lime-200' },
  { value: 'water', label: 'Water', color: 'bg-cyan-100 text-cyan-700 border-cyan-200' },
  { value: 'snow_ice', label: 'Snow/Ice', color: 'bg-sky-100 text-sky-700 border-sky-200' },
  { value: 'man_made', label: 'Man-made', color: 'bg-purple-100 text-purple-700 border-purple-200' },
  { value: 'meteorite', label: 'Meteorite', color: 'bg-red-100 text-red-700 border-red-200' },
  { value: 'lunar', label: 'Lunar', color: 'bg-gray-100 text-gray-700 border-gray-200' },
  { value: 'organic_compound', label: 'Organic', color: 'bg-pink-100 text-pink-700 border-pink-200' },
  { value: 'mixture', label: 'Mixture', color: 'bg-orange-100 text-orange-700 border-orange-200' },
  { value: 'other', label: 'Other', color: 'bg-gray-100 text-gray-600 border-gray-200' },
];

export default function CategoryFilter() {
  const { state, dispatch } = useAppContext();

  return (
    <div>
      <label className="text-xs font-medium text-gray-500 uppercase tracking-wider">
        Categories
      </label>
      <div className="flex flex-wrap gap-1.5 mt-1.5">
        {CATEGORIES.map((cat) => {
          const active = state.selectedCategories.includes(cat.value);
          return (
            <button
              key={cat.value}
              onClick={() => dispatch({ type: 'TOGGLE_CATEGORY', category: cat.value })}
              className={`px-2 py-0.5 text-xs rounded-full border transition-all
                ${active
                  ? cat.color + ' ring-1 ring-offset-1 ring-indigo-400'
                  : 'bg-white text-gray-500 border-gray-200 hover:bg-gray-50'
                }`}
            >
              {cat.label}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export { CATEGORIES };
