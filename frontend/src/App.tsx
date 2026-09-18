import { useEffect, useMemo, useState, type ChangeEvent } from 'react'
import { Bar, BarChart, Cell, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

type Marker = {
  amis_id: string
  site_name: string
  hazard_type: string
  hazard_class: string
  status: string
  score: number
  confidence: number
  x: number
  y: number
  z: number
}

type TerrainMeta = { marker_count: number; elevation_min_m: number; elevation_max_m: number; vertical_exaggeration: number; bbox_wgs84: number[] }
type Filters = { statuses: string[]; classes: string[]; types: string[]; scoreMin: number; scoreMax: number; confidenceMin: number; confidenceMax: number; query: string }

const emptyFilters: Filters = { statuses: [], classes: [], types: [], scoreMin: 0, scoreMax: 1, confidenceMin: 0, confidenceMax: 1, query: '' }
const renders = [
  { file: '01_full_scene.png', title: 'All features', description: 'Complete processed study window.' },
  { file: '02_high_severity.png', title: 'High severity', description: 'Features with severity greater than 0.7.' },
  { file: '03_high_severity_low_confidence.png', title: 'High severity / low confidence', description: 'Severity greater than 0.7 and confidence below 0.3.' },
  { file: '04_cross_section.png', title: 'Typical mine shaft section', description: 'Illustrative vertical section — not survey data.' },
]

const scoreLabel = (value: number) => value.toFixed(2)
const unique = (records: Marker[], field: keyof Pick<Marker, 'status' | 'hazard_class' | 'hazard_type'>) => [...new Set(records.map((record) => record[field]))].sort()
const selected = (event: ChangeEvent<HTMLSelectElement>) => Array.from(event.currentTarget.selectedOptions, (option) => option.value)

function distribution(records: Marker[], field: 'score' | 'confidence') {
  return Array.from({ length: 5 }, (_, index) => {
    const lower = index / 5
    const upper = (index + 1) / 5
    return { label: `${lower.toFixed(1)}–${upper.toFixed(1)}`, count: records.filter((item) => index === 4 ? item[field] >= lower && item[field] <= upper : item[field] >= lower && item[field] < upper).length }
  })
}

function MultiSelect({ label, options, value, onChange }: { label: string; options: string[]; value: string[]; onChange: (next: string[]) => void }) {
  return <label className="control multi-select"><span>{label}</span><select multiple value={value} onChange={(event) => onChange(selected(event))} aria-label={label}>{options.map((option) => <option key={option} value={option}>{option}</option>)}</select><small>{value.length ? `${value.length} selected` : 'All values'}</small></label>
}

function RangeControl({ label, lower, upper, onLower, onUpper }: { label: string; lower: number; upper: number; onLower: (value: number) => void; onUpper: (value: number) => void }) {
  return <div className="range-control"><span>{label}</span><div><label>Min<input type="number" min="0" max="1" step="0.05" value={lower} onChange={(event) => onLower(Math.min(Number(event.target.value), upper))} /></label><label>Max<input type="number" min="0" max="1" step="0.05" value={upper} onChange={(event) => onUpper(Math.max(Number(event.target.value), lower))} /></label></div></div>
}

function ChartCard({ title, subtitle, data, color }: { title: string; subtitle: string; data: { label: string; count: number }[]; color: string }) {
  return <section className="panel chart-card"><div className="panel-heading"><div><p className="eyebrow">Distribution</p><h2>{title}</h2></div><p>{subtitle}</p></div><div className="chart"><ResponsiveContainer width="100%" height="100%"><BarChart data={data} margin={{ top: 6, right: 6, left: -22, bottom: 0 }}><CartesianGrid vertical={false} stroke="#2d342f" /><XAxis dataKey="label" tick={{ fill: '#aeb7af', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis allowDecimals={false} tick={{ fill: '#aeb7af', fontSize: 11 }} axisLine={false} tickLine={false} /><Tooltip cursor={{ fill: '#ffffff0d' }} contentStyle={{ background: '#171c19', border: '1px solid #3c4740', borderRadius: 8 }} /><Bar dataKey="count" fill={color} radius={[3, 3, 0, 0]} /></BarChart></ResponsiveContainer></div></section>
}

function SpatialPlot({ records, allRecords }: { records: Marker[]; allRecords: Marker[] }) {
  const extent = useMemo(() => ({ minX: Math.min(...allRecords.map((item) => item.x)), maxX: Math.max(...allRecords.map((item) => item.x)), minY: Math.min(...allRecords.map((item) => item.y)), maxY: Math.max(...allRecords.map((item) => item.y)) }), [allRecords])
  const position = (record: Marker) => ({ x: 4 + 92 * (record.x - extent.minX) / (extent.maxX - extent.minX), y: 96 - 92 * (record.y - extent.minY) / (extent.maxY - extent.minY) })
  return <section className="panel spatial"><div className="panel-heading"><div><p className="eyebrow">Spatial distribution</p><h2>Relative district position</h2></div><p>Scene coordinates, north at top. This is not a geographic basemap.</p></div><div className="spatial-key"><span><i className="confidence-low" />Low confidence</span><span><i className="confidence-high" />High confidence</span><span>Point size = severity</span></div><svg viewBox="0 0 100 100" role="img" aria-label={`${records.length} selected hazards plotted by relative position`}><rect x="3" y="3" width="94" height="94" rx="1" /><path d="M50 3v94M3 50h94" /><text x="5" y="9">N</text>{records.map((record) => { const point = position(record); const fill = `hsl(${42 - record.confidence * 36} 73% ${55 - record.confidence * 8}%)`; return <circle key={`${record.amis_id}-${record.hazard_type}-${record.x}`} cx={point.x} cy={point.y} r={0.45 + record.score * 1.35} fill={fill} fillOpacity="0.82"><title>{`${record.site_name}: ${record.hazard_type}; severity ${scoreLabel(record.score)}, confidence ${scoreLabel(record.confidence)}`}</title></circle> })}</svg></section>
}

function App() {
  const [records, setRecords] = useState<Marker[]>([])
  const [meta, setMeta] = useState<TerrainMeta | null>(null)
  const [filters, setFilters] = useState<Filters>(emptyFilters)
  const [page, setPage] = useState(0)
  const [activeRender, setActiveRender] = useState(0)
  const [lightbox, setLightbox] = useState(false)

  useEffect(() => { Promise.all([fetch('/data/markers.json').then((response) => response.json()), fetch('/data/terrain_meta.json').then((response) => response.json())]).then(([markerData, terrainData]) => { setRecords(markerData); setMeta(terrainData) }).catch(console.error) }, [])
  useEffect(() => setPage(0), [filters])
  useEffect(() => { const close = (event: KeyboardEvent) => event.key === 'Escape' && setLightbox(false); window.addEventListener('keydown', close); return () => window.removeEventListener('keydown', close) }, [])

  const options = useMemo(() => ({ statuses: unique(records, 'status'), classes: unique(records, 'hazard_class'), types: unique(records, 'hazard_type') }), [records])
  const filtered = useMemo(() => records.filter((record) => {
    const query = filters.query.trim().toLowerCase()
    return (!filters.statuses.length || filters.statuses.includes(record.status)) && (!filters.classes.length || filters.classes.includes(record.hazard_class)) && (!filters.types.length || filters.types.includes(record.hazard_type)) && record.score >= filters.scoreMin && record.score <= filters.scoreMax && record.confidence >= filters.confidenceMin && record.confidence <= filters.confidenceMax && (!query || record.site_name.toLowerCase().includes(query) || record.amis_id.toLowerCase().includes(query))
  }), [records, filters])
  const stats = useMemo(() => ({ severity: filtered.length ? filtered.reduce((sum, record) => sum + record.score, 0) / filtered.length : 0, confidence: filtered.length ? filtered.reduce((sum, record) => sum + record.confidence, 0) / filtered.length : 0, sites: new Set(filtered.map((record) => record.site_name)).size }), [filtered])
  const types = useMemo(() => Object.entries(filtered.reduce<Record<string, number>>((result, record) => ({ ...result, [record.hazard_type]: (result[record.hazard_type] ?? 0) + 1 }), {})).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([label, count]) => ({ label: label.length > 22 ? `${label.slice(0, 21)}…` : label, count })), [filtered])
  const ordered = useMemo(() => [...filtered].sort((a, b) => b.score - a.score || b.confidence - a.confidence), [filtered])
  const pageSize = 20
  const pages = Math.max(1, Math.ceil(ordered.length / pageSize))
  const pageRows = ordered.slice(page * pageSize, (page + 1) * pageSize)
  const update = <K extends keyof Filters>(key: K, value: Filters[K]) => setFilters((current) => ({ ...current, [key]: value }))
  const render = renders[activeRender]

  if (!records.length) return <main className="loading"><p className="eyebrow">AMIS / Cobalt</p><h1>Loading field report…</h1></main>
  return <main>
    <header className="hero"><div><p className="eyebrow">Ontario abandoned mine information system</p><h1>Cobalt hazard<br /><em>field report.</em></h1><p className="lede">An interactive reading of abandoned-mine hazard features across the processed Cobalt study window.</p></div><dl><div><dt>Study window</dt><dd>{meta?.bbox_wgs84 ? 'Cobalt, Ontario' : 'Processed district'}</dd></div><div><dt>Terrain range</dt><dd>{meta ? `${Math.round(meta.elevation_min_m)}–${Math.round(meta.elevation_max_m)} m` : '—'}</dd></div><div><dt>Vertical exaggeration</dt><dd>{meta ? `${meta.vertical_exaggeration}×` : '—'}</dd></div></dl></header>

    <section className="filters panel" aria-label="Hazard filters"><div className="filter-title"><p className="eyebrow">Explore the data</p><h2>Filter every view</h2><button className="text-button" onClick={() => setFilters(emptyFilters)}>Reset filters</button></div><label className="control search"><span>Search site or AMIS ID</span><input value={filters.query} onChange={(event) => update('query', event.target.value)} placeholder="e.g. Nipissing or AMIS ID" /></label><MultiSelect label="Hazard status" options={options.statuses} value={filters.statuses} onChange={(value) => update('statuses', value)} /><MultiSelect label="Hazard class" options={options.classes} value={filters.classes} onChange={(value) => update('classes', value)} /><MultiSelect label="Hazard type" options={options.types} value={filters.types} onChange={(value) => update('types', value)} /><RangeControl label="Severity" lower={filters.scoreMin} upper={filters.scoreMax} onLower={(value) => update('scoreMin', value)} onUpper={(value) => update('scoreMax', value)} /><RangeControl label="Confidence" lower={filters.confidenceMin} upper={filters.confidenceMax} onLower={(value) => update('confidenceMin', value)} onUpper={(value) => update('confidenceMax', value)} /></section>

    <section className="kpis" aria-label="Filtered summary"><article><span>Matching hazards</span><strong>{filtered.length.toLocaleString()}</strong><small>of {records.length.toLocaleString()} processed records</small></article><article><span>Average severity</span><strong>{scoreLabel(stats.severity)}</strong><small>marker height in the renders</small></article><article><span>Average confidence</span><strong>{scoreLabel(stats.confidence)}</strong><small>marker colour in the renders</small></article><article><span>Distinct sites</span><strong>{stats.sites.toLocaleString()}</strong><small>with matching hazard features</small></article></section>

    <section className="analytics-grid"><ChartCard title="Severity" subtitle="0.0 low → 1.0 high" data={distribution(filtered, 'score')} color="#d66349" /><ChartCard title="Confidence" subtitle="0.0 low → 1.0 high" data={distribution(filtered, 'confidence')} color="#e8b33a" /><section className="panel chart-card types"><div className="panel-heading"><div><p className="eyebrow">Composition</p><h2>Most common hazard types</h2></div><p>Top eight in selection</p></div><div className="chart"><ResponsiveContainer width="100%" height="100%"><BarChart data={types} layout="vertical" margin={{ top: 6, right: 12, left: 20, bottom: 0 }}><CartesianGrid horizontal={false} stroke="#2d342f" /><XAxis type="number" allowDecimals={false} tick={{ fill: '#aeb7af', fontSize: 11 }} axisLine={false} tickLine={false} /><YAxis type="category" dataKey="label" width={145} tick={{ fill: '#aeb7af', fontSize: 10 }} axisLine={false} tickLine={false} /><Tooltip cursor={{ fill: '#ffffff0d' }} contentStyle={{ background: '#171c19', border: '1px solid #3c4740', borderRadius: 8 }} /><Bar dataKey="count" radius={[0, 3, 3, 0]}>{types.map((item, index) => <Cell key={item.label} fill={index < 3 ? '#b94537' : '#8f4c3a'} />)}</Bar></BarChart></ResponsiveContainer></div></section></section>

    <section className="detail-grid"><SpatialPlot records={filtered} allRecords={records} /><section className="panel table-panel"><div className="panel-heading"><div><p className="eyebrow">Feature records</p><h2>Highest severity first</h2></div><p>{ordered.length.toLocaleString()} matching records</p></div><div className="table-wrap"><table><thead><tr><th>Site / ID</th><th>Hazard type</th><th>Status</th><th>Severity</th><th>Confidence</th></tr></thead><tbody>{pageRows.map((record) => <tr key={`${record.amis_id}-${record.hazard_type}-${record.x}`}><td><strong>{record.site_name}</strong><small>{record.amis_id}</small></td><td>{record.hazard_type}</td><td><span className="status">{record.status}</span></td><td>{scoreLabel(record.score)}</td><td>{scoreLabel(record.confidence)}</td></tr>)}</tbody></table></div><nav className="pagination" aria-label="Hazard record pages"><button disabled={page === 0} onClick={() => setPage((current) => current - 1)}>Previous</button><span>Page {page + 1} of {pages}</span><button disabled={page >= pages - 1} onClick={() => setPage((current) => current + 1)}>Next</button></nav></section></section>

    <section className="gallery"><div className="gallery-copy"><p className="eyebrow">Generated renders</p><h2>Terrain as evidence.</h2><p>The terrain render encodes severity in marker height and confidence from amber to red. The cross-section is schematic and is not survey data.</p></div><figure><button className="featured-render" onClick={() => setLightbox(true)} aria-label={`Open ${render.title} in a larger view`}><img src={`/renders/${render.file}`} alt={render.description} /></button><figcaption><div><strong>{render.title}</strong><span>{render.description}</span></div><button className="text-button" onClick={() => setLightbox(true)}>Enlarge image</button></figcaption><div className="thumbnails">{renders.map((item, index) => <button key={item.file} className={index === activeRender ? 'active' : ''} onClick={() => setActiveRender(index)} aria-label={`Show ${item.title}`}><img src={`/renders/${item.file}`} alt="" /></button>)}</div></figure></section>
    <footer>Source: Ontario AMIS hazard features and PDEM terrain, processed for the configured Cobalt study window. Data shown here is a static export, not a live hazard service.</footer>
    {lightbox && <div className="lightbox" role="dialog" aria-modal="true" aria-label={render.title} onClick={() => setLightbox(false)}><button className="close" onClick={() => setLightbox(false)} aria-label="Close image">×</button><img src={`/renders/${render.file}`} alt={render.description} onClick={(event) => event.stopPropagation()} /></div>}
  </main>
}

export default App
