// NCM 解密参考实现（Node，仅用于 P0 算法验证与黄金测试向量生成）。
// 授权边界：只处理用户自己已合法下载到本机的文件。
// 用法: node ncm-decrypt.mjs <input.ncm> [output.mp3|output.flac]
//       node ncm-decrypt.mjs --golden <dir> <out.json>   生成黄金向量清单
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const CORE_KEY = Buffer.from('hzHRAmso5kInbaxW', 'latin1');
const META_KEY = Buffer.from('#14ljk_!\\]&0U<\'(', 'latin1');

function aesEcbDecrypt(key, data) {
  const d = crypto.createDecipheriv('aes-128-ecb', key, null);
  d.setAutoPadding(true);
  return Buffer.concat([d.update(data), d.final()]);
}

function buildKeyBox(key) {
  const box = Array.from({ length: 256 }, (_, i) => i);
  let c = 0, last = 0;
  for (let i = 0; i < 256; i++) {
    const swap = key[i % key.length];
    c = (c + box[i] + swap) & 0xff;
    [box[i], box[c]] = [box[c], box[i]];
  }
  const stream = Buffer.alloc(256);
  for (let i = 0; i < 256; i++) {
    last = (last + 1) & 0xff;
    const j = (box[last] + box[(box[last] + last) & 0xff]) & 0xff;
    stream[i] = box[j];
  }
  return stream;
}

export function decryptNcm(buf) {
  if (buf.toString('latin1', 0, 8) !== 'CTENFDAM') throw new Error('not an NCM container');
  let p = 8;
  p += 2; // gap
  const keyLen = buf.readUInt32LE(p); p += 4;
  const keyData = Buffer.from(buf.subarray(p, p + keyLen)); p += keyLen;
  for (let i = 0; i < keyData.length; i++) keyData[i] ^= 0x64;
  const deKey = aesEcbDecrypt(CORE_KEY, keyData);
  const rc4Key = deKey.subarray(17); // 去掉 "neteasecloudmusic" 前缀
  const stream = buildKeyBox(rc4Key);

  // keyData 之后无间隙，直接是 metaLen（实测本机样本确认）
  const metaLen = buf.readUInt32LE(p); p += 4;
  let meta = null;
  if (metaLen > 0) {
    const mData = Buffer.from(buf.subarray(p, p + metaLen)); p += metaLen;
    for (let i = 0; i < mData.length; i++) mData[i] ^= 0x63;
    const prefix = mData.toString('latin1', 0, 22);
    if (prefix !== "163 key(Don't modify):") throw new Error(`unexpected meta prefix: ${prefix}`);
    const b64 = mData.subarray(22).toString('latin1');
    const json = Buffer.from(b64, 'base64'); // 注意：此处无 0x64 异或，0x64 仅用于密钥数据
    try {
      const plain = aesEcbDecrypt(META_KEY, json).toString('utf8');
      const jsonText = plain.startsWith('music:') ? plain.slice(6) : plain;
      meta = JSON.parse(jsonText);
    } catch (e) { throw new Error(`meta decrypt failed: ${e.message}`); }
  }

  p += 4; // CRC
  p += 5; // gap
  const imgLen = buf.readUInt32LE(p); p += 4;
  const image = buf.subarray(p, p + imgLen); p += imgLen;

  const audio = Buffer.alloc(buf.length - p);
  for (let i = 0; i < audio.length; i++) audio[i] = buf[p + i] ^ stream[i & 0xff];
  return { audio, image, meta };
}

function sniff(buf) {
  if (buf.toString('latin1', 0, 3) === 'ID3' || (buf[0] === 0xff && (buf[1] & 0xe0) === 0xe0)) return 'mp3';
  if (buf.toString('latin1', 0, 4) === 'fLaC') return 'flac';
  if (buf.toString('latin1', 0, 4) === 'OggS') return 'ogg';
  if (buf.toString('latin1', 0, 4) === 'RIFF') return 'wav';
  return 'bin';
}

function sha256(b) { return crypto.createHash('sha256').update(b).digest('hex'); }

const argv = process.argv.slice(2);
const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (!isMain) {
  // 作为模块导入时不执行 CLI 逻辑
} else if (argv[0] === '--golden') {
  const dir = argv[1], out = argv[2];
  const entries = [];
  for (const f of fs.readdirSync(dir).sort()) {
    if (!f.toLowerCase().endsWith('.ncm')) continue;
    const { audio, image, meta } = decryptNcm(fs.readFileSync(path.join(dir, f)));
    const ext = sniff(audio);
    entries.push({
      input: f, inputSha256: sha256(fs.readFileSync(path.join(dir, f))),
      format: ext, audioSha256: sha256(audio), audioBytes: audio.length,
      hasCover: image.length > 0, coverBytes: image.length,
      title: meta?.musicName ?? null, artist: meta?.artist?.map((a) => a[0]).join('/') ?? null,
      album: meta?.album ?? null, durationMs: meta?.duration ?? null,
    });
  }
  fs.writeFileSync(out, JSON.stringify({ generatedAt: new Date().toISOString(), vectors: entries }, null, 2));
  console.log(`wrote ${entries.length} vectors to ${out}`);
} else {
  const [, outPath] = argv;
  const { audio, image, meta } = decryptNcm(fs.readFileSync(argv[0]));
  const ext = sniff(audio);
  const o = outPath || argv[0].replace(/\.ncm$/i, '') + '.' + ext;
  fs.writeFileSync(o, audio);
  if (image.length) fs.writeFileSync(o.replace(/\.\w+$/, '') + '.cover.jpg', image);
  console.log(`${o} format=${ext} audio=${audio.length}B cover=${image.length}B title=${meta?.musicName ?? '?'}`);
}
