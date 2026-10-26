// README screenshots from `burnrate demo`, taken with one headless Chromium
// browser (Edge or Chrome) over the DevTools protocol. Development only.
//
//   burnrate demo --no-browser          (in another terminal)
//   node scripts/readme-screenshots.mjs [--out docs/images] [--base http://127.0.0.1:17377]
//                                       [--browser PATH] [--profile DIR]
//
// Writes hud.png (380 x 580 at 2x), week-card.png (the share button's image)
// and dashboard.png (1440 x 900). Node 22 or later (built-in WebSocket).
import { execSync, spawn } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const args = Object.fromEntries(process.argv.slice(2).reduce((pairs, arg, index, all) => {
  if (arg.startsWith("--")) pairs.push([arg.slice(2), all[index + 1]]);
  return pairs;
}, []));
const out = resolve(args.out || "docs/images");
const base = args.base || "http://127.0.0.1:17377";
const profile = resolve(args.profile || join(tmpdir(), "burnrate-readme-shots"));
const port = 9347;
const candidates = [
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
  "/usr/bin/microsoft-edge",
];
const browserPath = args.browser || candidates.find(existsSync);
if (!browserPath) {
  console.error("No Edge or Chrome found; pass --browser PATH.");
  process.exit(2);
}

const sleep = ms => new Promise(done => setTimeout(done, ms));
mkdirSync(out, { recursive: true });
rmSync(profile, { recursive: true, force: true });
mkdirSync(profile, { recursive: true });
const browser = spawn(browserPath, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--hide-scrollbars", "--force-color-profile=srgb", "about:blank"],
  { stdio: "ignore", windowsHide: true });

// Headless Edge on Windows re-parents its children, so killing the launcher
// leaves processes holding the profile. Stop every process using it.
function stopBrowser() {
  browser.kill();
  if (process.platform !== "win32") return;
  const needle = profile.replaceAll("'", "''");
  try {
    execSync(`powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*${needle}*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"`,
      { stdio: "ignore" });
  } catch {}
}

async function devtools(path, method = "GET") {
  const response = await fetch(`http://127.0.0.1:${port}${path}`, { method });
  return response.json();
}

async function connect() {
  for (let attempt = 0; attempt < 60; attempt++) {
    try { await devtools("/json/version"); break; } catch { await sleep(250); }
  }
  const target = await devtools("/json/new?about:blank", "PUT");
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((done, fail) => { socket.onopen = done; socket.onerror = fail; });
  let next = 0;
  const pending = new Map();
  socket.onmessage = event => {
    const message = JSON.parse(event.data);
    const waiter = pending.get(message.id);
    if (!waiter) return;
    pending.delete(message.id);
    message.error ? waiter.fail(new Error(JSON.stringify(message.error))) : waiter.done(message.result);
  };
  const send = (method, params = {}) => new Promise((done, fail) => {
    const id = ++next;
    pending.set(id, { done, fail });
    socket.send(JSON.stringify({ id, method, params }));
  });
  return { send, close: () => socket.close() };
}

async function evaluate(page, expression) {
  const result = await page.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

async function until(page, expression, ms = 20000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    if (await evaluate(page, expression)) return;
    await sleep(200);
  }
  throw new Error("Timed out waiting for " + expression);
}

async function shot(page, name, { width, height, scale, url, ready }) {
  await page.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: scale, mobile: false });
  await page.send("Page.navigate", { url });
  await until(page, "document.readyState === 'complete'");
  await until(page, ready);
  await sleep(1200);
  const { data } = await page.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(join(out, name), Buffer.from(data, "base64"));
  console.log("wrote", join(out, name));
}

try {
  const page = await connect();
  await page.send("Page.enable");
  await page.send("Runtime.enable");
  await page.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: "dark" }] });
  // Keep the share button's image instead of putting it on the clipboard.
  await page.send("Page.addScriptToEvaluateOnNewDocument", { source: `
    if (navigator.clipboard) navigator.clipboard.write = async items => {
      const blob = await items[0].getType("image/png");
      window.__card = await new Promise(done => { const reader = new FileReader(); reader.onload = () => done(reader.result); reader.readAsDataURL(blob); });
    };` });
  const hudReady = "!!document.querySelector('#hud-repos') && document.body.innerText.includes('$')";
  await shot(page, "hud.png", { width: 380, height: 580, scale: 2, url: base + "/hud", ready: hudReady });
  await evaluate(page, "document.getElementById('hud-share-btn').click()");
  await until(page, "!!window.__card");
  writeFileSync(join(out, "week-card.png"), Buffer.from((await evaluate(page, "window.__card")).split(",")[1], "base64"));
  console.log("wrote", join(out, "week-card.png"));
  await shot(page, "dashboard.png", { width: 1440, height: 900, scale: 1, url: base + "/",
    ready: "document.body.innerText.includes('$')" });
  page.close();
} catch (error) {
  console.error("Screenshots failed:", error.message);
  process.exitCode = 1;
} finally {
  stopBrowser();
}
