// scripts/generate-manifest.js
// Dryrun: node scripts/generate-manifest.js --validate-only
// Run:    node scripts/generate-manifest.js
//
// New model (no single media_type):
// - Galleries only via subdirectories: Images/<id>/NN.ext, Gifs/<id>/NN.gif, Videos/<id>/NN.mp4, RedGiphys/<id>/NN.mp4
// - Singles via flat files: Images/<id>.<ext>, Gifs/<id>.gif, Videos/<id>.mp4, RedGiphys/<id>.mp4
// - Mixed-media galleries supported (NN indices are 2-digit and globally unique per <id> across folders)
//
// Manifest outputs (per post):
//   media_items   : [{ index, kind: "image"|"gif"|"video"|"redgiphy", url, poster? }]
//   media_types   : string[] (unique kinds present)
//   gallery_count : number | null
//   media_preview : image URL used for the card preview (always an image)
//   preview       : { src, srcset?, sizes, w?, h?, source }  // responsive card preview block
//
// Posters / previews:
// - Prefer earliest 'image' item for media_preview.
// - Else use sidecar poster Posters/<id>/<NN>.(avif|webp|jpg|jpeg) for the first gallery item.
// - Else optional generated poster ladder (single format, widths = POSTER_WIDTHS) from first playable (video/gif).
// - Else Reddit preview ladder; else selftext first image.
//
// Facets:
// - mediaKinds facet counts each kind found in media_types (union), replacing old single-type facet.
//
// Env (required):
//   R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
//   PUBLIC_MEDIA_BASE   = "https://<your-worker>/"   // MUST end with '/'
// Optional:
//   R2_OBJECTS_CACHE    = "data/r2_objects.json"
//   R2_SKIP_LIST        = "1"                        // read cache instead of R2
//   ENABLE_LOCAL_POSTERS= "1"                        // ffmpeg+sharp poster generation for motion-only posts
//   POSTER_WIDTHS       = "240,360,480,720"
//   POSTER_FORMATS      = "avif,webp,jpeg"           // priority order; writes only the first that works
//   POSTER_QUALITY_AVIF = "50"
//   POSTER_QUALITY_WEBP = "74"
//   POSTER_QUALITY_JPEG = "78"

console.log("ENV present:",
    !!process.env.R2_ENDPOINT,
    !!process.env.R2_ACCESS_KEY_ID,
    !!process.env.R2_SECRET_ACCESS_KEY,
    !!process.env.R2_BUCKET,
    !!process.env.PUBLIC_MEDIA_BASE
);

import { promises as fs } from "node:fs";
import path from "node:path";
import { S3Client, ListObjectsV2Command } from "@aws-sdk/client-s3";
try { await import("dotenv/config"); } catch { }

const ROOT = process.cwd();
const INPUT_DIR = path.resolve(ROOT, "data/posts");
const INDEX_OUT_DIR = path.resolve(ROOT, "public/data/indexes");
const POSTS_PUBLIC_DIR = path.resolve(ROOT, "public/data/posts");
const ORDERED_CSV = path.resolve(ROOT, "data/ordered_posts.csv");
await fs.mkdir(INDEX_OUT_DIR, { recursive: true });
await fs.mkdir(POSTS_PUBLIC_DIR, { recursive: true });

/** ---------- ENV ---------- */
const PUBLIC_MEDIA_BASE_RAW = process.env.PUBLIC_MEDIA_BASE || "";
const PUBLIC_MEDIA_BASE = PUBLIC_MEDIA_BASE_RAW ? PUBLIC_MEDIA_BASE_RAW.replace(/\/?$/, "/") : "";
const MEDIA_BASE_OK = !!PUBLIC_MEDIA_BASE;

const R2_ENDPOINT = process.env.R2_ENDPOINT || "";
const R2_ACCESS_KEY_ID = process.env.R2_ACCESS_KEY_ID || "";
const R2_SECRET_ACCESS_KEY = process.env.R2_SECRET_ACCESS_KEY || "";
const R2_BUCKET = process.env.R2_BUCKET || "";
const R2_OBJECTS_CACHE = process.env.R2_OBJECTS_CACHE || path.resolve(ROOT, "data/r2_objects.json");
const R2_SKIP_LIST = process.env.R2_SKIP_LIST === "1";

