/**
 * The OpenSpecLib Format reference, generated from schemas/format-v1/*.json
 * (columns, vocabularies, footer schema, rules). Only the explanations around
 * the tables are written here.
 */
import type { ReactNode } from 'react';
import {
  ANY_FILE_COLUMNS,
  COLLECTION_ID_PATTERN,
  CORE_COLUMNS,
  EXTENSION_COLUMNS,
  EXTENSIONS,
  FOOTER_KEY,
  FOOTER_SCHEMA,
  FORMAT_VERSION,
  INDEX_FOOTER_KEY,
  INDEX_ONLY_COLUMNS,
  LOCAL_ID_PATTERN,
  QA_BOUNDS,
  QA_WAVELENGTH_NM,
  RULES,
  VOCABULARIES,
  type ColumnSpec,
  type RuleLevel,
} from '../../format/spec';
import { EXAMPLE_FILE, EXAMPLE_FOOTER, EXAMPLE_ID, READING_SNIPPETS, WRITE_PYARROW } from '../../format/examples';
import { GEOMETRY_IN_PRACTICE } from '../../format/geometry';
import { COLLECTIONS_URL, DOCS_URL, GITHUB, SCHEMAS_URL, SPEC_URL } from '../../format/links';
import { Code, CodeBlock, ExtLink, Prose, Section, SubHeading, Table } from './common';
import { MAX_LICENCE_NESTING } from '../../lib/conformance/license';
import GeometryDiagram from './GeometryDiagram';

const VOCAB_COLUMN_LINK: Record<string, string> = {
  material_category: 'vocabularies',
  quantity: 'vocabularies',
  geometry: 'geometry',
  wavelength_unit: 'vocabularies',
};

function TypeBadge({ type }: { type: string }) {
  return <span className="font-mono text-[11px] text-indigo-700 whitespace-nowrap">{type}</span>;
}

function columnRows(cols: ColumnSpec[], showNullability: boolean): ReactNode[][] {
  return cols.map((c) => {
    const row: ReactNode[] = [
      <span className="font-mono font-medium text-gray-900 whitespace-nowrap">{c.name}</span>,
      <TypeBadge type={c.type} />,
    ];
    if (showNullability) {
      row.push(
        <span className="whitespace-nowrap text-gray-500">
          {c.nullable ? 'may be null' : 'not null'}
          {c.non_empty ? ', not empty' : ''}
        </span>,
      );
    }
    row.push(<Prose text={c.description} />);
    return row;
  });
}

// --- JSON Schema helpers for the footer tables ----------------------------

interface SchemaNode {
  type?: string;
  $ref?: string;
  enum?: unknown[];
  pattern?: string;
  minLength?: number;
  minimum?: number;
  items?: SchemaNode;
  description?: string;
  required?: string[];
  properties?: Record<string, SchemaNode>;
  additionalProperties?: SchemaNode | boolean;
}

const DEFS = (FOOTER_SCHEMA as { $defs: Record<string, SchemaNode> }).$defs;

const REF_LABELS: Record<string, string> = {
  nonEmptyString: 'string, not empty',
  formatVersion: 'string, 1.x.y',
  collectionId: 'collection id',
  license: 'licence expression',
  dateTime: 'RFC 3339 timestamp',
  collection: 'object (below)',
  dataset: 'dataset (below)',
  licenseRef: 'licence definition (below)',
  collectionFooter: 'collection footer',
  extensions: 'list of extension names',
};

/** Short type label for a schema node, plus its pattern if it has one. */
function describe(node: SchemaNode): { label: string; pattern?: string } {
  if (node.$ref) {
    const name = node.$ref.split('/').pop()!;
    const target = DEFS[name];
    const hidePattern = name === 'formatVersion' || name === 'dateTime';
    return { label: REF_LABELS[name] ?? name, pattern: hidePattern ? undefined : target?.pattern };
  }
  if (node.enum) return { label: node.enum.map((v) => JSON.stringify(v)).join(' | ') };
  if (node.type === 'array') return { label: `list of ${node.items ? describe(node.items).label : 'values'}` };
  if (node.type === 'object' && node.additionalProperties && typeof node.additionalProperties === 'object') {
    return { label: `map of ${describe(node.additionalProperties).label}` };
  }
  return { label: `${node.type ?? 'any'}${node.minimum !== undefined ? `, ≥ ${node.minimum}` : ''}`, pattern: node.pattern };
}

