import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const project = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const ffmpeg = process.env.FFMPEG_PATH || 'ffmpeg';
const hash = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');

test('relative configuration, preview, incremental import and source preservation', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'music-toolkit-test-'));
  try {
    const downloads = path.join(root, 'downloads');fs.mkdirSync(downloads);
    const input = path.join(downloads, 'Singer - Generated.wav');
    const generated = spawnSync(ffmpeg, ['-v','error','-f','lavfi','-i',
      'sine=frequency=440:duration=1','-c:a','pcm_s16le',input]);
    assert.equal(generated.status,0,generated.stderr.toString());
    const before = hash(input);
    const config = path.join(root, 'config.json');
    fs.writeFileSync(config,JSON.stringify({sourceDirs:['./downloads'],outputDir:'./library',
      workDir:'./work',ffmpegPath:ffmpeg,validateFullDecode:true,
      runReportDir:'./reports',replacedDir:'./backups'}));
    const run = extra => spawnSync(process.execPath,[path.join(project,'src/import-cli.mjs'),
      '--config',config,...extra],{cwd:project,encoding:'utf8'});
    assert.equal(run(['--dry-run']).status,0);
    assert.equal(fs.existsSync(path.join(root,'library')),false);
    let result = run([]);assert.equal(result.status,0,result.stderr);
    const final = path.join(root,'library','Singer - Generated.wav');
    assert.equal(hash(final),before);
    const first = fs.readdirSync(path.join(root,'library')).filter(n=>n.endsWith('.wav'));
    result = run([]);assert.equal(result.status,0,result.stderr);
    assert.deepEqual(fs.readdirSync(path.join(root,'library')).filter(n=>n.endsWith('.wav')),first);
    assert.equal(hash(input),before);
    assert.equal(fs.existsSync(path.join(root,'library','_juyin-import-runs')),false);
  } finally { fs.rmSync(root,{recursive:true,force:true}); }
});