/** ---------- POSTER CONFIG (single-format ladder) ---------- */
const ENABLE_LOCAL_POSTERS = process.env.ENABLE_LOCAL_POSTERS === "1";
const PREVIEWS_DIR = path.resolve(ROOT, "public/previews");

const POSTER_WIDTHS = (process.env.POSTER_WIDTHS || "240,360,480,720")
    .split(",").map(s => parseInt(s.trim(), 10)).filter(Boolean);
const POSTER_MAXW = Math.max(...POSTER_WIDTHS);

const POSTER_FORMATS = (process.env.POSTER_FORMATS || "avif,webp,jpeg")
    .split(",").map(s => s.trim().toLowerCase()).filter(Boolean);

const POSTER_QUALITY_AVIF = parseInt(process.env.POSTER_QUALITY_AVIF || "50", 10);
const POSTER_QUALITY_WEBP = parseInt(process.env.POSTER_QUALITY_WEBP || "74", 10);
const POSTER_QUALITY_JPEG = parseInt(process.env.POSTER_QUALITY_JPEG || "78", 10);

/** ---------- Tiny CLI progress (no deps) ---------- */
class ProgressBar {
    constructor(label, total) {
        this.label = label;
        this.total = Math.max(1, total || 1);
        this.curr = 0;
        this.start = Date.now();
        this.lastRender = 0;
        this.enabled = process.stdout.isTTY === true;
    }
    tick(n = 1) { this.curr += n; this.render(); }
    set(v) { this.curr = Math.min(this.total, Math.max(0, v)); this.render(true); }
    fmt() {
        const width = 24;
        const ratio = Math.min(1, this.curr / this.total);
        const filled = Math.round(ratio * width);
        const bar = "█".repeat(filled) + "░".repeat(width - filled);
        const pct = (ratio * 100).toFixed(1).padStart(5);
        const eta = this.eta();
        return `${this.label} [${bar}] ${pct}%  ${this.curr}/${this.total}${eta ? `  ETA ${eta}` : ""}`;
    }
    eta() {
        if (this.curr <= 0) return "";
        const elapsed = (Date.now() - this.start) / 1000;
        const rate = this.curr / elapsed;
        if (!rate) return "";
        const remain = (this.total - this.curr) / rate;
        if (!Number.isFinite(remain) || remain < 0) return "";
        if (remain < 60) return `${Math.round(remain)}s`;
        const m = Math.floor(remain / 60), s = Math.round(remain % 60);
        return `${m}m ${s}s`;
    }
    render(force = false) {
        const now = Date.now();
        if (!force && now - this.lastRender < 50) return;
        this.lastRender = now;
        const line = this.fmt();
        if (this.enabled) {
            process.stdout.write("\r" + line.padEnd(process.stdout.columns || 100));
            if (this.curr >= this.total) process.stdout.write("\n");
        } else {
            if (this.curr === 0 || this.curr === this.total || this.curr % Math.ceil(this.total / 10) === 0) {
                console.log(line);
            }
        }
    }
}

