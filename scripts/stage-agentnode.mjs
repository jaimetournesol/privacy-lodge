// Bundle only runtime source, never a developer's credentials, caches or node_modules.
import { promises as fs } from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
const lodge = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
// Public builds use only the reviewed snapshot. A private checkout is an explicit
// development override, still restricted to the reviewed list of file names.
const override = process.env.PRIVACY_LODGE_AGENTNODE_SOURCE;
const source = path.resolve(override || path.join(lodge, 'vendor/agentnode-runtime'));
const destination = path.join(lodge, 'src-tauri/agentnode-runtime');
const reviewed = JSON.parse(await fs.readFile(path.join(lodge, 'scripts/agentnode-runtime-files.json'), 'utf8'));
if (reviewed.version !== 1) throw new Error('Unsupported runtime file list');
const files = Object.keys(reviewed.files).sort();
const temporary = `${destination}.staging`;
await fs.rm(temporary, { recursive: true, force: true });
await fs.mkdir(temporary, { recursive: true });
const manifest = { version: 1, digest: '', files: {} };
const digest = crypto.createHash('sha256');
for (const name of files) {
  const parts = name.split('/');
  if (parts.some(p => !p || p.startsWith('.') || p.includes('\\') || p.includes(':'))) {
    throw new Error(`Invalid runtime source path: ${name}`);
  }
  for (let i = 1; i <= parts.length; i++) {
    const entry = await fs.lstat(path.join(source, ...parts.slice(0, i)));
    if (entry.isSymbolicLink() || (i === parts.length && !entry.isFile())) {
      throw new Error(`Runtime source must be a regular file with no symlink ancestors: ${name}`);
    }
  }
  const bytes = await fs.readFile(path.join(source, name));
  manifest.files[name] = crypto.createHash('sha256').update(bytes).digest('hex');
  if (!override && manifest.files[name] !== reviewed.files[name]) {
    throw new Error(`Reviewed runtime source failed its checksum: ${name}`);
  }
  digest.update(name).update('\0').update(manifest.files[name]).update('\n');
  await fs.mkdir(path.dirname(path.join(temporary, name)), { recursive: true });
  await fs.writeFile(path.join(temporary, name), bytes);
}
manifest.digest = digest.digest('hex');
await fs.writeFile(path.join(temporary, 'manifest.json'), JSON.stringify(manifest, null, 2));
await fs.rm(destination, { recursive: true, force: true });
await fs.rename(temporary, destination);
console.log(`Staged ${files.length} Agentnode runtime files (${manifest.digest.slice(0, 16)}).`);
