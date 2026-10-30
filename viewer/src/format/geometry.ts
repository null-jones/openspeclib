/**
 * How each `geometry` vocabulary value breaks down into illumination and
 * viewing (Schaepman-Strub et al. 2006), for the diagrams, plus a plain
 * note on where each one comes up. The definitions themselves come from
 * vocabularies.json; values added there later still render, without a
 * diagram or note.
 */
export type Leg = 'directional' | 'conical' | 'hemispherical' | 'none' | 'unknown';

export const GEOMETRY_LEGS: Record<string, { illumination: Leg; view: Leg }> = {
  bidirectional: { illumination: 'directional', view: 'directional' },
  biconical: { illumination: 'conical', view: 'conical' },
  directional_hemispherical: { illumination: 'directional', view: 'hemispherical' },
  hemispherical_directional: { illumination: 'hemispherical', view: 'directional' },
  hemispherical_conical: { illumination: 'hemispherical', view: 'conical' },
  bihemispherical: { illumination: 'hemispherical', view: 'hemispherical' },
  directional: { illumination: 'none', view: 'directional' },
  unknown: { illumination: 'unknown', view: 'unknown' },
};

export const GEOMETRY_IN_PRACTICE: Record<string, string> = {
  bidirectional:
    'A goniometer, or a near-collimated setup with stated angles, lights the sample from one direction and measures from another. Each value belongs to one pair of angles. Lab spectra that older libraries call "bidirectional" are usually biconical.',
  biconical:
    'A lamp and a fore-optic, contact probe, leaf clip or FTIR reflectance attachment. Light and view each cover a small cone. Values are reflectance factors: the ratio to a white reference panel measured the same way.',
  directional_hemispherical:
    'A spectrometer with an integrating sphere. The beam comes from one direction and the sphere collects the light reflected into every direction, specular part included. The value is a true reflectance and does not depend on a view angle.',
  hemispherical_directional:
    'Outdoors under sun and sky light, viewed with a very narrow field of view. Values change with sun angle, sky conditions and view angle.',
  hemispherical_conical:
    'The usual field spectroradiometer measurement: sun and sky light, a bare fibre or fore-optic with a field of view of a few degrees. Values change with sun angle, sky conditions and view angle.',
  bihemispherical:
    'Light from every direction of the hemisphere, reflected light collected over the whole hemisphere: white-sky albedo. In this release it labels a few USGS laboratory measurements; elsewhere it is often a modelled or derived quantity.',
  directional:
    'An emitted quantity, such as emissivity, measured in one direction. There is no illumination geometry.',
  unknown:
    'The source does not say. Compare such spectra with others only with care, and do not guess a geometry when publishing.',
};
