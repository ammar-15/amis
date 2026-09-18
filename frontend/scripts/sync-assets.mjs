import { cp, mkdir, rm } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const publicDir = resolve(root, 'frontend/public')
const sources = [
  ['data/processed/markers.json', 'data/markers.json'],
  ['data/processed/terrain_meta.json', 'data/terrain_meta.json'],
  ['renders/01_full_scene.png', 'renders/01_full_scene.png'],
  ['renders/02_high_severity.png', 'renders/02_high_severity.png'],
  ['renders/03_high_severity_low_confidence.png', 'renders/03_high_severity_low_confidence.png'],
  ['renders/04_cross_section.png', 'renders/04_cross_section.png'],
]

await rm(resolve(publicDir, 'data'), { recursive: true, force: true })
await rm(resolve(publicDir, 'renders'), { recursive: true, force: true })
for (const [from, to] of sources) {
  const destination = resolve(publicDir, to)
  await mkdir(dirname(destination), { recursive: true })
  await cp(resolve(root, from), destination)
}
console.log('Synchronized processed dashboard data and rendered images.')
