/** Sections of the Format reference, in page order (ids are used in the URL hash). */
export const FORMAT_SECTIONS = [
  { id: 'overview', title: 'What the format is' },
  { id: 'columns', title: 'Core columns' },
  { id: 'extensions', title: 'Extensions and other columns' },
  { id: 'vocabularies', title: 'Vocabularies' },
  { id: 'geometry', title: 'Measurement geometry' },
  { id: 'footer', title: 'Footer metadata' },
  { id: 'index', title: 'Index files' },
  { id: 'rules', title: 'Conformance rules' },
  { id: 'reading', title: 'Reading files' },
  { id: 'publishing', title: 'Publish your own library' },
] as const;

export type FormatSectionId = (typeof FORMAT_SECTIONS)[number]['id'];