function TypeCell({ node }: { node: SchemaNode }) {
  const d = describe(node);
  return (
    <span className="text-gray-600">
      {d.label}
      {d.pattern && <span className="block font-mono text-[10px] text-gray-500 break-all">{d.pattern}</span>}
    </span>
  );
}

function propertyRows(def: SchemaNode, notes: Record<string, string> = {}): ReactNode[][] {
  const required = new Set(def.required ?? []);
  return Object.entries(def.properties ?? {}).map(([name, node]) => [
    <span className="font-mono font-medium text-gray-900 whitespace-nowrap">{name}</span>,
    <TypeCell node={node} />,
    required.has(name) ? <span className="text-gray-900">yes</span> : <span className="text-gray-400">no</span>,
    <Prose text={notes[name] ?? node.description ?? ''} />,
  ]);
}

const FOOTER_NOTES: Record<string, string> = {
  format_version: 'Version of this specification the file follows.',
  collection: 'Identity, licence and citation of the collection.',
  datasets: 'For aggregated collections: one entry per contributing dataset, keyed by the `dataset_id` used in rows.',
  extensions: 'Optional column groups the file may contain.',
  created: 'When the file was written, with a time zone, e.g. `2026-10-29T12:00:00Z`.',
  generator: 'Software that wrote the file, e.g. `openspeclib 0.2.0`.',
};

const COLLECTION_NOTES: Record<string, string> = {
  id: 'Also the prefix of every row `id`.',
  license:
    'Licence of the collection as a whole. Use `NOASSERTION` when rows carry different licences, and explain in `license_note`; the row `license` column is authoritative.',
  license_refs: 'Defines every `LicenseRef-` identifier used anywhere in the file.',
  inclusion: '`complete`: everything the source publishes. `subset`: a deliberate selection.',
  inclusion_notes: 'Required when `inclusion` is `subset`: how the selection was made.',
};

const LEVELS: Array<{ level: RuleLevel; title: string; blurb: string }> = [
  { level: 'must', title: 'Required', blurb: 'A file conforms when it meets every one of these.' },
  { level: 'should', title: 'Recommended', blurb: 'Checkers report these as warnings. They make files smaller and faster to read over HTTP.' },
  {
    level: 'qa',
    title: 'OpenSpecLib quality profile',
    blurb: 'Extra checks the OpenSpecLib collection applies on top of conformance. Other publishers may adopt them.',
  },
];

