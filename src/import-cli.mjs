#!/usr/bin/env node
// 聚音 统一曲库导入 CLI（v1）
// 用途：扫描配置中的 QQ / 网易云下载目录 → 识别格式 → 本地解码（NCM=项目内置参考实现；QQ mflac/mgg=客户端辅助 qqd）
//       → 全长解码校验（ffmpeg）→ 原子发布到统一曲库目录（本地音乐播放器 读取）。
// 硬约束：原下载文件只读不动；失败文件不进正式曲库并记录原因；第二次运行只处理新增/变化文件；
//         不覆盖同名不同歌曲（外部同名有效文件→加后缀；外部同名无效文件→归档到 _replaced/ 后发布）。
// 用法：node import-cli.mjs --config <config.json> [--dry-run]
// 退出码：0=全部成功或无事可做；1=有失败项；2=配置/环境错误。

import fs from "node:fs";
import path from "node:path";
import crypto from "node:crypto";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";

// ---------- 小工具 ----------
const MAGIC = [
  { ext: "flac", test: (b) => b.subarray(0, 4).toString("latin1") === "fLaC" },
  { ext: "ogg", test: (b) => b.subarray(0, 4).toString("latin1") === "OggS" },
  { ext: "mp3", test: (b) => b.subarray(0, 3).toString("latin1") === "ID3" || (b[0] === 0xff && (b[1] & 0xe0) === 0xe0) },
  { ext: "m4a", test: (b) => b.length >= 8 && b.subarray(4, 8).toString("latin1") === "ftyp" },
  { ext: "wav", test: (b) => b.subarray(0, 4).toString("latin1") === "RIFF" },
];
const NCM_MAGIC = (b) => b.subarray(0, 8).toString("latin1") === "CTENFDAM";
const QMC_EXT = /\.(mflac\d*|mgg\d*|qmc[0-9a-z]*|mflach|mggl)$/i;
const PARTIAL_EXT = /\.(part|tmp|downloading|crdownload|!ut)$/i;

