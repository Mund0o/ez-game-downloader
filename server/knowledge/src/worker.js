// EZ Game Downloader — shared crash knowledge (Cloudflare Worker + D1).
//
// POST /v1/report     a sealed report from an app: what fixed which failure. Each report is encrypted with a fresh
//                     random key (X25519 with a new ephemeral key every time → HKDF-SHA256 → AES-256-GCM) that only
//                     this worker can open: no one on the way — and no other user — can read it.
// GET  /v1/knowledge  what every app may know: failure pattern → fix → on how many different networks it worked,
//                     signed with Ed25519 so apps reject anything this worker did not publish.
//
// Nothing personal is kept: no IP address (only a salted hash of its network, to count confirmations from different
// places and to rate-limit), no game titles (apps never send them), no machine ids. Every field of a report is
// checked against a strict schema; anything else is refused. Apps check everything again before using it, and a
// shared fix can only toggle known, harmless settings (see ezgd/library/tracer.py: clean_record).

const MAX_BODY = 64 * 1024;
const MAX_RECORDS = 20;
const PER_HOUR = 40;                     // records per network per hour
const PUBLISH_EVERY = 600;               // seconds the signed knowledge is cached
const MAX_ENTRIES = 3000;
const PER_SIGNATURE = 5;                 // fixes published per failure pattern
const AAD = new TextEncoder().encode("ezgd-crowd-v1");
const INFO = new TextEncoder().encode("ezgd crowd v1");

const SCHEMA = `
CREATE TABLE IF NOT EXISTS item (hash TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS confirm (sig_hash TEXT NOT NULL, fix_hash TEXT NOT NULL, who TEXT NOT NULL,
  vendor TEXT NOT NULL, at INTEGER NOT NULL, PRIMARY KEY (sig_hash, fix_hash, who));
CREATE TABLE IF NOT EXISTS rate (k TEXT PRIMARY KEY, n INTEGER NOT NULL, until INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS published (k TEXT PRIMARY KEY, doc TEXT NOT NULL, sig TEXT NOT NULL, at INTEGER NOT NULL);`;

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    try {
      await ready(env);
      if (req.method === "POST" && url.pathname === "/v1/report") return await report(req, env);
      if (req.method === "GET" && url.pathname === "/v1/knowledge") return await knowledge(env);
      return text("not found", 404);
    } catch (e) {
      if (!(e instanceof Refused)) console.error(String((e && e.stack) || e));      // (no request data in it)
      return text(e instanceof Refused ? e.message : "error", e instanceof Refused ? e.status : 500);
    }
  },
};

class Refused extends Error {
  constructor(message, status = 400) { super(message); this.status = status; }
}

const text = (body, status = 200) => new Response(body, { status, headers: { "content-type": "text/plain" } });
const now = () => Math.floor(Date.now() / 1000);

let schemaReady = false;
async function ready(env) {
  if (schemaReady) return;
  for (const stmt of SCHEMA.split(";").map(s => s.trim()).filter(Boolean)) await env.DB.prepare(stmt).run();
  schemaReady = true;
}

// ---------------------------------------------------------------- encoding helpers
const b64d = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
const b64e = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const b64u = s => b64d(s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - s.length % 4) % 4));

function canonical(v) {                   // JSON with sorted keys: the same value always hashes the same
  if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
  if (v && typeof v === "object") return "{" + Object.keys(v).sort().map(k => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
  return JSON.stringify(v);
}

async function sha256(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------- the sealed report
async function open(sealed, env) {
  const jwk = JSON.parse(env.KEY_X25519);
  const priv = await crypto.subtle.importKey("jwk", jwk, { name: "X25519" }, false, ["deriveBits"]);
  const epkRaw = b64d(sealed.epk), nonce = b64d(sealed.nonce), ct = b64d(sealed.ct);
  if (epkRaw.length !== 32 || nonce.length !== 12 || ct.length < 16) throw new Refused("bad envelope");
  const epk = await crypto.subtle.importKey("raw", epkRaw, { name: "X25519" }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: "X25519", public: epk }, priv, 256);
  const salt = new Uint8Array(64);
  salt.set(epkRaw, 0);
  salt.set(b64u(jwk.x), 32);
  const base = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt, info: INFO }, base,
                                            { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  let plain;
  try {
    plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv: nonce, additionalData: AAD }, key, ct);
  } catch {
    throw new Refused("not sealed for this server");
  }
  return JSON.parse(new TextDecoder().decode(plain));
}

