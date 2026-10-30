/**
 * Small schematic of an illumination/viewing geometry: light (amber) comes
 * in from the left, the view (indigo) leaves to the right. Directional =
 * one ray, conical = a narrow cone, hemispherical = the whole dome.
 */
import { GEOMETRY_LEGS, type Leg } from '../../format/geometry';
import { own } from '../../lib/own';

const CX = 80;
const CY = 78;
const AMBER = '#d97706';
const INDIGO = '#4f46e5';

function Arrow({ x1, y1, x2, y2, color }: { x1: number; y1: number; x2: number; y2: number; color: string }) {
  const a = Math.atan2(y2 - y1, x2 - x1);
  const h = 6;
  const p1 = `${x2 - h * Math.cos(a - 0.45)},${y2 - h * Math.sin(a - 0.45)}`;
  const p2 = `${x2 - h * Math.cos(a + 0.45)},${y2 - h * Math.sin(a + 0.45)}`;
  return (
    <g>
      <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={color} strokeWidth={1.8} strokeLinecap="round" />
      <polygon points={`${x2},${y2} ${p1} ${p2}`} fill={color} />
    </g>
  );
}

/** Point at `deg` degrees from the surface normal on a circle of radius r (negative = left). */
function polar(deg: number, r: number): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [CX + r * Math.sin(rad), CY - r * Math.cos(rad)];
}

/** A dashed dome with rays at `angles`, from radius `inner` to `outer`. */
function Dome({ r, color, inward, angles, inner }: { r: number; color: string; inward: boolean; angles: number[]; inner: number }) {
  return (
    <g>
      <path
        d={`M ${CX - r} ${CY} A ${r} ${r} 0 0 1 ${CX + r} ${CY}`}
        fill={color}
        fillOpacity={0.06}
        stroke={color}
        strokeWidth={1.2}
        strokeDasharray="3 3"
      />
      {angles.map((deg) => {
        const [ox, oy] = polar(deg, r - 4);
        const [ix, iy] = polar(deg, inner);
        return inward ? (
          <Arrow key={deg} x1={ox} y1={oy} x2={ix} y2={iy} color={color} />
        ) : (
          <Arrow key={deg} x1={ix} y1={iy} x2={ox} y2={oy} color={color} />
        );
      })}
    </g>
  );
}

function Cone({ deg, color, inward }: { deg: number; color: string; inward: boolean }) {
  const [ax, ay] = polar(deg - 9, 62);
  const [bx, by] = polar(deg + 9, 62);
  const [mx, my] = polar(deg, 60);
  const [nx, ny] = polar(deg, 8);
  return (
    <g>
      <polygon points={`${CX},${CY} ${ax},${ay} ${bx},${by}`} fill={color} fillOpacity={0.15} stroke={color} strokeWidth={0.8} />
      {inward ? <Arrow x1={mx} y1={my} x2={nx} y2={ny} color={color} /> : <Arrow x1={nx} y1={ny} x2={mx} y2={my} color={color} />}
    </g>
  );
}

function Ray({ deg, color, inward }: { deg: number; color: string; inward: boolean }) {
  const [fx, fy] = polar(deg, 62);
  const [nx, ny] = polar(deg, 6);
  return inward ? <Arrow x1={fx} y1={fy} x2={nx} y2={ny} color={color} /> : <Arrow x1={nx} y1={ny} x2={fx} y2={fy} color={color} />;
}

// Ray angles (degrees from the normal) chosen so that rays of the two sides
// never sit on top of each other.
const ILLUM_DEG = -40;
const VIEW_DEG = 35;
const ILLUM_DOME = [-66, -38, -10, 16, 62];
const VIEW_DOME = [-52, -24, 2, 30, 58];

function LegShape({ leg, side, bothDomes }: { leg: Leg; side: 'illumination' | 'view'; bothDomes: boolean }) {
  const illum = side === 'illumination';
  const color = illum ? AMBER : INDIGO;
  const deg = illum ? ILLUM_DEG : VIEW_DEG;
  switch (leg) {
    case 'directional':
      return <Ray deg={deg} color={color} inward={illum} />;
    case 'conical':
      return <Cone deg={deg} color={color} inward={illum} />;
    case 'hemispherical':
      return illum ? (
        <Dome r={70} color={color} inward angles={ILLUM_DOME} inner={bothDomes ? 44 : 14} />
      ) : (
        <Dome r={56} color={color} inward={false} angles={VIEW_DOME} inner={12} />
      );
    default:
      return null;
  }
}

export default function GeometryDiagram({ geometry }: { geometry: string }) {
  const legs = own(GEOMETRY_LEGS, geometry) ?? { illumination: 'unknown', view: 'unknown' };
  const both = legs.illumination === 'hemispherical' && legs.view === 'hemispherical';
  const label =
    legs.illumination === 'unknown'
      ? 'Geometry not stated'
      : `${legs.illumination === 'none' ? 'No illumination' : `${legs.illumination} illumination`}, ${legs.view} view`;
  return (
    <svg viewBox="0 0 160 92" className="w-full h-auto" role="img" aria-label={label}>
      {/* Draw hemispheres first so rays stay on top. */}
      {legs.illumination === 'hemispherical' && <LegShape leg="hemispherical" side="illumination" bothDomes={both} />}
      {legs.view === 'hemispherical' && <LegShape leg="hemispherical" side="view" bothDomes={both} />}
      {legs.illumination !== 'hemispherical' && <LegShape leg={legs.illumination} side="illumination" bothDomes={both} />}
      {legs.view !== 'hemispherical' && <LegShape leg={legs.view} side="view" bothDomes={both} />}
      {legs.illumination === 'unknown' && (
        <text x={CX} y={46} textAnchor="middle" fontSize={26} fill="#9ca3af" fontWeight={600}>
          ?
        </text>
      )}
      {/* Sample surface */}
      <line x1={18} y1={CY} x2={142} y2={CY} stroke="#6b7280" strokeWidth={1.5} />
      {Array.from({ length: 13 }, (_, i) => (
        <line key={i} x1={22 + i * 10} y1={CY + 1} x2={16 + i * 10} y2={CY + 8} stroke="#d1d5db" strokeWidth={1} />
      ))}
    </svg>
  );
}
