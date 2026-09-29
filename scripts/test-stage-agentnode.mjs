import { test } from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';

test('reviewed runtime contains every local asset needed to render Conductor', async () => {
  const source = new URL('../vendor/agentnode-runtime/', import.meta.url);
  const reviewed = JSON.parse(await fs.readFile(new URL('./agentnode-runtime-files.json', import.meta.url), 'utf8'));
  const html = await fs.readFile(new URL('static/index.html', source), 'utf8');
  const assets = [...html.matchAll(/(?:src|href)="\/static\/([^"\n]+)"/g)].map(match => `static/${match[1]}`);
  assert.ok(assets.length > 0, 'Conductor asset references must be checked');
  for (const name of assets) {
    assert.ok(Object.hasOwn(reviewed.files, name), `${name} is missing from the reviewed runtime manifest`);
    const bytes = await fs.readFile(new URL(name, source));
    assert.equal(crypto.createHash('sha256').update(bytes).digest('hex'), reviewed.files[name], name);
  }
});

test('runtime staging is reproducible, excludes extra files, and rejects tampering and symlinks', async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'lodge-stage-test-'));
  try {
    const source = path.join(root, 'vendor/agentnode-runtime');
    await fs.mkdir(path.join(source, 'docker'), { recursive: true });
    await fs.mkdir(path.join(root, 'scripts'));
    await fs.copyFile(new URL('./stage-agentnode.mjs', import.meta.url), path.join(root, 'scripts/stage-agentnode.mjs'));
    const bytes = 'FROM scratch\n';
    await fs.writeFile(path.join(source, 'docker/Dockerfile'), bytes);
    await fs.writeFile(path.join(source, 'credentials.json'), 'fixture that must never be copied');
    await fs.writeFile(path.join(root, 'scripts/agentnode-runtime-files.json'), JSON.stringify({
      version: 1, files: { 'docker/Dockerfile': crypto.createHash('sha256').update(bytes).digest('hex') },
    }));
    const env = { ...process.env };
    delete env.PRIVACY_LODGE_AGENTNODE_SOURCE;
    const run = () => spawnSync(process.execPath, ['scripts/stage-agentnode.mjs'], { cwd: root, env, encoding: 'utf8' });
    assert.equal(run().status, 0);
    const output = path.join(root, 'src-tauri/agentnode-runtime');
    const first = await fs.readFile(path.join(output, 'manifest.json'), 'utf8');
    assert.deepEqual(await fs.readdir(output), ['docker', 'manifest.json']);
    assert.equal(run().status, 0);
    assert.equal(await fs.readFile(path.join(output, 'manifest.json'), 'utf8'), first);
    await fs.writeFile(path.join(source, 'docker/Dockerfile'), 'tampered');
    assert.notEqual(run().status, 0);
    assert.equal(await fs.readFile(path.join(output, 'docker/Dockerfile'), 'utf8'), bytes);
    await fs.unlink(path.join(source, 'docker/Dockerfile'));
    await fs.symlink('../credentials.json', path.join(source, 'docker/Dockerfile'));
    assert.notEqual(run().status, 0);
    await fs.unlink(path.join(source, 'docker/Dockerfile'));
    await fs.rmdir(path.join(source, 'docker'));
    await fs.mkdir(path.join(root, 'outside'));
    await fs.writeFile(path.join(root, 'outside/Dockerfile'), bytes);
    await fs.symlink(path.join(root, 'outside'), path.join(source, 'docker'));
    assert.notEqual(run().status, 0);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
