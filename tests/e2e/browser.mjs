// Controlled integration smoke: the unpacked extension, real CPU backend and
// FFmpeg are exercised together. Only source acquisition uses a local fixture.
// Chromium's default --mute-audio remains enabled; no sound is sent to speakers.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { createReadStream } from "node:fs";
import { copyFile, mkdir, readFile, readdir, stat, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { installPlaybackObserver, runPlaybackScenarios } from "./playback-scenarios.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const sourceUrl = "https://www.youtube.com/watch?v=nomusic_e2e_fixture";
const usage = "node tests/e2e/browser.mjs --backend http://127.0.0.1:PORT --fixture FIXTURE_DIR --output NEW_DIR --auth-key-file KEY_FILE [--timeout-seconds 300] [--extension DIR] [--playback true]";

function argumentsFrom(argv) {
  const allowed = new Set(["backend", "fixture", "output", "auth-key-file", "timeout-seconds", "extension", "playback"]);
  const args = {};
  for (let index = 0; index < argv.length; index += 2) {
    const name = argv[index].replace(/^--/, "");
    assert.ok(argv[index].startsWith("--") && allowed.has(name), usage);
    assert.ok(argv[index + 1] && !argv[index + 1].startsWith("--"), usage);
    assert.ok(!(name in args), `Duplicate argument: ${name}`);
    args[name] = argv[index + 1];
  }
  for (const name of ["backend", "fixture", "output", "auth-key-file"]) assert.ok(args[name], usage);
  const base = new URL(args.backend);
  assert.ok(base.protocol === "http:" && base.hostname === "127.0.0.1", "Use a loopback HTTP test backend");
  assert.ok(base.pathname === "/" && !base.search && !base.hash && !base.username && !base.password, usage);
  const timeoutSeconds = Number(args["timeout-seconds"] || 300);
  assert.ok(Number.isFinite(timeoutSeconds) && timeoutSeconds >= 30 && timeoutSeconds <= 1800, "Timeout must be 30–1800 seconds");
  assert.ok(args.playback === undefined || args.playback === "true", usage);
  return {
    backend: base.origin,
    fixture: path.resolve(args.fixture, "clip.mp4"),
    output: path.resolve(args.output),
    authKeyFile: path.resolve(args["auth-key-file"]),
    extension: path.resolve(args.extension || path.join(repo, "extension")),
    timeoutSeconds,
    playback: args.playback === "true",
  };
}

const options = argumentsFrom(process.argv.slice(2));
assert.ok((await stat(options.fixture)).isFile(), "Fixture must be a media file");
const operatorKey = (await readFile(options.authKeyFile, "utf8")).trim();
assert.match(operatorKey, /^nm_[0-9a-f]{64}$/, "Smoke key must be a generated operator key");
await mkdir(path.dirname(options.output), { recursive: true });
await mkdir(options.output); // Refuse to reuse a browser profile or old evidence.

const report = {
  startedAt: new Date().toISOString(),
  mode: options.playback ? "controlled-playback" : "controlled-fixture",
  sourceUrl,
  boundaries: {
    actualUnmodifiedExtension: true,
    actualCPUModel: true,
    actualFFmpeg: true,
    fixtureAcquisition: true,
    liveYouTube: false,
    browserAudioOutputMuted: true,
    subjectiveListening: false,
  },
  steps: [],
  network: [],
  pageErrors: [],
};
const started = performance.now();
let context, page, server;
let timedOut = false;
const watchdog = setTimeout(() => {
  timedOut = true;
  console.error(`Browser smoke exceeded ${options.timeoutSeconds} seconds`);
  void context?.close().catch(() => {});
}, options.timeoutSeconds * 1000);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function note(name, data = {}) {
  const item = { name, elapsedSeconds: +((performance.now() - started) / 1000).toFixed(3), ...data };
  report.steps.push(item);
  console.log(JSON.stringify(item));
}

async function until(description, fn, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (!timedOut && Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await sleep(100);
  }
  throw new Error(`Timed out: ${description}`);
}

async function waitForNativeDownload(format, seen = new Set()) {
  const destination = path.join(options.output, `export.${format}`);
  const roots = [
    path.join(options.output, "downloads"),
    path.join(options.output, "profile", "Downloads"),
  ];
  return until(`native ${format} download`, async () => {
    for (const root of roots) {
      let entries;
      try {
        entries = await readdir(root, { withFileTypes: true });
      } catch {
        continue;
      }
      // chrome.downloads may assign a UUID filename in headless Chromium
      // even when the extension supplies a friendly name. The test owns a
      // fresh directory, so any completed regular file is the requested
      // artifact; a .crdownload remains in-flight.
      const candidate = entries.find((entry) =>
        entry.isFile() && !entry.name.endsWith(".crdownload") && !seen.has(`${root}/${entry.name}`));
      if (candidate) {
        const source = path.join(root, candidate.name);
        try {
          const first = await stat(source);
          if (first.size === 0) continue;
          await sleep(100);
          const second = await stat(source);
          if (first.size !== second.size) continue;
          await copyFile(source, destination);
          seen.add(`${root}/${candidate.name}`);
          return destination;
        } catch {
          // Chrome can publish the directory entry before the file is ready.
          // Retry on the next poll instead of treating that transient state as
          // the requested export.
        }
      }
    }
    return false;
  }, 120000);
}

async function backendJson(route) {
  const response = await fetch(`${options.backend}${route}`, {
    signal: AbortSignal.timeout(10000),
    headers: { Authorization: `Bearer ${operatorKey}` },
  });
  assert.equal(response.status, 200, `${route}: ${await response.clone().text()}`);
  return response.json();
}

function fixturePage(boundary) {
  return `<!doctype html><html lang="en"><meta charset="utf-8">
    <title>nomusic controlled fixture</title>
    <style>body{margin:32px;font:18px sans-serif;background:#18202c;color:white}#movie_player{position:relative;width:640px;height:360px}button{margin:10px;padding:10px}</style>
    <h1>Controlled extension smoke</h1><p>Generated media; real CPU processing. Browser output is muted.</p>
    <div id="movie_player"><video controls preload="auto" width="640" height="360" src="/clip.mp4"></video></div>
    <button id="play">Play</button><button id="pause">Pause</button>
    <button id="seek-start">Seek to one second</button><button id="seek-boundary">Seek before chunk boundary</button>
    <script>
      const video = document.querySelector('video');
      document.querySelector('#movie_player').getVideoData = () => ({video_id: 'nomusic_e2e_fixture'});
      document.querySelector('#play').onclick = () => video.play();
      document.querySelector('#pause').onclick = () => video.pause();
      document.querySelector('#seek-start').onclick = () => { video.currentTime = 1; };
      document.querySelector('#seek-boundary').onclick = () => { video.currentTime = ${boundary - 0.3}; };
    </script></html>`;
}

async function startFixtureServer(boundary) {
  const size = (await stat(options.fixture)).size;
  const html = fixturePage(boundary);
  const http = createServer((req, res) => {
    if (req.url === "/") {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
      res.end(html);
      return;
    }
    if (new URL(req.url, "http://fixture.local").pathname !== "/clip.mp4") {
      res.writeHead(404);
      res.end();
      return;
    }
    const match = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range || "");
    const start = match ? Number(match[1]) : 0;
    const end = match?.[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
    if ((req.headers.range && !match) || start > end || start >= size) {
      res.writeHead(416, { "Content-Range": `bytes */${size}` });
      res.end();
      return;
    }
    res.writeHead(match ? 206 : 200, {
      "Content-Type": "video/mp4", "Accept-Ranges": "bytes", "Content-Length": end - start + 1,
      ...(match ? { "Content-Range": `bytes ${start}-${end}/${size}` } : {}),
    });
    const stream = createReadStream(options.fixture, { start, end });
    stream.on("error", () => res.destroy());
    res.on("close", () => stream.destroy());
    stream.pipe(res);
  });
  await new Promise((resolve, reject) => {
    http.once("error", reject);
    http.listen(0, "127.0.0.1", resolve);
  });
  return http;
}

// Runs only in the extension's isolated world. A parallel analyser observes the
// existing output gain; original connect/start methods and audio routing remain
// intact. No network, model, decoding or scheduling results are substituted.
function installAudioObserver() {
  const observation = globalThis.__nomusicSmoke = { starts: [], contexts: [], maxRms: 0, samples: 0, maxSourceAhead: 0 };
  const graphs = new Set();
  const connect = AudioNode.prototype.connect;
  const start = AudioBufferSourceNode.prototype.start;
  AudioNode.prototype.connect = function (target, ...args) {
    const result = connect.call(this, target, ...args);
    if (this instanceof GainNode && target instanceof AudioDestinationNode) {
      const analyser = this.context.createAnalyser();
      analyser.fftSize = 2048;
      connect.call(this, analyser);
      const gain = this;
      const context = this.context;
      const record = { state: context.state, sampleRate: context.sampleRate };
      observation.contexts.push(record);
      if (observation.contexts.length > 64) observation.contexts.shift();
      const data = new Float32Array(analyser.fftSize);
      const graph = { stop: () => {
        clearInterval(timer);
        record.state = context.state;
        gain.disconnect(analyser);
        analyser.disconnect();
        graphs.delete(graph);
      } };
      const timer = setInterval(() => {
        record.state = context.state;
        if (context.state === "closed") { graph.stop(); return; }
        analyser.getFloatTimeDomainData(data);
        let squares = 0;
        for (const value of data) squares += value * value;
        observation.maxRms = Math.max(observation.maxRms, Math.sqrt(squares / data.length));
        observation.samples++;
      }, 50);
      graphs.add(graph);
    }
    return result;
  };
  AudioBufferSourceNode.prototype.start = function (...args) {
    const result = start.apply(this, args);
    const video = document.querySelector("video");
    const ahead = this._nomusicChunkStart - (video?.currentTime || 0);
    observation.maxSourceAhead = Math.max(observation.maxSourceAhead, ahead || 0);
    observation.starts.push({ chunk: this._nomusicIdx, duration: this.buffer?.duration,
      ahead, rate: video?.playbackRate, sampleRate: this.buffer?.sampleRate });
    if (observation.starts.length > 4096) observation.starts.shift();
    return result;
  };
  observation.restore = () => {
    AudioNode.prototype.connect = connect;
    AudioBufferSourceNode.prototype.start = start;
    for (const graph of graphs) graph.stop();
  };
}

function decodeExport(file, format, expectedDuration) {
  const commandOptions = { encoding: "utf8", timeout: 60000, maxBuffer: 1024 * 1024 };
  const media = JSON.parse(execFileSync("ffprobe", ["-v", "error", "-show_format", "-show_streams", "-of", "json", file], commandOptions));
  assert.ok(Math.abs(Number(media.format.duration) - expectedDuration) < 0.3, `${format}: container duration`);
  assert.ok(media.streams.some((stream) => stream.codec_type === "audio"), `${format}: audio stream`);
  if (format === "mp4") assert.ok(media.streams.some((stream) => stream.codec_type === "video"), "MP4: video stream");
  // Decode every audio sample and every video frame, failing on decoder errors.
  execFileSync("ffmpeg", ["-v", "error", "-xerror", "-i", file, "-map", "0", "-f", "null", "-"], commandOptions);
  const pcm = execFileSync("ffmpeg", ["-v", "error", "-xerror", "-i", file, "-map", "0:a:0", "-f", "f32le", "-ac", "1", "-ar", "48000", "pipe:1"], {
    timeout: 60000, maxBuffer: Math.max(32 * 1024 * 1024, (expectedDuration + 5) * 48000 * 4),
  });
  assert.ok(pcm.length > 0 && pcm.length % 4 === 0, `${format}: decoded PCM`);
  let squares = 0;
  for (let index = 0; index < pcm.length; index += 4) {
    const sample = pcm.readFloatLE(index);
    assert.ok(Number.isFinite(sample), `${format}: finite samples`);
    squares += sample * sample;
  }
  const seconds = pcm.length / 4 / 48000;
  const rms = Math.sqrt(squares / (pcm.length / 4));
  assert.ok(Math.abs(seconds - expectedDuration) < 0.3, `${format}: decoded duration`);
  assert.ok(rms > 1e-8, `${format}: nonzero decoded audio`);
  return { durationSeconds: seconds, rms, finite: true, codecs: media.streams.map((stream) => stream.codec_name) };
}

try {
  const readiness = await backendJson("/readyz");
  assert.equal(readiness.ok, true);
  assert.equal(readiness.state, "ready");
  const capabilities = await backendJson("/capabilities");
  assert.match(capabilities.engine.device, /^cpu(?:\s|$)/);
  const boundary = capabilities.defaults.chunk_seconds - capabilities.defaults.chunk_overlap_seconds;
  assert.ok(Number.isFinite(boundary) && boundary > 1, "Usable chunk stride");
  note("ready-cpu-backend", { readiness, device: capabilities.engine.device, boundary });
  server = await startFixtureServer(boundary);
  context = await chromium.launchPersistentContext(path.join(options.output, "profile"), {
    ...(process.env.NOMUSIC_VERIFY_BROWSER_LAUNCHER ? {
      executablePath: process.env.NOMUSIC_VERIFY_BROWSER_LAUNCHER,
      env: { ...process.env, NOMUSIC_VERIFY_BROWSER_HELPER: String(process.pid),
        NOMUSIC_VERIFY_CHROMIUM: chromium.executablePath() },
    } : {}),
    channel: "chromium", headless: true, acceptDownloads: true,
    downloadsPath: path.join(options.output, "downloads"),
    viewport: { width: 1280, height: 800 },
    args: ["--mute-audio", `--disable-extensions-except=${options.extension}`, `--load-extension=${options.extension}`],
  });
  context.setDefaultTimeout(20000);
  await context.tracing.start({ screenshots: true, snapshots: true });
  report.browserVersion = context.browser()?.version();
  const worker = context.serviceWorkers().find((item) => item.url().endsWith("/background.js"))
    || await context.waitForEvent("serviceworker");
  const extensionId = new URL(worker.url()).hostname;
  const manifest = await worker.evaluate(() => chrome.runtime.getManifest());
  assert.equal(manifest.name, "nomusic");
  assert.equal(manifest.manifest_version, 3);
  // Let onInstalled finish writing defaults before replacing the test URL.
  await until("extension initial settings", async () => (await worker.evaluate(() => chrome.storage.sync.get("autoStart"))).autoStart === false);
  await worker.evaluate(() => {
    const records = [];
    const control = { fault: null, delayed: null };
    const originalFetch = globalThis.fetch.bind(globalThis);
    globalThis.__nomusicE2ETransport = records;
    globalThis.__nomusicE2EControl = control;
    globalThis.fetch = async (input, init) => {
      const url = new URL(typeof input === "string" ? input : input.url);
      const record = { path: url.pathname, method: init?.method || (typeof input === "string" ? "GET" : input.method || "GET") };
      if (record.path === "/process" && typeof init?.body === "string") {
        try { record.requestBody = JSON.parse(init.body); } catch { record.requestBody = null; }
      }
      records.push(record);
      const chunkMatch = /^\/chunk\/[^/]+\/(\d+)$/.exec(record.path);
      const chunkIndex = chunkMatch ? Number(chunkMatch[1]) : null;
      const held = chunkIndex !== null && control.delayed?.index === chunkIndex && !control.delayed.entered
        ? control.delayed : null;
      if (held) {
        held.entered = true;
        try {
          const response = await originalFetch(input, init);
          held.fetched = true;
          await new Promise((resolve) => {
            const timer = setInterval(() => {
              if (held.released) {
                clearInterval(timer);
                resolve();
              }
            }, 10);
          });
          held.delivered = true;
          return response;
        } catch (error) {
          held.fetched = true;
          throw error;
        }
      }
      if (chunkIndex !== null && control.fault?.index === chunkIndex && control.fault.remaining > 0) {
        control.fault.remaining--;
        record.status = 503;
        record.faultName = control.fault.name;
        return new Response("Injected playback regression fault", {
          status: 503,
          headers: { "Cache-Control": "no-store" },
        });
      }
      try {
        const response = await originalFetch(input, init);
        record.status = response.status;
        if (record.path === "/process" && response.ok) {
          record.body = await response.clone().json();
        }
        return response;
      } catch (error) {
        record.error = String(error);
        throw error;
      }
    };
  });
  const popup = await context.newPage();
  popup.on("pageerror", (error) => report.pageErrors.push(`popup: ${error}`));
  await popup.goto(`chrome-extension://${extensionId}/popup.html`);
  await popup.getByText("operator key required", { exact: true }).waitFor();
  await popup.locator("#backend").fill(options.backend);
  await popup.locator("#operatorKey").fill(operatorKey);
  await popup.locator("#saveAuth").click();
  await until("trusted popup connects", async () => {
    const error = await popup.locator("#err").textContent();
    assert.equal(error, "", error || "Popup setup failed");
    return await popup.locator("#status").evaluate((element) => element.classList.contains("ok"));
  });
  assert.equal(await popup.locator("#backend").inputValue(), options.backend);
  assert.equal(await popup.locator("#operatorKey").inputValue(), "");
  const ping = await popup.evaluate(() => chrome.runtime.sendMessage({ type: "ping-backend" }));
  assert.equal(ping.ok, true, "Actual service-worker backend ping");
  await popup.screenshot({ path: path.join(options.output, "settings.png") });
  await popup.close();
  note("actual-extension-popup-and-background", { manifestVersion: manifest.manifest_version, ping });

  page = await context.newPage();
  page.on("pageerror", (error) => report.pageErrors.push(String(error)));
  let requestSerial = 0;
  const requests = new Map();
  page.on("request", (request) => {
    if (request.url().startsWith(`${options.backend}/`)) {
      requests.set(request, { id: ++requestSerial, path: new URL(request.url()).pathname });
    }
  });
  function expectAborts(reason) {
    for (const record of requests.values()) record.expectedAbort = reason;
  }
  function expectHttp(request, status, reason) {
    const record = requests.get(request);
    assert.ok(record, "Injected request is tracked");
    record.expectedHttp = { status, reason };
  }
  page.on("response", (response) => {
    if (response.url().startsWith(`${options.backend}/`)) {
      const path = new URL(response.url()).pathname;
      report.network.push({ ...requests.get(response.request()), path, status: response.status() });
    }
  });
  page.on("requestfailed", (request) => {
    if (request.url().startsWith(`${options.backend}/`)) report.network.push({ ...requests.get(request), path: new URL(request.url()).pathname, failed: request.failure() });
    requests.delete(request);
  });
  page.on("requestfinished", (request) => requests.delete(request));
  const cdp = await context.newCDPSession(page);
  const worlds = new Map();
  cdp.on("Runtime.executionContextCreated", ({ context: world }) => worlds.set(world.id, world));
  cdp.on("Runtime.executionContextDestroyed", ({ executionContextId }) => worlds.delete(executionContextId));
  cdp.on("Runtime.executionContextsCleared", () => worlds.clear());
  await cdp.send("Runtime.enable");
  await page.goto(`http://127.0.0.1:${server.address().port}/`, { waitUntil: "domcontentloaded" });
  await page.locator(".nomusic-btn").waitFor();
  await page.waitForFunction(() => document.querySelector("video")?.readyState >= 2);
  const duration = await page.locator("video").evaluate((video) => video.duration);
  assert.ok(duration > boundary + 1, "Fixture must extend beyond the first chunk boundary");
  const world = await until("extension execution world", () => [...worlds.values()].find((item) => item.origin === `chrome-extension://${extensionId}` && !item.auxData?.isDefault));
  async function isolated(expression) {
    const response = await cdp.send("Runtime.evaluate", { expression, contextId: world.id, returnByValue: true, awaitPromise: true });
    assert.ok(!response.exceptionDetails, JSON.stringify(response.exceptionDetails));
    return response.result.value;
  }
  await isolated(`(${installAudioObserver.toString()})()`);
  if (options.playback) await isolated(`(${installPlaybackObserver.toString()})()`);
  const bridge = await page.evaluate(() => ({ volume: window.__nomusicVolumePatched, source: window.__nomusicSourceUrlBridge }));
  assert.deepEqual(bridge, { volume: true, source: true });
  note("actual-content-scripts", { bridge, duration });
  await page.locator("#play").click();
  const clickedAt = performance.now();
  await page.locator(".nomusic-btn").click();
  const submitted = await until("authenticated process request", async () => {
    const record = (await worker.evaluate(() => globalThis.__nomusicE2ETransport || []))
      .find((item) => item.path === "/process" && item.status === 200 && item.body);
    return record || false;
  });
  assert.equal(submitted.requestBody?.url, sourceUrl, "Actual page bridge resolved the controlled source");
  const job = submitted.body;
  assert.equal(job.chunks_ready, 0, "Fresh job must require real inference");
  report.jobId = job.job_id;
  note("extension-submitted-fresh-job", { jobId: job.job_id });
  const audioState = () => isolated("({maxRms:__nomusicSmoke.maxRms,samples:__nomusicSmoke.samples,starts:__nomusicSmoke.starts,contexts:__nomusicSmoke.contexts.map(context=>context.state),maxSourceAhead:__nomusicSmoke.maxSourceAhead})");
  async function measuredAudio(description) {
    return until(description, async () => {
      const observed = await audioState();
      return observed.maxRms > 1e-8 && observed.samples >= 4 && observed.starts.length ? observed : false;
    }, options.timeoutSeconds * 1000);
  }
  const firstAudio = await measuredAudio("first real processed audio");
  note("processed-audio-observed-before-output-mute", { secondsAfterClick: +((performance.now() - clickedAt) / 1000).toFixed(3), ...firstAudio });
  const time = await page.locator("video").evaluate((video) => video.currentTime);
  await page.waitForFunction((previous) => document.querySelector("video").currentTime > previous + 0.5, time);
  assert.deepEqual(await page.locator("video").evaluate((video) => ({ volume: video.volume, blocked: video.dataset.nomusicVolBlock })), { volume: 0, blocked: "1" });
  const ready = await until("all real CPU chunks", async () => {
    const status = await backendJson(`/status/${job.job_id}`);
    assert.notEqual(status.state, "error", JSON.stringify(status));
    return status.state === "ready" ? status : false;
  }, options.timeoutSeconds * 1000);
  assert.ok(ready.chunks_ready >= 2, "At least two real chunks");
  assert.ok(Math.abs(ready.duration_seconds - duration) < 0.3);
  if (options.playback) note("authenticated-status-polling", {
    statusRequests: (await worker.evaluate(() => globalThis.__nomusicE2ETransport || []))
      .filter((item) => item.path.startsWith("/status/")).length,
  });
  await page.waitForFunction(() => document.querySelector(".nomusic-btn__label")?.textContent === "nomusic on");
  note("all-chunks-ready", { chunks: ready.chunks_ready, duration: ready.duration_seconds });

  await page.locator("#pause").click();
  expectAborts("baseline backward seek replaces playback window");
  await page.locator("#seek-start").click();
  await page.waitForFunction(() => !document.querySelector("video").seeking && Math.abs(document.querySelector("video").currentTime - 1) < 0.1);
  await isolated("__nomusicSmoke.maxRms=0;__nomusicSmoke.samples=0");
  const priorStarts = (await audioState()).starts.length;
  await page.locator("#play").click();
  const resumed = await measuredAudio("audio after backward seek and resume");
  assert.ok(resumed.starts.slice(priorStarts).some((start) => start.chunk === 0));
  note("backward-seek-and-resume", { audio: resumed });
  await page.locator("#pause").click();
  await sleep(200); // Let already-rendered audio quanta drain before measuring.
  const pausedAt = await page.locator("video").evaluate((video) => video.currentTime);
  await isolated("__nomusicSmoke.maxRms=0;__nomusicSmoke.samples=0");
  await sleep(400);
  assert.equal(await page.locator("video").evaluate((video) => video.paused), true);
  assert.ok(Math.abs(await page.locator("video").evaluate((video) => video.currentTime) - pausedAt) < 0.05);
  assert.ok((await audioState()).maxRms < 1e-7, "Processed audio stops on pause");
  note("pause-stops-video-and-processed-audio");

  expectAborts("baseline boundary seek replaces playback window");
  await page.locator("#seek-boundary").click();
  await page.waitForFunction(() => !document.querySelector("video").seeking);
  const beforeBoundary = (await audioState()).starts.length;
  await page.locator("#play").click();
  await page.waitForFunction((position) => document.querySelector("video").currentTime > position + 0.05, boundary);
  await isolated("__nomusicSmoke.maxRms=0;__nomusicSmoke.samples=0");
  const afterBoundary = await measuredAudio("fresh audio after chunk boundary");
  assert.ok(afterBoundary.starts.slice(beforeBoundary).some((start) => start.chunk === 1));
  await page.locator("#pause").click();
  note("forward-seek-and-chunk-boundary", { audio: afterBoundary });
  await page.screenshot({ path: path.join(options.output, "processed.png") });

  // Export URLs become authenticated worker-owned downloads in the transport
  // layer. The earlier key/config layers intentionally defer that boundary;
  // keep their browser smoke focused on processing and playback, while later
  // layers run the same real export checks once backend-client.js is present.
  const exportTransport = await readFile(path.join(options.extension, "button.js"), "utf8")
    .then((source) => source.includes('backendRequest("export-submit"'), () => false);
  if (exportTransport) {
    const nativeDownloads = new Set();
    for (const [format, label] of [["mp3", "MP3 — audio only"], ["mp4", "480p"]]) {
      await page.locator(".nomusic-btn__dl").click();
      await page.getByRole("button", { name: label, exact: true }).click();
      const file = await waitForNativeDownload(format, nativeDownloads);
      note(`${format}-export-decoded`, { bytes: (await stat(file)).size, ...decodeExport(file, format, ready.duration_seconds) });
    }
  } else {
    note("exports-deferred-until-authenticated-export-transport");
  }
  if (options.playback) {
    await runPlaybackScenarios({ page, worker, isolated, audioState, until, sleep, note,
      report, options, ready, boundary, expectAborts, expectHttp });
  } else {
    expectAborts("baseline toggle off disposes session");
    await page.locator(".nomusic-btn").click();
    await until("audio context disposed", async () => (await audioState()).contexts.every((state) => state === "closed"));
    assert.deepEqual(await page.locator("video").evaluate((video) => ({ volume: video.volume, blocked: video.dataset.nomusicVolBlock || "" })), { volume: 1, blocked: "" });
    note("toggle-off-restores-source-and-closes-audio");
  }
  await isolated("__nomusicSmoke.restore()");
  const transport = await worker.evaluate(() => globalThis.__nomusicE2ETransport || []);
  assert.ok(transport.some((item) => item.path.startsWith("/process") && item.status === 200));
  assert.ok(transport.some((item) => item.path.startsWith("/status/") && item.status === 200), "Authenticated status polling ran");
  assert.ok(transport.some((item) => item.path.startsWith("/chunk/") && item.status === 200), "Authenticated chunk transport ran");
  assert.ok(transport.every((item) => !item.path.includes(operatorKey)), "Operator key never entered a request URL");
  const injectedHttpFault = (item) => item.expectedHttp?.status === item.status;
  assert.deepEqual(report.network.filter((item) =>
    (item.status >= 400 && !injectedHttpFault(item)) || item.failed), [],
  "The fixture page must not have unexpected backend failures");
  assert.deepEqual(report.pageErrors, [], "No fixture or extension page errors");
  assert.equal(timedOut, false);
  report.passed = true;
  note("controlled-browser-smoke-passed");
} catch (error) {
  report.passed = false;
  report.error = error.stack || String(error);
  process.exitCode = 1;
  console.error(report.error);
  await page?.screenshot({ path: path.join(options.output, "failure.png"), timeout: 5000 }).catch(() => {});
} finally {
  clearTimeout(watchdog);
  if (context) {
    await context.tracing.stop({ path: path.join(options.output, "trace.zip") }).catch(() => {});
    await context.close().catch((error) => {
      report.passed = false;
      report.cleanupError = String(error);
      process.exitCode = 1;
    });
  }
  if (server) {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
  report.finishedAt = new Date().toISOString();
  await writeFile(path.join(options.output, "report.json"), `${JSON.stringify(report, null, 2)}\n`);
  console.log(`RESULT ${path.join(options.output, "report.json")}`);
}
