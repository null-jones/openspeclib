const CATEGORY_COLORS: Record<string, string> = {
  mineral: 'bg-blue-100 text-blue-700',
  rock: 'bg-amber-100 text-amber-700',
  soil: 'bg-yellow-100 text-yellow-700',
  vegetation: 'bg-green-100 text-green-700',
  npv: 'bg-lime-100 text-lime-700',
  water: 'bg-cyan-100 text-cyan-700',
  snow_ice: 'bg-sky-100 text-sky-700',
  man_made: 'bg-purple-100 text-purple-700',
  meteorite: 'bg-red-100 text-red-700',
  lunar: 'bg-gray-100 text-gray-700',
  organic_compound: 'bg-pink-100 text-pink-700',
  mixture: 'bg-orange-100 text-orange-700',
  other: 'bg-gray-100 text-gray-600',
};

export default function Badge({ category }: { category: string }) {
  const color = CATEGORY_COLORS[category] ?? 'bg-gray-100 text-gray-600';
  return (
    <span className={`px-1.5 py-0.5 text-xs rounded-md font-medium ${color}`}>
      {category.replace(/_/g, ' ')}
    </span>
  );
}