export default function FormatReference({ onOpenChecker, onGoto }: { onOpenChecker: () => void; onGoto: (id: string) => void }) {
  const sectionLink = (id: string, children: ReactNode) => (
    <button onClick={() => onGoto(id)} className="text-indigo-600 hover:text-indigo-800 underline">
      {children}
    </button>
  );

  return (
    <div className="space-y-4">
      <Section id="overview" title={`OpenSpecLib Format ${FORMAT_VERSION}`}>
        <p className="text-xs text-gray-700 bg-gray-50 border border-gray-200 rounded px-2.5 py-1.5">
          Version {FORMAT_VERSION}, accepted through RFC 0001. The normative text is{' '}
          <ExtLink href={SPEC_URL}>docs/spec/format-v1.md</ExtLink>; this page is built from its machine-readable parts in{' '}
          <ExtLink href={SCHEMAS_URL}>schemas/format-v1</ExtLink>.
        </p>
        <p>
          The OpenSpecLib Format stores a spectral library in one Apache Parquet file per collection. It is meant for
          anyone who publishes spectra, whether or not their data is part of OpenSpecLib.
        </p>
        <ul className="list-disc pl-5 space-y-1.5">
          <li>
            <strong>Self-describing.</strong> One file holds the spectra, their wavelength axes, and the collection's
            provenance and licence (in the Parquet footer).
          </li>
          <li>
            <strong>Any Parquet tool reads it.</strong> Columns are flat and plainly typed, so DuckDB, pandas, Polars,
            pyarrow, R <Code>arrow</Code>, Spark and DuckDB-WASM all work without a plugin.
          </li>
          <li>
            <strong>Says what was measured.</strong> Each spectrum carries its <Code>quantity</Code> (reflectance,
            transmittance, emissivity, absorbance) and its measurement {sectionLink('geometry', 'geometry')}.
          </li>
          <li>
            <strong>Native units.</strong> Wavelengths stay in the unit the source published (nm, µm or cm⁻¹), so no
            resampling error creeps in. Two extra columns give every axis's extent in nm for searching.
          </li>
          <li>
            <strong>NaN, and only NaN, marks a missing value.</strong> No −1.23e34, −9999 or 0 standing in for a
            missing band.
          </li>
        </ul>
        <p>
          A file has one row per spectrum. The wavelength axis and the values are list columns, so one row is one
          complete spectrum. Optional {sectionLink('extensions', 'extensions')} add columns for sample, measurement and
          other details. An optional {sectionLink('index', 'index file')} lets readers search several collections
          without reading their arrays.
        </p>
        <p>
          To check a file of your own,{' '}
          <button onClick={onOpenChecker} className="text-indigo-600 hover:text-indigo-800 underline">
            open the checker
          </button>
          . It runs in your browser.
        </p>
      </Section>

      <Section id="columns" title="Core columns">
        <p>
          Every collection file has these columns. Only <Code>dataset_id</Code> may be null, and the text columns marked
          below hold no empty or whitespace-only strings. String lists (<Code>material_keywords</Code>,{' '}
          <Code>measurement_processing</Code>) hold no null elements. A column's type is the one its Parquet logical type implies:
          hints some writers add in the <Code>ARROW:schema</Code> footer entry (dictionary encoding,{' '}
          <Code>fixed_size_list</Code>, <Code>large_string</Code>, <Code>large_list</Code>, view types,{' '}
          <Code>date64</Code>) do not change it, and both the 3-level and the legacy 2-level Parquet list encodings are
          accepted, so files from every Parquet library get the same verdict. A nullable column whose values are all null
          may use the Arrow <Code>null</Code> type.
        </p>
        <Table head={['Column', 'Type', 'Values', 'Content']} rows={columnRows(CORE_COLUMNS, true)} />
        <p>
          <strong>Ids.</strong> <Code>id</Code> is <Code>{'{collection}:{local_id}'}</Code>, where the local id matches{' '}
          <Code>{LOCAL_ID_PATTERN}</Code> and the collection id matches <Code>{COLLECTION_ID_PATTERN}</Code>. Ids are
          unique within a file. Local ids are opaque: <Code>original_id</Code> is the authoritative upstream
          identifier.
        </p>
        <p>
          <strong>Values.</strong> Reflectance, transmittance and emissivity are fractions with a nominal range of 0 to 1:
          convert percentages and scaled integers before writing. Do not clip small negative values or reflectance
          factors above 1. Absorbance is decadic, A = log₁₀(1/x).
        </p>
        <p>
          <strong>Wavelengths.</strong> The axis has at least two points, is strictly increasing or strictly
          decreasing, and every point is finite and above 0. <Code>wavelength_min_nm</Code> and{' '}
          <Code>wavelength_max_nm</Code> are its extent in nm: µm × 1000, and 10⁷ / ν for wavenumbers in cm⁻¹.
        </p>
        <p>
          <strong>Vocabularies.</strong>{' '}
          {Object.keys(VOCAB_COLUMN_LINK).map((c, i, all) => (
            <span key={c}>
              {i > 0 && (i === all.length - 1 ? ' and ' : ', ')}
              {sectionLink(VOCAB_COLUMN_LINK[c], c)}
            </span>
          ))}{' '}
          take their values from controlled vocabularies.
        </p>
      </Section>

      <Section id="extensions" title="Extensions and other columns">
        <p>
          Extensions are optional groups of columns. A file may contain a column from an extension only if its footer
          lists that extension in <Code>extensions</Code>. A file that declares an extension need not contain all of its
          columns. All extension columns are nullable.
        </p>
        {EXTENSIONS.map((ext) => (
          <div key={ext} className="space-y-1.5">
            <SubHeading>
              <span className="font-mono normal-case">{ext}</span>
              <span className="ml-2 normal-case font-normal tracking-normal text-gray-500">{VOCABULARIES.extension[ext]}</span>
            </SubHeading>
            <Table head={['Column', 'Type', 'Content']} rows={columnRows(EXTENSION_COLUMNS.filter((c) => c.extension === ext), false)} />
          </div>
        ))}
        <SubHeading>Other columns</SubHeading>
        <Table head={['Column', 'Type', 'Content']} rows={columnRows(ANY_FILE_COLUMNS, false)} />
        <p>
          Add your own columns with names that start with <Code>x_</Code>. Like every column, they are flat: primitive
          types or lists of primitives, no structs or maps. Readers ignore columns they do not know. Any other column
          name makes the file non-conformant (rule F3), which keeps names free for later versions of the format.
        </p>
        <SubHeading>Recommended values</SubHeading>
        <ul className="list-disc pl-5 space-y-1 text-gray-600">
          <li>
            <Code>measurement_instrument_type</Code>: <Code>laboratory_spectrometer</Code>, <Code>field_spectrometer</Code>,{' '}
            <Code>ftir</Code>, <Code>imaging_spectrometer</Code>, <Code>goniometer</Code>.
          </li>
          <li>
            <Code>measurement_reference</Code> (what the values are relative to): <Code>spectralon</Code>,{' '}
            <Code>halon</Code>, <Code>gold</Code>, <Code>stainless_steel_mirror</Code>, <Code>blackbody</Code>, and{' '}
            <Code>absolute</Code> for values corrected to absolute reflectance.
          </li>
          <li>
            <Code>measurement_processing</Code> flags: <Code>averaged</Code>, <Code>interpolated</Code>,{' '}
            <Code>resampled</Code>, <Code>smoothed</Code>, <Code>continuum_removed</Code>, <Code>spliced</Code>,{' '}
            <Code>scaled_to_reference_level</Code>, <Code>simulated</Code> (synthetic or modelled, not measured; a
            resampled measurement is not simulated), <Code>absorbance_from_reflectance</Code>,{' '}
            <Code>absorbance_from_transmittance</Code>.
          </li>
        </ul>
        <p className="text-xs text-gray-500">
          These lists are from the specification text (section 5); the format does not enforce them.
        </p>
      </Section>

      <Section id="vocabularies" title="Vocabularies">
        <p>Vocabulary values are case-sensitive. Minor versions of the format may add values but never remove them.</p>
        {(
          [
            ['quantity', 'quantity: what values measure'],
            ['material_category', 'material_category: what was measured'],
            ['wavelength_unit', 'wavelength_unit: unit of the axis'],
            ['inclusion', 'inclusion (footer): complete or subset'],
          ] as const
        ).map(([key, title]) => (
          <div key={key} className="space-y-1.5">
            <SubHeading>{title}</SubHeading>
            <Table
              head={['Value', 'Meaning']}
              rows={Object.entries(VOCABULARIES[key]).map(([v, d]) => [
                <span className="font-mono font-medium text-gray-900 whitespace-nowrap">{v}</span>,
                <Prose text={d} />,
              ])}
            />
          </div>
        ))}
        <p>
          The <Code>geometry</Code> vocabulary has its {sectionLink('geometry', 'own section')}.
        </p>
      </Section>

      <Section id="geometry" title="Measurement geometry">
        <p>
          <Code>geometry</Code> names how the sample was lit and viewed, using the terms of Schaepman-Strub et al.
          (2006), <em>Reflectance quantities in optical remote sensing: definitions and case studies</em>, Remote Sensing
          of Environment 103, 27–42. Each side is <em>directional</em> (one direction), <em>conical</em> (a small cone of
          directions) or <em>hemispherical</em> (every direction above the surface). Together with{' '}
          <Code>quantity</Code>, it tells you whether two reflectance spectra can be compared directly.
        </p>
        <div className="flex gap-4 text-xs text-gray-500">
          <span className="flex items-center gap-1.5">
            <span className="w-3 h-0.5 bg-amber-600 inline-block" /> illumination
          </span>
          <span className="flex items-center gap-1.5">
            <span className="w-3 h-0.5 bg-indigo-600 inline-block" /> view (what the instrument collects)
          </span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {Object.entries(VOCABULARIES.geometry).map(([value, definition]) => (
            <div key={value} className="border border-gray-200 rounded-md p-3 flex gap-3">
              <div className="w-28 flex-shrink-0">
                <GeometryDiagram geometry={value} />
              </div>
              <div className="min-w-0 space-y-1">
                <div className="font-mono text-xs font-semibold text-gray-900">{value}</div>
                <p className="text-xs text-gray-600">
                  <Prose text={definition} />
                </p>
                {GEOMETRY_IN_PRACTICE[value] && <p className="text-xs text-gray-500">{GEOMETRY_IN_PRACTICE[value]}</p>}
              </div>
            </div>
          ))}
        </div>
        <SubHeading>Why integrating-sphere and contact-probe spectra differ</SubHeading>
        <p>
          An integrating sphere (<Code>directional_hemispherical</Code>) adds up the light reflected in every direction,
          including the mirror-like specular part. A contact probe, leaf clip or field fore-optic (<Code>biconical</Code>,{' '}
          <Code>hemispherical_conical</Code>) sees only a small cone of directions, and reports a reflectance factor: the
          ratio to a white reference panel seen the same way.
        </p>
        <p>
          For a perfectly diffuse surface the two numbers agree. Real surfaces are not perfectly diffuse. Rough soils and
          plant canopies scatter more light back towards the source; glossy leaves, wet or polished surfaces reflect more
          light forward. For such samples the two measurements can differ by several percent of reflectance, more near
          specular angles. The positions of absorption features usually agree better than overall brightness.
        </p>
        <p>
          So compare spectra with different geometries only when you know the sample is close to diffuse, and say that
          you did. When a source does not state its geometry, producers use <Code>unknown</Code> rather than guess.
        </p>
        <SubHeading>Recording the physical setup</SubHeading>
        <p>
          Geometry describes the physical setup, so equal setups get equal values across sources. Older libraries often
          call laboratory measurements with a lamp and fore-optic, contact probe or FTIR reflectance attachment
          "bidirectional". In Schaepman-Strub terms these are biconical reflectance factors (Kokaly et al. 2017, USGS
          Data Series 1035, p. 4), so producers record them as <Code>biconical</Code> and keep the source's own words in{' '}
          <Code>extra</Code>, e.g. <Code>{'"source_geometry_term": "Bidirectional reflectance"'}</Code>.{' '}
          <Code>bidirectional</Code> is for goniometric or near-collimated measurements with stated angles.
        </p>
      </Section>

      <Section id="footer" title="Footer metadata">
        <p>
          The Parquet footer's key-value metadata holds the key <Code>{FOOTER_KEY}</Code>. Its value is a UTF-8 JSON
          object that validates against <Code>#/$defs/collectionFooter</Code> in{' '}
          <ExtLink href={`${SCHEMAS_URL}/footer.schema.json`}>footer.schema.json</ExtLink>. Every Parquet library can
          write footer metadata (in pyarrow: <Code>table.replace_schema_metadata</Code>).
        </p>
        <CodeBlock label="Example footer" code={JSON.stringify(EXAMPLE_FOOTER, null, 2)} />
        <SubHeading>Top level</SubHeading>
        <Table head={['Field', 'Type', 'Required', 'Meaning']} rows={propertyRows(DEFS.collectionFooter, FOOTER_NOTES)} />
        <SubHeading>collection</SubHeading>
        <Table head={['Field', 'Type', 'Required', 'Meaning']} rows={propertyRows(DEFS.collection, COLLECTION_NOTES)} />
        <SubHeading>datasets.*</SubHeading>
        <p>
          Aggregated collections (for example EcoSIS) gather spectra from several datasets, each with its own licence and
          citation. Rows point at their dataset with <Code>dataset_id</Code>.
        </p>
        <Table head={['Field', 'Type', 'Required', 'Meaning']} rows={propertyRows(DEFS.dataset)} />
        <SubHeading>license_refs.*</SubHeading>
        <Table head={['Field', 'Type', 'Required', 'Meaning']} rows={propertyRows(DEFS.licenseRef)} />
        <p>
          Licences are SPDX expressions such as <Code>CC-BY-4.0</Code> or <Code>MIT OR Apache-2.0</Code>, a{' '}
          <Code>LicenseRef-</Code> identifier defined in <Code>license_refs</Code>, or <Code>NOASSERTION</Code> on its
          own. Operators are upper case; tokens are separated by single spaces or parentheses, with no tabs, no
          non-breaking spaces and nothing leading or trailing; <Code>DocumentRef-</Code> references are not allowed.
          When a source names a licence without a version, define a <Code>LicenseRef-</Code> with the source's wording
          rather than guess an SPDX version.
        </p>
        <p className="text-xs text-gray-600">
          Checker limit, not a format rule: this checker and the reference checker stop parsing parentheses nested more
          than {MAX_LICENCE_NESTING} deep, and report such an expression under F11.
        </p>
      </Section>

      <Section id="index" title="Index files">
        <p>
          An index file lists the spectra of several collection files without their arrays, so a reader can search them
          all by downloading one small file. It is optional and can always be rebuilt from the collection files. Its
          footer key is <Code>{INDEX_FOOTER_KEY}</Code>, holding the footer of every collection file it lists. The
          OpenSpecLib distribution names its index <Code>openspeclib-index.parquet</Code>.
        </p>
        <p>
          An index has every non-array column of the collection files it lists (everything except{' '}
          <Code>wavelengths</Code>, <Code>values</Code>, <Code>bandpass_fwhm</Code> and <Code>values_stddev</Code>), with
          the same values, plus these columns:
        </p>
        <Table head={['Column', 'Type', 'Content']} rows={columnRows(INDEX_ONLY_COLUMNS, false)} />
        <Table
          head={['Field', 'Type', 'Required', 'Meaning']}
          rows={propertyRows(DEFS.indexFooter, {
            collections:
              'One entry per collection file: `file`, `num_spectra`, optional `sha256`, and `metadata` (that file\'s complete footer).',
          })}
        />
        <p>
          Each <Code>file</Code> is a path relative to the index or an http(s) URL (no absolute paths or{' '}
          <Code>file:</Code> URLs), and each collection file is listed once, comparing resolved paths. Every index row
          matches exactly one row of its collection file, with equal values in the shared columns (<Code>x_</Code>{' '}
          columns, which must be flat as in collection files, are not compared). <Code>id</Code> is unique across the
          whole index, so two listed files cannot share a collection id.
        </p>
      </Section>

      <Section id="rules" title="Conformance rules">
        <p>
          Rule ids are stable: checkers report them, and the{' '}
          <button onClick={onOpenChecker} className="text-indigo-600 hover:text-indigo-800 underline">
            checker in this viewer
          </button>{' '}
          and the reference Python checker (<Code>openspeclib validate</Code>) implement the same rules from{' '}
          <ExtLink href={`${SCHEMAS_URL}/rules.json`}>rules.json</ExtLink>.
        </p>
        <p className="text-xs text-gray-600">
          Forward compatibility: minor versions (1.1, 1.2, …) only add things. When a file names a newer 1.x version,
          vocabulary values, columns, extensions and footer members a checker does not know are warnings, not failures.
          A file with a different major version is rejected. An index whose listed collection files cannot be fetched
          does not conform, because the cross-file checks cannot be completed.
        </p>
        {LEVELS.map(({ level, title, blurb }) => (
          <div key={level} className="space-y-1.5">
            <SubHeading>{title}</SubHeading>
            <p className="text-xs text-gray-500">{blurb}</p>
            <Table
              head={['Rule', 'Title', 'Requirement']}
              rows={RULES.filter((r) => r.level === level).map((r) => [
                <span className="font-mono font-semibold text-gray-900">{r.id}</span>,
                <span className="whitespace-nowrap">
                  {r.title}
                  {r.applies_to === 'index' && <span className="ml-1 text-gray-400">(index files)</span>}
                </span>,
                <Prose text={r.text} />,
              ])}
            />
          </div>
        ))}
        <SubHeading>Quality-profile bounds</SubHeading>
        <p className="text-xs text-gray-500">
          A valid value is finite and not a sentinel; NaN bands are ignored. Each spectrum is reported under at most one
          of Q1, Q2 and Q3. Q4 expects axis extents between {QA_WAVELENGTH_NM[0].toLocaleString('en-US')} nm and{' '}
          {QA_WAVELENGTH_NM[1].toLocaleString('en-US')} nm.
        </p>
        <Table
          head={['Quantity', 'Hard bounds (Q1, Q2)', 'Warning bounds (Q3)']}
          rows={Object.entries(QA_BOUNDS).map(([q, b]) => [
            <span className="font-mono">{q}</span>,
            `${b.hard[0]} to ${b.hard[1]}`,
            `${b.warn[0]} to ${b.warn[1]}`,
          ])}
        />
      </Section>

      <Section id="reading" title="Reading files">
        <p>
          A Parquet reader is all you need. Over HTTP, query one id per <Code>WHERE id = …</Code>: DuckDB then fetches
          only the row group that holds it. Replace the file name and id with your own.
        </p>
        <div className="space-y-3">
          {READING_SNIPPETS.map((s) => (
            <CodeBlock key={s.id} label={s.label} note={s.note} code={s.code(EXAMPLE_FILE, EXAMPLE_ID)} />
          ))}
        </div>
      </Section>

      <Section id="publishing" title="Publish your own library">
        <ol className="list-decimal pl-5 space-y-2">
          <li>
            <strong>Choose a collection id</strong> that matches <Code>{COLLECTION_ID_PATTERN}</Code> and is not in use.
            Known ids are listed in <ExtLink href={COLLECTIONS_URL}>docs/spec/collections.md</ExtLink>; open a pull
            request to add yours.
          </li>
          <li>
            <strong>Write one row per spectrum</strong> with the {sectionLink('columns', 'core columns')}. Keep
            wavelengths in your instrument's unit, store values as fractions, and put NaN in missing bands. Set{' '}
            <Code>quantity</Code> and {sectionLink('geometry', 'geometry')}; use <Code>unknown</Code> if you do not know
            the geometry.
          </li>
          <li>
            <strong>Add extension columns</strong> you can fill, and list their {sectionLink('extensions', 'extensions')}{' '}
            in the footer. Anything else goes in <Code>x_</Code> columns or the <Code>extra</Code> JSON column.
          </li>
          <li>
            <strong>Write the {sectionLink('footer', 'footer')}</strong>: title, licence, citation and whether the file
            is complete or a subset.
          </li>
          <li>
            <strong>Write the file</strong> sorted by id, with row groups of at most 250 spectra, and the arrays
            BYTE_STREAM_SPLIT-encoded and zstd-compressed (rules R1–R3). With pyarrow:
          </li>
        </ol>
        <CodeBlock label="Python (pyarrow): writing a collection file" code={WRITE_PYARROW} />
        <ol start={6} className="list-decimal pl-5 space-y-2">
          <li>
            <strong>Check it.</strong> Drop the file into the{' '}
            <button onClick={onOpenChecker} className="text-indigo-600 hover:text-indigo-800 underline">
              checker
            </button>
            , or run <Code>openspeclib validate your_file.parquet</Code>. A file with no failed rule conforms.
          </li>
        </ol>
        <p>
          More: the full specification (<ExtLink href={SPEC_URL}>format-v1.md</ExtLink>), the{' '}
          <ExtLink href={DOCS_URL}>project documentation</ExtLink> and the <ExtLink href={GITHUB}>GitHub repository</ExtLink>
          .
        </p>
      </Section>
    </div>
  );
}