// ---------------------------------------------------------------- the schema (anything else is refused)
const WORD = /^[a-z0-9_]{1,40}$/;
const DLL = /^[A-Za-z0-9_.-]{1,64}$/;
const ENV_NAME = /^(PROTON_[A-Z0-9_]{1,40}|DXVK_[A-Z0-9_]{1,40}|VKD3D_[A-Z0-9_]{1,40}|RADV_[A-Z0-9_]{1,40}|WINE_CPU_TOPOLOGY|WINE_FULLSCREEN_FSR(_[A-Z]+)?|WINE_LARGE_ADDRESS_AWARE|WINEESYNC|WINEFSYNC|PULSE_LATENCY_MSEC|LC_ALL|LANG|DRI_PRIME|__NV_PRIME_RENDER_OFFLOAD|__GLX_VENDOR_LIBRARY_NAME|__VK_LAYER_NV_optimus|SDL_[A-Z0-9_]{1,40})$/;
const ENV_NEVER = /PATH|PRELOAD|ICD|LOADER|DRIVER_FILES|LIBRARY_PATH|FILE|_DIR$|_LOG|HUD_CONFIG|CMD|_BIN$|EXEC|DYNAMIC_API|REMOTE_DEBUG/;
const ENV_VALUE = /^[A-Za-z0-9 _.,:;=+\-()\[\]#@]{0,160}$/;
const ARGS = /^[A-Za-z0-9 _.,:=+\-/]{0,200}$/;
const MODES = new Set(["", "n", "b", "n,b", "b,n", "d"]);
const COMPAT = new Set(["use_runtime", "protonfixes", "report_app_id"]);

const str = (v, n) => typeof v === "string" && v.length <= n && !/[<>\u0000-\u001f]/.test(v);
const strs = (v, max, n, rx) => Array.isArray(v) && v.length <= max && v.every(x => str(x, n) && (!rx || rx.test(x)));
const obj = v => v && typeof v === "object" && !Array.isArray(v);

function checkSig(s) {
  if (!obj(s)) return null;
  const allowed = { engine: 40, module: 80, box: 60 };
  const out = {};
  for (const [k, v] of Object.entries(s)) {
    if (k in allowed) { if (!str(v, allowed[k])) return null; out[k] = v; }
    else if (k === "code") {               // an exception / exit code: "c0000005" (as the app writes it) or a number
      if (!((typeof v === "string" && /^[0-9a-f]{1,16}$/.test(v)) || (Number.isInteger(v) && Math.abs(v) < 2 ** 40)))
        return null;
      out[k] = v;
    }
    else if (k === "null") { if (typeof v !== "boolean") return null; out[k] = v; }
    else if (["replaced", "loaders", "missing"].includes(k)) { if (!strs(v, 20, 80)) return null; out[k] = v; }
    else return null;
  }
  return Object.keys(out).length ? out : null;
}

const PROTON_KINDS = new Set(["ge", "cachyos", "valve"]);

function checkFix(f) {
  if (!obj(f) || !str(f.gen_id, 80) || !/^[\w:.\-]{1,80}$/.test(f.gen_id)) return null;
  const known = new Set(["gen_id", "label", "why", "env", "args", "dll_overrides", "options", "fixes_on", "fixes_off",
                         "compat_set", "winetricks", "proton_kind"]);
  if (Object.keys(f).some(k => !known.has(k))) return null;
  const env = f.env ?? {}, dlls = f.dll_overrides ?? {}, compat = f.compat_set ?? {};
  if (!obj(env) || !obj(dlls) || !obj(compat)) return null;
  if (Object.keys(env).length > 8 || !Object.entries(env).every(([k, v]) => ENV_NAME.test(k) && !ENV_NEVER.test(k)
      && typeof v === "string" && ENV_VALUE.test(v))) return null;
  if (Object.keys(dlls).length > 16 || !Object.entries(dlls).every(([k, v]) => DLL.test(k) && MODES.has(v))) return null;
  if (!Object.entries(compat).every(([k, v]) => COMPAT.has(k) && typeof v === "boolean")) return null;
  if (!(typeof (f.args || "") === "string" && ARGS.test(f.args || ""))) return null;
  for (const k of ["options", "fixes_on", "fixes_off", "winetricks"]) if (!strs(f[k] || [], 8, 40, WORD)) return null;
  if (!str(f.label || "", 120) || !str(f.why || "", 400)) return null;
  // a kind of Proton (each computer uses its own build of that kind) — never a folder name
  if (f.proton_kind !== undefined && f.proton_kind !== "" && !PROTON_KINDS.has(f.proton_kind)) return null;
  const kind = f.proton_kind ? { proton_kind: f.proton_kind } : {};
  return { ...kind, gen_id: f.gen_id, label: f.label || "", why: f.why || "", env, args: f.args || "", dll_overrides: dlls,
           options: f.options || [], fixes_on: f.fixes_on || [], fixes_off: f.fixes_off || [], compat_set: compat,
           winetricks: f.winetricks || [] };
}

function checkMachine(m) {
  if (!obj(m)) return "";
  return ["nvidia", "amd", "intel", "other"].includes(m.gpu) ? m.gpu : "";
}

// ---------------------------------------------------------------- endpoints
function network(req) {                  // the sender's network (IPv4 /24, IPv6 /48): one vote per place
  const ip = req.headers.get("cf-connecting-ip") || "";
  return ip.includes(":") ? ip.split(":").slice(0, 3).join(":") : ip.split(".").slice(0, 3).join(".");
}

async function report(req, env) {
  const len = Number(req.headers.get("content-length") || 0);
  if (len > MAX_BODY) throw new Refused("too large", 413);
  const raw = await req.text();
  if (raw.length > MAX_BODY) throw new Refused("too large", 413);
  let sealed;
  try { sealed = JSON.parse(raw); } catch { throw new Refused("not json"); }
  if (!obj(sealed) || sealed.v !== 1 || !str(sealed.epk, 64) || !str(sealed.nonce, 32) || typeof sealed.ct !== "string")
    throw new Refused("bad envelope");
  const payload = await open(sealed, env);
  if (!obj(payload) || payload.v !== 1 || !Array.isArray(payload.records) || payload.records.length > MAX_RECORDS)
    throw new Refused("bad report");

  const who = await sha256(env.SALT + "|" + network(req));
  const hour = Math.floor(now() / 3600);
  const rk = `${who}:${hour}`;
  const used = await env.DB.prepare("SELECT n FROM rate WHERE k = ?").bind(rk).first("n") || 0;
  if (used + payload.records.length > PER_HOUR) throw new Refused("slow down", 429);
  await env.DB.batch([
    env.DB.prepare("INSERT INTO rate (k, n, until) VALUES (?, ?, ?) ON CONFLICT(k) DO UPDATE SET n = n + excluded.n")
      .bind(rk, payload.records.length, (hour + 2) * 3600),
    env.DB.prepare("DELETE FROM rate WHERE until < ?").bind(now()),
  ]);

  let kept = 0;
  const stmts = [];
  for (const r of payload.records) {
    const sig = checkSig(r && r.sig), fix = checkFix(r && r.fix);
    if (!sig || !fix) continue;
    const sigBody = canonical(sig), fixBody = canonical(fix);
    const sh = await sha256("sig|" + sigBody), fh = await sha256("fix|" + fixBody);
    stmts.push(env.DB.prepare("INSERT OR IGNORE INTO item (hash, body) VALUES (?, ?)").bind(sh, sigBody));
    stmts.push(env.DB.prepare("INSERT OR IGNORE INTO item (hash, body) VALUES (?, ?)").bind(fh, fixBody));
    stmts.push(env.DB.prepare("INSERT INTO confirm (sig_hash, fix_hash, who, vendor, at) VALUES (?, ?, ?, ?, ?) " +
                              "ON CONFLICT DO UPDATE SET at = excluded.at, vendor = excluded.vendor")
      .bind(sh, fh, who, checkMachine(r.machine), now()));
    kept++;
  }
  if (stmts.length) {
    // what changed is published within a minute (not on every report: rebuilding is bounded however many arrive)
    stmts.push(env.DB.prepare("UPDATE published SET at = MIN(at, ?) WHERE k = 'latest'").bind(now() - PUBLISH_EVERY + 60));
    await env.DB.batch(stmts);
  }
  return new Response(JSON.stringify({ kept }), { headers: { "content-type": "application/json" } });
}

async function knowledge(env) {
  let row = await env.DB.prepare("SELECT doc, sig, at FROM published WHERE k = 'latest'").first();
  if (!row || row.at < now() - PUBLISH_EVERY) {
    const { results } = await env.DB.prepare(
      "SELECT c.sig_hash, c.fix_hash, COUNT(DISTINCT c.who) AS n, MAX(c.at) AS last, " +
      "GROUP_CONCAT(c.vendor) AS vendors, s.body AS sig, f.body AS fix FROM confirm c " +
      "JOIN item s ON s.hash = c.sig_hash JOIN item f ON f.hash = c.fix_hash " +
      "GROUP BY c.sig_hash, c.fix_hash ORDER BY n DESC, last DESC LIMIT ?").bind(MAX_ENTRIES * 2).all();
    const perSig = {}, entries = [];
    for (const r of results) {
      if ((perSig[r.sig_hash] = (perSig[r.sig_hash] || 0) + 1) > PER_SIGNATURE) continue;
      const vendors = {};
      for (const v of (r.vendors || "").split(",").filter(Boolean)) vendors[v] = (vendors[v] || 0) + 1;
      entries.push({ sig: JSON.parse(r.sig), fix: JSON.parse(r.fix), n: r.n, last: r.last, vendors });
      if (entries.length >= MAX_ENTRIES) break;
    }
    const doc = JSON.stringify({ v: 1, generated: now(), entries });
    const key = await crypto.subtle.importKey("jwk", JSON.parse(env.KEY_ED25519), { name: "Ed25519" }, false, ["sign"]);
    const sig = b64e(await crypto.subtle.sign({ name: "Ed25519" }, key, new TextEncoder().encode(doc)));
    await env.DB.prepare("INSERT INTO published (k, doc, sig, at) VALUES ('latest', ?, ?, ?) " +
                         "ON CONFLICT(k) DO UPDATE SET doc = excluded.doc, sig = excluded.sig, at = excluded.at")
      .bind(doc, sig, now()).run();
    row = { doc, sig };
  }
  return new Response(JSON.stringify({ doc: row.doc, sig: row.sig }), {
    headers: { "content-type": "application/json", "cache-control": "public, max-age=300" },
  });
}