function sniffHead(file) {
  const fd = fs.openSync(file, "r");
  try {
    const b = Buffer.alloc(16);
    const n = fs.readSync(fd, b, 0, 16, 0);
    const head = b.subarray(0, n);
    if (NCM_MAGIC(head)) return "ncm";
    for (const m of MAGIC) if (m.test(head)) return m.ext;
    return "unknown";
  } finally {
    fs.closeSync(fd);
  }
}
function sha256File(file) {
  const h = crypto.createHash("sha256");
  h.update(fs.readFileSync(file));
  return h.digest("hex");
}
function walk(dir, out = []) {
  let entries;
  try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return out; }
  for (const e of entries) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (e.isFile()) out.push(p);
  }
  return out;
}
function run(cmd, args, { input, timeoutMs = 300000, cwd } = {}) {
  return new Promise((resolve) => {
    const p = spawn(cmd, args, { cwd, stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "", stderr = "";
    p.stdout.on("data", (d) => (stdout += d.toString("utf8")));
    p.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    const t = setTimeout(() => { try { p.kill(); } catch {} }, timeoutMs);
    p.on("close", (code) => { clearTimeout(t); resolve({ code, stdout, stderr }); });
    p.on("error", (e) => { clearTimeout(t); resolve({ code: -1, stdout, stderr: String(e) }); });
    if (input !== undefined) {
      const steps = Array.isArray(input) ? input : [{ data: input, delayMs: 0 }];
      (async () => {
        for (const s of steps) {
          await new Promise((r) => setTimeout(r, s.delayMs || 0));
          try { p.stdin.write(s.data); } catch {}
        }
        try { p.stdin.end(); } catch {}
      })();
    } else {
      try { p.stdin.end(); } catch {}
    }
  });
}
function fullDecode(ffmpegPath, file) {
  return new Promise((resolve) => {
    const p = spawn(ffmpegPath, ["-v", "error", "-i", file, "-map", "0:a:0", "-vn", "-sn", "-dn", "-f", "null", "-"], { stdio: ["ignore", "ignore", "pipe"] });
    let stderr = "";
    p.stderr.on("data", (d) => (stderr += d.toString("utf8")));
    const t = setTimeout(() => { try { p.kill(); } catch {} }, 600000);
    p.on("close", (code) => { clearTimeout(t); resolve({ ok: code === 0 && stderr.trim() === "", code, stderr: stderr.trim().slice(0, 300) }); });
    p.on("error", (e) => { clearTimeout(t); resolve({ ok: false, code: -1, stderr: String(e) }); });
  });
}

// ---------- 主流程 ----------
const argv = process.argv.slice(2);
const cfgPath = argv.includes("--config") ? argv[argv.indexOf("--config") + 1] : null;
const dryRun = argv.includes("--dry-run");
if (!cfgPath) { console.error("用法: node import-cli.mjs --config <config.json> [--dry-run]"); process.exit(2); }
let cfg;
try { cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8")); } catch (e) { console.error("配置读取失败: " + e.message); process.exit(2); }
for (const k of ["sourceDirs", "outputDir"]) if (!cfg[k]) { console.error("配置缺字段: " + k); process.exit(2); }

// Relative paths are anchored to the config file, never the shell's cwd.
const configDir = path.dirname(path.resolve(cfgPath));
const resolveSetting = (s) => path.resolve(configDir, s);
cfg.sourceDirs = cfg.sourceDirs.map(resolveSetting);
for (const k of ["outputDir", "workDir", "replacedDir", "runReportDir", "ncmDecoder"]) {
  if (cfg[k]) cfg[k] = resolveSetting(cfg[k]);
}
if (!cfg.ncmDecoder) cfg.ncmDecoder = fileURLToPath(new URL("./ncm-decrypt.mjs", import.meta.url));
if (cfg.ffmpegPath && /[\\/]/.test(cfg.ffmpegPath)) cfg.ffmpegPath = resolveSetting(cfg.ffmpegPath);
if (cfg.qqd) for (const k of ["exe", "workdir", "outputDir"]) {
  if (cfg.qqd[k]) cfg.qqd[k] = resolveSetting(cfg.qqd[k]);
}
const OUT = path.resolve(cfg.outputDir);
const WORK = path.resolve(cfg.workDir || path.join(OUT, "_juyin-work"));
const MANIFEST = path.join(OUT, ".juyin-import-manifest.json");
const STAGING = path.join(WORK, "staging");
const REPLACED = path.resolve(cfg.replacedDir || path.join(OUT, "_replaced"));
const RUNS = path.resolve(cfg.runReportDir || path.join(OUT, "_juyin-import-runs"));
// A preview must not execute decryptors, move existing output, or create folders.
if (dryRun) {
  const preview = cfg.sourceDirs.flatMap((d) => fs.existsSync(d) ? walk(path.resolve(d)) : [])
    .filter((f) => !f.startsWith(OUT + path.sep) && !f.startsWith(WORK + path.sep))
    .filter((f) => /\.(ncm|mflac\d*|mgg\d*|qmc\w*|flac|ogg|mp3|m4a|wav)$/i.test(f));
  console.log(JSON.stringify({ dryRun: true, candidates: preview.length, outputDir: OUT, noFilesChanged: true }));
  process.exit(0);
}
fs.mkdirSync(OUT, { recursive: true }); fs.mkdirSync(WORK, { recursive: true }); fs.mkdirSync(STAGING, { recursive: true }); fs.mkdirSync(RUNS, { recursive: true });

let manifest = { version: 1, entries: {} };
try { manifest = JSON.parse(fs.readFileSync(MANIFEST, "utf8")); } catch {}
manifest.entries ||= {};

const report = { startedAt: new Date().toISOString(), config: cfgPath, outputDir: OUT, discovered: 0, added: [], existed: [], waiting: [], failed: [], replaced: [], skippedSameSong: [], notes: [] };

// 1) 扫描
const files = [];
for (const d of cfg.sourceDirs) {
  const abs = path.resolve(d);
  if (!fs.existsSync(abs)) { report.notes.push("源目录不存在，跳过: " + abs); continue; }
  for (const f of walk(abs)) {
    if (f.startsWith(OUT + path.sep) || f.startsWith(WORK + path.sep)) continue; // 不扫自己的输出
    files.push(f);
  }
}
files.sort();
report.discovered = files.length;

// 2) 分类与增量过滤
const tasks = [];
for (const f of files) {
  const st = fs.statSync(f);
  const key = f;
  const prev = manifest.entries[key];
  if (prev && prev.status === "ok" && prev.size === st.size && prev.mtimeMs === st.mtimeMs && fs.existsSync(path.join(OUT, prev.output))) {
    report.existed.push({ source: f, output: prev.output });
    continue;
  }
  if (PARTIAL_EXT.test(f)) { report.waiting.push({ source: f, reason: "疑似未完成下载（扩展名）" }); continue; }
  if (/\.(lrc|jpg|jpeg|png|bmp|webp|txt)$/i.test(f)) continue;   // 歌词/封面等旁车文件不是曲库条目（随主文件复制）
  const head = sniffHead(f);
  let kind;
  if (head === "ncm") kind = "ncm";
  else if (head !== "unknown") kind = "plain";
  else if (QMC_EXT.test(f)) kind = "qmc";
  else if (/\.(flac|ogg|mp3|m4a|wav|ape|wv)$/i.test(f)) kind = "broken-audio";   // 音频扩展名但头字节非法 → 记失败
  else { report.skippedJunk = (report.skippedJunk || 0) + 1; continue; }        // 客户端目录里的 dll/exe/aep 等：静默跳过
  if (kind === "broken-audio") { report.failed.push({ source: f, reason: "音频扩展名但头字节非法（疑似损坏或未完成）" }); continue; }
  // A name alone is not an identity. This optional legacy shortcut is off by default.
  if (cfg.skipQmcTwins === true && kind === "qmc" && /\.mgg\d*$/i.test(f)) {
    const stemF = f.replace(/\.mgg\d*$/i, "");
    if (fs.existsSync(stemF + ".mflac") || fs.existsSync(stemF + ".mflac0")) {
      report.skippedSameSong.push({ source: f, reason: "同曲 .mflac 存在（保 FLAC）" });
      continue;
    }
  }
  tasks.push({ source: f, kind, size: st.size, mtimeMs: st.mtimeMs });
}
report.discovered = tasks.length + report.waiting.length + report.failed.length;

// 3) 解码到 staging
const results = []; // {task, file, ext, note}
async function decodeNcm(t) {
  const stage = fs.mkdtempSync(path.join(STAGING, "ncm-"));
  const localIn = path.join(stage, path.basename(t.source));
  fs.copyFileSync(t.source, localIn);
  const r = await run(process.execPath, [path.resolve(cfg.ncmDecoder), localIn], { timeoutMs: 300000 });
  if (r.code !== 0) return { error: "NCM 解码失败: " + (r.stderr || r.stdout).trim().slice(0, 200) };
  const m = /format=(\w+)/.exec(r.stdout);
  const ext = m ? m[1] : "bin";
  const produced = localIn.replace(/\.ncm$/i, "") + "." + ext;
  if (!fs.existsSync(produced)) return { error: "NCM 解码声称完成但产物缺失" };
  const cover = produced.replace(/\.\w+$/, "") + ".cover.jpg";
  return { file: produced, ext, cover: fs.existsSync(cover) ? cover : null, note: "ncm decoded" };
}
async function decodeQmcBatch(qmcTasks) {
  const qqd = cfg.qqd;
  if (!qqd || cfg.allowQqd !== true) {
    return qmcTasks.map((t) => ({task:t, waiting:"QQ 客户端辅助处理未启用（allowQqd=true 才运行）；现有普通音频可继续整理"}));
  }
  const qqdOut = path.resolve(qqd.outputDir);
  const exeOk = fs.existsSync(qqd.exe);
  const ps = await run("cmd", ["/c", "tasklist"]).catch(() => ({ stdout: "" }));
  const clientRunning = /QQMusic\.exe/i.test(ps.stdout);
  if (!exeOk || !clientRunning) {
    return qmcTasks.map((t) => ({ task: t, waiting: !clientRunning ? "QQ 客户端未运行（需登录并播放一首歌以加载取钥模块）" : "qqd 工具缺失" }));
  }
  // 归档 qqd 既有输出（只移动，不删除），保持其输出目录干净
  const archiveDir = path.join(qqdOut, "..", "output-archive", String(Date.now()));
  if (fs.existsSync(qqdOut)) {
    for (const f of fs.readdirSync(qqdOut)) {
      fs.mkdirSync(archiveDir, { recursive: true });
      try { fs.renameSync(path.join(qqdOut, f), path.join(archiveDir, f)); } catch {}
    }
  }
  // 暂存全部待处理 QMC 到临时输入目录（扁平）
  const inDir = fs.mkdtempSync(path.join(STAGING, "qmc-in-"));
  const staged = [];
  for (const [index, t] of qmcTasks.entries()) {
    const base = "juyin-" + index + "-" + crypto.createHash("sha256").update(t.source).digest("hex").slice(0,12) + path.extname(t.source);
    const dest = path.join(inDir, base);
    fs.copyFileSync(t.source, dest);
    staged.push({ t, dest, base });
  }
  const r = await run(qqd.exe, [], {
    cwd: qqd.workdir,
    input: [{ data: "n\n", delayMs: 800 }, { data: inDir + "\n", delayMs: 500 }],
    timeoutMs: 120000 + qmcTasks.length * 20000,
  });
  const outputs = fs.existsSync(qqdOut) ? fs.readdirSync(qqdOut) : [];
  return staged.map(({ t, base }) => {
    const stem = base.replace(/\.[^.]+$/, "");
    const hit = outputs.find((o) => o.replace(/\.[^.]+$/, "") === stem);
    if (!hit) return { task: t, error: "qqd 未产出（可能需在客户端播放一次加载模块；日志: " + ((r.stdout + r.stderr).replace(/\s+/g, " ").slice(-160)) + ")" };
    const src = path.join(qqdOut, hit);
    const stage = fs.mkdtempSync(path.join(STAGING, "qmc-"));
    const dst = path.join(stage, hit);
    fs.renameSync(src, dst);
    const ext = hit.split(".").pop().toLowerCase();
    return { task: t, file: dst, ext, note: "qqd decoded" };
  });
}

const qmcTasks = tasks.filter((t) => t.kind === "qmc");
for (const t of tasks) {
  if (t.kind === "plain") { results.push({ task: t, file: t.source, ext: path.extname(t.source).slice(1).toLowerCase(), note: "plain" }); }
  else if (t.kind === "ncm") { const r = await decodeNcm(t); results.push({ task: t, ...(r.error ? { error: r.error } : r) }); }
}
if (qmcTasks.length) {
  const rs = await decodeQmcBatch(qmcTasks);
  for (const r of rs) if (r.waiting) report.waiting.push({ source: r.task.source, reason: r.waiting });
  for (const r of rs) if (!r.waiting) results.push(r);
}

// 4) 校验 + 发布
function publishName(sourceFile, ext) {
  return path.basename(sourceFile).replace(/\.[^.]+$/, "") + "." + ext;
}
function archiveForeign(target) {
  fs.mkdirSync(REPLACED, { recursive: true });
  const dest = path.join(REPLACED, path.basename(target) + "." + Date.now());
  fs.renameSync(target, dest);
  report.replaced.push({ path: target, archivedTo: dest });
}
for (const r of results) {
  if (r.error) { report.failed.push({ source: r.task.source, reason: r.error }); continue; }
  const ext = r.ext;
  if (cfg.validateFullDecode && cfg.ffmpegPath) {
    const v = await fullDecode(cfg.ffmpegPath, r.file);
    if (!v.ok) { report.failed.push({ source: r.task.source, reason: "完整解码校验失败: " + v.stderr }); continue; }
  }
  let name = publishName(r.task.source, ext);
  let target = path.join(OUT, name);
  const owner = manifest.entries[r.task.source];
  const ownerOk = owner && owner.status === "ok";
  if (fs.existsSync(target)) {
    const sameAsOurs = ownerOk && path.resolve(path.join(OUT, owner.output)) === target;
    if (sameAsOurs) { /* 不应发生（增量已过滤） */ }
    else {
      const ourOwner = Object.entries(manifest.entries).find(([, e]) => e.output === name && e.status === "ok");
      const isOurs = !!ourOwner;
      // 自有冲突 + 已有无损件而新件为有损（.ogg）⇒ 保无损，把本源登记指向既有输出
      if (isOurs && path.extname(name).toLowerCase() === ".ogg" && path.extname(ourOwner[1].output).toLowerCase() === ".flac") {
        manifest.entries[r.task.source] = { size: r.task.size, mtimeMs: r.task.mtimeMs, srcSha256: sha256File(r.task.source), output: ourOwner[1].output, format: "flac", status: "ok", processedAt: new Date().toISOString() };
        report.existed.push({ source: r.task.source, output: ourOwner[1].output, reason: "同曲已有无损件（保 FLAC）" });
        continue;
      }
      const headOk = (() => { try { const b = fs.readFileSync(target).subarray(0, 16); return MAGIC.some((m) => m.test(b)) || NCM_MAGIC(b); } catch { return false; } })();
      if (!isOurs && !headOk) { archiveForeign(target); }
      else {
        const stem = name.replace(/\.[^.]+$/, "");
        let i = 2;
        while (fs.existsSync(path.join(OUT, stem + " (" + i + ")." + ext))) i++;
        name = stem + " (" + i + ")." + ext;
        target = path.join(OUT, name);
      }
    }
  }
  if (dryRun) { report.added.push({ source: r.task.source, output: name, dryRun: true }); continue; }
  const isCopy = r.note === "plain";                                   // 普通音频只复制，原文件保留不动
  const srcSha = sha256File(r.task.source);                            // 发布前取源哈希（plain 不移动源文件）
  if (isCopy) fs.copyFileSync(r.file, target);
  else {
    // Copy to the destination volume first, then atomically publish. Staging may be on another drive.
    const tmp = target + ".partial-" + process.pid;
    fs.copyFileSync(r.file, tmp);
    if (sha256File(tmp) !== sha256File(r.file)) throw new Error("发布副本哈希不符");
    fs.renameSync(tmp, target);
  }
  // 歌词旁车文件（源文件侧；解码件与普通件都适用）
  const lrcSrc = r.task.source.replace(/\.[^.]+$/, ".lrc");
  if (fs.existsSync(lrcSrc)) {
    try { fs.copyFileSync(lrcSrc, path.join(OUT, name.replace(/\.[^.]+$/, ".lrc"))); } catch {}
  }
  // 封面旁车（NCM 参考实现产物）
  if (r.cover) {
    try { fs.copyFileSync(r.cover, path.join(OUT, name.replace(/\.[^.]+$/, ".cover.jpg"))); } catch {}
  }
  manifest.entries[r.task.source] = {
    size: r.task.size, mtimeMs: r.task.mtimeMs, srcSha256: srcSha,
    output: name, format: ext, status: "ok", processedAt: new Date().toISOString(),
  };
  report.added.push({ source: r.task.source, output: name, format: ext });
}
// 失败项记入清单（未变且仍失败 → 下次重试）
for (const f of report.failed) {
  const st = fs.existsSync(f.source) ? fs.statSync(f.source) : null;
  if (st) manifest.entries[f.source] = { size: st.size, mtimeMs: st.mtimeMs, status: "failed", reason: f.reason, processedAt: new Date().toISOString() };
}

report.finishedAt = new Date().toISOString();
if (!dryRun) {
  const tmp = MANIFEST + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(manifest, null, 1));
  fs.renameSync(tmp, MANIFEST);
  const runFile = path.join(RUNS, report.startedAt.replace(/[:.]/g, "-") + ".json");
  fs.writeFileSync(runFile, JSON.stringify(report, null, 1));
  report.runFile = runFile;
}

// 5) 汇总输出
const fmt = (a) => a.length;
console.log("== 聚音导入 " + (dryRun ? "(dry-run) " : "") + "完成 ==");
console.log("发现 " + report.discovered + "，成功 " + fmt(report.added) + "，已存在 " + fmt(report.existed) + "，等待 " + fmt(report.waiting) + "，失败 " + fmt(report.failed) + "，归档替换 " + fmt(report.replaced) + "，同曲跳过 " + fmt(report.skippedSameSong));
for (const a of report.added) console.log("  + " + a.output + "  ← " + a.source);
for (const w of report.waiting) console.log("  … 等待: " + w.source + "（" + w.reason + "）");
for (const f of report.failed) console.log("  ! 失败: " + f.source + "（" + f.reason + "）");
for (const n of report.notes) console.log("  备注: " + n);
process.exit(report.failed.length ? 1 : 0);