/** ---------- Small utils ---------- */
function extFromUrl(u) {
    if (!u) return null;
    const clean = u.split("?")[0].split("#")[0];
    const m = clean.match(/\.([a-z0-9]+)$/i);
    return m ? m[1].toLowerCase() : null;
}
function isImageUrl(u) {
    const ext = extFromUrl(u);
    return !!ext && /^(jpe?g|png|webp|avif)$/i.test(ext);
}
function plainExcerpt(text, n = 320) {
    if (!text) return "";
    const t = String(text)
        .replace(/\r\n?|\n/g, " ")
        .replace(/\s+/g, " ")
        .replace(/[*_`>#~]|\\|\[(.*?)\]\((.*?)\)/g, "$1");
    return t.length > n ? t.slice(0, n - 1).trimEnd() + "…" : t;
}
function unescapeAmp(u) { return typeof u === "string" ? u.replace(/&amp;/g, "&") : u; }

function pickRedditPreview(p) {
    const img0 = p?.preview?.images?.[0];
    const res = Array.isArray(img0?.resolutions) ? img0.resolutions : [];
    const srcs = res.map(r => ({ url: unescapeAmp(r.url), w: r.width, h: r.height }))
        .filter(x => x.url && x.w && x.h);
    const srcBig = img0?.source?.url ? { url: unescapeAmp(img0.source.url), w: img0.source.width, h: img0.source.height } : null;
    if (!srcs.length && !srcBig) return null;
    const ladder = srcBig ? [...srcs, srcBig] : srcs;
    const pick = ladder.find(x => x.w >= 360 && x.w <= 520) || ladder[Math.min(1, ladder.length - 1)] || ladder[0];
    return { url: pick?.url || null, w: pick?.w || null, h: pick?.h || null };
}

// parse embedded images from selftext (markdown + bare links)
function extractImageLinksFromSelftext(p) {
    const raw = String(p?.selftext || "");
    if (!raw.trim()) return [];
    const out = []; const seen = new Set(); const clean = raw.replace(/&amp;/g, "&");
    for (const m of clean.matchAll(/!\[[^\]]*?\]\((https?:\/\/[^\s)]+)\)/gi)) {
        const u = m[1]; if (/\.(?:jpe?g|png|webp|gif|avif)(?:\?|#|$)/i.test(u) && !seen.has(u)) { seen.add(u); out.push(u); }
    }
    for (const m of clean.matchAll(/\[[^\]]*?\]\((https?:\/\/[^\s)]+)\)/gi)) {
        const u = m[1]; if (/\.(?:jpe?g|png|webp|gif|avif)(?:\?|#|$)/i.test(u) && !seen.has(u)) { seen.add(u); out.push(u); }
    }
    for (const m of clean.matchAll(/https?:\/\/\S+/gi)) {
        const u = m[0].replace(/[)\],.]+$/, "");
        if (/\.(?:jpe?g|png|webp|gif|avif)(?:\?|#|$)/i.test(u) && !seen.has(u)) { seen.add(u); out.push(u); }
    }
    return out;
}

/** ---------- Local posters (single-format ladder) ---------- */
async function fileExists(fp) { try { await fs.access(fp); return true; } catch { return false; } }
async function ensureDir(p) { try { await fs.mkdir(p, { recursive: true }); } catch { } }
function makeLocalPreviewPaths(id) {
    const dir = path.join(PREVIEWS_DIR, id);
    const make = (ext, w) => path.join(dir, `${id}@${w}w.${ext}`);
    return { dir, make };
}
async function downloadToTemp(url) {
    const { createWriteStream } = await import("node:fs");
    const os = await import("node:os");
    const crypto = await import("node:crypto");
    const isHttps = /^https:/i.test(url);
    const net = await import(isHttps ? "node:https" : "node:http");
    const tmp = path.join(os.tmpdir(), `poster-${crypto.randomBytes(6).toString("hex")}`);
    await new Promise((resolve, reject) => {
        const out = createWriteStream(tmp);
        const req = net.request(url, res => {
            if (res.statusCode && res.statusCode >= 400) { reject(new Error(`HTTP ${res.statusCode} for ${url}`)); return; }
            res.pipe(out);
            out.on("finish", () => out.close(resolve));
        });
        req.on("error", reject);
        req.end();
    });
    return tmp;
}

async function generateLocalPreviewsFromImage(srcPath, id) {
    let sharpMod = null;
    try { sharpMod = (await import("sharp")).default; }
    catch { console.warn("⚠️  'sharp' not installed; cannot generate local previews."); return null; }

    const { dir, make } = makeLocalPreviewPaths(id);
    await ensureDir(dir);

    const sharp = sharpMod(srcPath);
    const meta = await sharp.metadata().catch(() => ({}));
    const w0 = meta.width || POSTER_MAXW;
    const h0 = meta.height || Math.round((POSTER_MAXW * 9) / 16);

    async function writeOne(format, width, outPath) {
        const piped = sharp.clone().resize({ width, withoutEnlargement: true });
        if (format === "avif") return await piped.clone().avif({ quality: POSTER_QUALITY_AVIF }).toFile(outPath);
        if (format === "webp") return await piped.clone().webp({ quality: POSTER_QUALITY_WEBP }).toFile(outPath);
        if (format === "jpeg" || format === "jpg")
            return await piped.clone().jpeg({ quality: POSTER_QUALITY_JPEG, progressive: true }).toFile(outPath);
        throw new Error(`Unsupported format: ${format}`);
    }

    // Choose a format by probing the first width; fall back down the priority list.
    let chosenFormat = null;
    const firstW = POSTER_WIDTHS[0];
    for (const fmt of POSTER_FORMATS) {
        const probeExt = (fmt === "jpeg" ? "jpg" : fmt);
        const probePath = make(probeExt, firstW);
        try {
            await writeOne(fmt, firstW, probePath);
            chosenFormat = fmt;
            break;
        } catch (e) {
            try { await fs.unlink(probePath); } catch { }
        }
    }
    if (!chosenFormat) {
        console.warn("⚠️  No poster format succeeded (tried:", POSTER_FORMATS.join(", "), ")");
        return null;
    }

    // Write the rest of the ladder in the chosen format
    const ext = (chosenFormat === "jpeg" ? "jpg" : chosenFormat);
    const tasks = [];
    for (const w of POSTER_WIDTHS.slice(1)) {
        tasks.push(writeOne(chosenFormat, w, make(ext, w)));
    }
    await Promise.allSettled(tasks);

    // Build single-format src/srcset
    const pickW = POSTER_WIDTHS.includes(360) ? 360 : POSTER_WIDTHS[0];
    const src = `/previews/${id}/${id}@${pickW}w.${ext}`;
    const srcset = POSTER_WIDTHS.map(w => `/previews/${id}/${id}@${w}w.${ext} ${w}w`).join(", ");

    return {
        src,
        srcset,
        sizes: "(max-width: 640px) 44vw, 320px",
        w: w0, h: h0,
        source: "local",
        format: chosenFormat
    };
}

async function generatePosterFromVideoUrl(videoUrl, id) {
    let sharpMod = null;
    try { sharpMod = (await import("sharp")).default; }
    catch { console.warn("⚠️  'sharp' not installed; cannot resize poster from ffmpeg frame."); return null; }

    const { spawn } = await import("node:child_process");
    const tmpVideo = await downloadToTemp(videoUrl);
    const os = await import("node:os");
    const tmpPng = path.join(os.tmpdir(), `${id}-frame.png`);

    // Status line (TTY-friendly)
    if (process.stdout.isTTY) process.stdout.write(`\rGenerating poster for ${id}…`.padEnd(process.stdout.columns || 100));

    await new Promise((resolve, reject) => {
        const ff = spawn("ffmpeg", ["-y", "-ss", "00:00:01", "-i", tmpVideo, "-frames:v", "1", tmpPng], { stdio: "ignore" });
        ff.on("exit", (code) => code === 0 ? resolve() : reject(new Error(`ffmpeg exit ${code}`)));
        ff.on("error", reject);
    }).catch((e) => {
        console.warn("⚠️  ffmpeg frame extraction failed:", e.message);
    });

    const ladder = await generateLocalPreviewsFromImage(tmpPng, id);
    if (process.stdout.isTTY) process.stdout.write(`\rGenerated poster for ${id} ✅\n`);
    return ladder;
}

/** ---------- Preview builder ---------- */
async function buildPreviewBlock({ post, media_items, id }) {
    // 1) If we already have an image item or a sidecar poster, prefer those.
    const firstImage = media_items?.find(x => x.kind === "image");
    if (firstImage) {
        return {
            src: firstImage.url,
            srcset: null,
            sizes: "(max-width: 640px) 44vw, 320px",
            w: null, h: null,
            source: "r2-image"
        };
    }
    const firstPoster = media_items?.find(x => x.poster && isImageUrl(x.poster));
    if (firstPoster) {
        return {
            src: firstPoster.poster,
            srcset: null,
            sizes: "(max-width: 640px) 44vw, 320px",
            w: null, h: null,
            source: "poster"
        };
    }

    // 2) Reddit preview ladder
    const red = pickRedditPreview(post);
    if (red) return { ...red, sizes: "(max-width: 640px) 44vw, 320px", source: "reddit" };

    // 3) Selftext first image
    const emb = extractImageLinksFromSelftext(post);
    if (emb.length) {
        return { src: emb[0], srcset: null, sizes: "(max-width: 640px) 44vw, 320px", w: null, h: null, source: "selftext" };
    }

    // 4) Generate local poster from the first playable (video/gif) if enabled
    if (ENABLE_LOCAL_POSTERS && media_items?.length) {
        const playable = media_items.find(x => x.kind === "video" || x.kind === "redgiphy" || x.kind === "gif");
        if (playable?.url) {
            const loc = await generatePosterFromVideoUrl(playable.url, id);
            if (loc) return loc;
        }
    }

    return null;
}

/** ---------- R2 list + index ---------- */
function assertR2Config() {
    if (!R2_ENDPOINT || !R2_ACCESS_KEY_ID || !R2_SECRET_ACCESS_KEY || !R2_BUCKET) {
        throw new Error("Missing R2 env: R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET");
    }
}
async function listAllR2Keys() {
    if (R2_SKIP_LIST) {
        try {
            const cached = JSON.parse(await fs.readFile(R2_OBJECTS_CACHE, "utf8"));
            if (Array.isArray(cached)) return cached;
        } catch { }
        throw new Error("R2_SKIP_LIST=1 but no cache file found.");
    }
    assertR2Config();
    const s3 = new S3Client({
        region: "auto",
        endpoint: R2_ENDPOINT,
        credentials: { accessKeyId: R2_ACCESS_KEY_ID, secretAccessKey: R2_SECRET_ACCESS_KEY },
        forcePathStyle: true,
    });

    const keys = [];
    let ContinuationToken = undefined;
    let page = 0;
    let totalSoFar = 0;
    const bar = new ProgressBar("Listing R2 objects", 100); // fake total; we just animate

    do {
        const resp = await s3.send(new ListObjectsV2Command({
            Bucket: R2_BUCKET,
            ContinuationToken,
            MaxKeys: 1000,
        }));
        const batch = (resp.Contents || []).map(o => o.Key).filter(Boolean);
        keys.push(...batch);
        totalSoFar += batch.length;
        page++;

        bar.label = `Listing R2 objects (${totalSoFar.toLocaleString()} so far)`;
        bar.tick(1);

        ContinuationToken = resp.IsTruncated ? resp.NextContinuationToken : undefined;
    } while (ContinuationToken);

    bar.set(bar.total);
    try { await fs.writeFile(R2_OBJECTS_CACHE, JSON.stringify(keys, null, 2)); } catch { }
    return keys;
}

/**
 * Index R2 objects for mixed-media galleries with sidecar posters.
 * - Singles (flat): Images/<id>.<ext>, Gifs/<id>.gif, Videos/<id>.mp4, RedGiphys/<id>.mp4
 * - Galleries:      .../<id>/NN.ext   (indices are 2-digit, globally unique per id across folders)
 * - Sidecar posters: Posters/<id>/NN.(avif|webp|jpg|jpeg)
 */
function buildR2Index(keys) {
    const idx = new Map(); // id -> { singles:{image:[],gif,video,redgiphy}, gallery:{image:[],gif:[],video:[],redgiphy:[]}, posters:Map }
    const urlForKey = (k) => MEDIA_BASE_OK ? (PUBLIC_MEDIA_BASE + k) : null;

    const ensureRec = (id) => {
        const prev = idx.get(id);
        if (prev) return prev;
        const rec = {
            singles: { image: [], gif: null, video: null, redgiphy: null },
            gallery: { image: [], gif: [], video: [], redgiphy: [] },
            posters: new Map(),
        };
        idx.set(id, rec);
        return rec;
    };

    for (const key of keys) {
        let m;

        // Galleries
        if ((m = key.match(/^Images\/([^/]+)\/(\d{2})\.([a-z0-9]+)$/i))) {
            const [, id, nnStr, ext] = m; ensureRec(id).gallery.image.push({ n: parseInt(nnStr, 10), url: urlForKey(key), ext: ext.toLowerCase() }); continue;
        }
        if ((m = key.match(/^Gifs\/([^/]+)\/(\d{2})\.gif$/i))) {
            const [, id, nnStr] = m; ensureRec(id).gallery.gif.push({ n: parseInt(nnStr, 10), url: urlForKey(key) }); continue;
        }
        if ((m = key.match(/^Videos\/([^/]+)\/(\d{2})\.mp4$/i))) {
            const [, id, nnStr] = m; ensureRec(id).gallery.video.push({ n: parseInt(nnStr, 10), url: urlForKey(key) }); continue;
        }
        if ((m = key.match(/^RedGiphys\/([^/]+)\/(\d{2})\.mp4$/i))) {
            const [, id, nnStr] = m; ensureRec(id).gallery.redgiphy.push({ n: parseInt(nnStr, 10), url: urlForKey(key) }); continue;
        }

        // Singles
        if ((m = key.match(/^Images\/([^/]+)\.([a-z0-9]+)$/i))) {
            const [, id, ext] = m; ensureRec(id).singles.image.push({ url: urlForKey(key), ext: ext.toLowerCase() }); continue;
        }
        if ((m = key.match(/^Gifs\/([^/]+)\.gif$/i))) {
            const [, id] = m; ensureRec(id).singles.gif = urlForKey(key); continue;
        }
        if ((m = key.match(/^Videos\/([^/]+)\.mp4$/i))) {
            const [, id] = m; ensureRec(id).singles.video = urlForKey(key); continue;
        }
        if ((m = key.match(/^RedGiphys\/([^/]+)\.mp4$/i))) {
            const [, id] = m; ensureRec(id).singles.redgiphy = urlForKey(key); continue;
        }

        // Sidecar posters (accept avif/webp/jpg/jpeg)
        if ((m = key.match(/^Posters\/([^/]+)\/(\d{2})\.(avif|webp|jpe?g)$/i))) {
            const [, id, nnStr] = m; ensureRec(id).posters.set(parseInt(nnStr, 10), urlForKey(key)); continue;
        }
    }

    // Sort gallery items by index; clean single images list
    for (const [, rec] of idx) {
        for (const kind of ["image", "gif", "video", "redgiphy"]) {
            const arr = rec.gallery[kind]; if (arr.length) arr.sort((a, b) => a.n - b.n);
        }
        if (rec.singles.image.length) {
            const seen = new Set();
            rec.singles.image = rec.singles.image.map(i => i.url).filter(u => !!u && !seen.has(u) && seen.add(u));
        }
    }
    return idx;
}

/** ---------- Posts IO ---------- */
async function loadOrderIndex(csvPath) {
    const raw = await fs.readFile(csvPath, "utf8").catch(() => "");
    if (!raw.trim()) return new Map();
    const map = new Map();
    for (const line of raw.split(/\r?\n/)) {
        if (!line.trim()) continue;
        const cells = line.split(",").map(s => s?.trim().replace(/^"(.*)"$/, "$1"));
        const [idxStr, _url, id] = cells;
        const idx = Number(idxStr);
        if (Number.isFinite(idx) && id) map.set(id, idx);
    }
    return map;
}
const ORDER_INDEX = await loadOrderIndex(ORDERED_CSV);

async function loadPosts(rootDir) {
    const entries = await fs.readdir(rootDir, { withFileTypes: true });
    const out = [];
    for (const ent of entries) {
        const full = path.join(rootDir, ent.name);
        if (ent.isDirectory()) {
            const sub = ent.name;
            const files = await fs.readdir(full);
            for (const f of files) {
                if (!f.endsWith(".json")) continue;
                const stem = path.parse(f).name;
                const raw = await fs.readFile(path.join(full, f), "utf8").catch(() => "");
                if (!raw.trim()) continue;
                const p = JSON.parse(raw);
                out.push({ __stem: stem, __folder: sub, __rel: `${sub}/${f}`, ...p });
                await fs.writeFile(path.join(POSTS_PUBLIC_DIR, `${stem}.json`), JSON.stringify(p));
            }
        } else if (ent.isFile() && ent.name.endsWith(".json")) {
            const stem = path.parse(ent.name).name;
            const raw = await fs.readFile(full, "utf8").catch(() => "");
            if (!raw.trim()) continue;
            const p = JSON.parse(raw);
            out.push({ __stem: stem, __folder: null, __rel: ent.name, ...p });
            await fs.writeFile(path.join(POSTS_PUBLIC_DIR, `${stem}.json`), JSON.stringify(p));
        }
    }
    return out;
}

/** ---------- Build ---------- */
const posts = await loadPosts(INPUT_DIR);

// List objects (with progress)
let r2Keys = [];
try {
    r2Keys = await listAllR2Keys();
    console.log(`R2: indexed ${r2Keys.length} objects`);
} catch (err) {
    console.warn("⚠️  Could not list R2 objects:", err.message);
}
const r2Index = buildR2Index(r2Keys);

const manifest = [];
const warnings = [];

const postsBar = new ProgressBar("Building manifest", posts.length);

for (const p of posts) {
    const id = p.__stem || p.id || p.name;
    const order_index =
        ORDER_INDEX.get(id) ??
        ORDER_INDEX.get(p.id) ??
        ORDER_INDEX.get(p.name) ??
        null;

    const rec = r2Index.get(id) || null;

    // Build media_items
    let media_items = [];
    let media_types = [];
    let gallery_count = null;
    let media_preview = null;
    let preview = null;
    let preview_width = null, preview_height = null;

    const hasGallery = !!rec && (
        rec.gallery.image.length || rec.gallery.gif.length || rec.gallery.video.length || rec.gallery.redgiphy.length
    );

    if (hasGallery) {
        const merged = [];
        const push = (kind, arr) => { for (const it of arr) merged.push({ index: it.n, kind, url: it.url }); };
        push("image", rec.gallery.image);
        push("gif", rec.gallery.gif);
        push("video", rec.gallery.video);
        push("redgiphy", rec.gallery.redgiphy);
        merged.sort((a, b) => a.index - b.index);

        // Attach sidecar posters
        if (rec.posters?.size) {
            for (const it of merged) {
                const poster = rec.posters.get(it.index);
                if (poster) it.poster = poster;
            }
        }

        media_items = merged;
        media_types = Array.from(new Set(merged.map(x => x.kind)));
        gallery_count = merged.length;

        const firstImage = merged.find(x => x.kind === "image");
        if (firstImage) media_preview = firstImage.url;
        else if (merged[0]?.poster) media_preview = merged[0].poster;

        if (merged.length < 2) {
            warnings.push({ id, note: "Gallery directory has < 2 items; consider collapsing to single.", type: "gallery" });
        }
    } else if (rec) {
        // Singles (priority: video > redgiphy > gif > image)
        if (rec.singles.video) {
            media_items = [{ index: 1, kind: "video", url: rec.singles.video }];
            media_types = ["video"];
        } else if (rec.singles.redgiphy) {
            media_items = [{ index: 1, kind: "redgiphy", url: rec.singles.redgiphy }];
            media_types = ["redgiphy"];
        } else if (rec.singles.gif) {
            media_items = [{ index: 1, kind: "gif", url: rec.singles.gif }];
            media_types = ["gif"];
        } else if (rec.singles.image.length) {
            media_items = [{ index: 1, kind: "image", url: rec.singles.image[0] }];
            media_types = ["image"];
            media_preview = rec.singles.image[0];
        }
    } else {
        // No R2 media — embedded selftext images
        const embedded = extractImageLinksFromSelftext(p);
        if (embedded.length >= 2) {
            media_items = embedded.map((url, i) => ({ index: i + 1, kind: "image", url }));
            media_types = ["image"];
            gallery_count = media_items.length;
            media_preview = embedded[0];
        } else if (embedded.length === 1) {
            media_items = [{ index: 1, kind: "image", url: embedded[0] }];
            media_types = ["image"];
            media_preview = embedded[0];
        }
    }

    // Guarantee an IMAGE preview
    if (media_preview && !isImageUrl(media_preview)) media_preview = null;
    if (!media_preview) {
        const firstImg = media_items.find(x => x.kind === "image");
        if (firstImg && isImageUrl(firstImg.url)) media_preview = firstImg.url;
        else {
            const firstPoster = media_items.find(x => x.poster && isImageUrl(x.poster));
            if (firstPoster) media_preview = firstPoster.poster;
        }
    }
    if (!media_preview) {
        const pr = pickRedditPreview(p);
        if (pr?.url) { media_preview = pr.url; preview_width = pr.w; preview_height = pr.h; }
    }

    // Build responsive preview block (may generate local poster if needed and enabled)
    try {
        preview = await buildPreviewBlock({ post: p, media_items, id });
    } catch { }

    if (!media_preview && preview?.src) media_preview = preview.src;

    if (MEDIA_BASE_OK && media_items.length === 0) {
        warnings.push({ id, note: "No media_items produced for post; will be treated as text.", type: "none" });
    }
    if (media_items.length && !media_preview) {
        warnings.push({ id, note: "Media post lacks an image preview (no still/poster/ladder).", type: "preview" });
    }

    manifest.push({
        id,
        permalink: p.permalink,
        url: p.url,
        link_domain: p.link_domain || null,
        title: p.title,
        selftext_preview: plainExcerpt(p.selftext || ""),
        subreddit: p.subreddit,
        author: p.author,
        flair: p.link_flair_text || p.flair || null,
        created_utc: p.created_utc,
        saved_index: p.saved_utc ?? (order_index ?? null),
        order_index,
        score: p.score,
        num_comments: p.num_comments,

        // New model
        media_items,      // [{ index, kind, url, poster? }]
        media_types,      // ["image","gif","video",...]
        gallery_count,    // number | null

        // Card preview
        media_preview,    // image URL
        preview,          // { src, srcset?, sizes, w?, h?, source }
        preview_width,
        preview_height,
    });

    postsBar.tick(1);
}

/** ---------- Facets (by kinds union) ---------- */
function inc(map, key) { if (!key) return; map.set(key, (map.get(key) || 0) + 1); }
const subFreq = new Map(), authorFreq = new Map(), flairFreq = new Map(),
    domainFreq = new Map(), mediaKindsFreq = new Map();

for (const m of manifest) {
    inc(subFreq, m.subreddit);
    inc(authorFreq, m.author);
    inc(flairFreq, m.flair);
    inc(domainFreq, m.link_domain);
    if (Array.isArray(m.media_types) && m.media_types.length) {
        for (const k of new Set(m.media_types)) inc(mediaKindsFreq, k);
    } else {
        inc(mediaKindsFreq, "none");
    }
}
const toPairs = (map) => Array.from(map, ([name, count]) => ({ name, count }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
const pairsToObj = (pairs) => Object.fromEntries(pairs.map(({ name, count }) => [name, count]));
const facets = {
    subreddits: pairsToObj(toPairs(subFreq)),
    authors: pairsToObj(toPairs(authorFreq)),
    flairs: pairsToObj(toPairs(flairFreq)),
    domains: pairsToObj(toPairs(domainFreq)),
    mediaKinds: pairsToObj(toPairs(mediaKindsFreq)),
};

/** ---------- Write ---------- */
console.log("Writing outputs…");
await fs.writeFile(path.join(INDEX_OUT_DIR, "posts-manifest.json"), JSON.stringify(manifest, null, 2));
await fs.writeFile(path.join(INDEX_OUT_DIR, "facets.json"), JSON.stringify(facets, null, 2));
await fs.writeFile(path.join(INDEX_OUT_DIR, "build-report.json"), JSON.stringify({ posts: manifest.length, warnings }, null, 2));

console.log(`✅ Manifest built: ${manifest.length} posts`);
console.log(`✅ Facets — subs:${Object.keys(facets.subreddits).length} authors:${Object.keys(facets.authors).length} flairs:${Object.keys(facets.flairs).length} domains:${Object.keys(facets.domains).length} mediaKinds:${Object.keys(facets.mediaKinds).length}`);
console.log(warnings.length ? `ℹ️ Warnings: ${warnings.length} (see build-report.json)` : "ℹ️ Warnings: 0");
