// Opt-in actual-extension regressions. Observation wrappers call the original
// methods unchanged; only explicitly recorded HTTP faults replace responses.
import assert from "node:assert/strict";

// Serialized into the extension's isolated world before the first selection.
// Weak references and numeric snapshots avoid making the observer itself an
// owner of retired sessions, decoded PCM, AudioNodes or detached videos.
export async function installPlaybackObserver() {
  const { Session } = await import(chrome.runtime.getURL("session.js"));
  const start = Session.prototype.start;
  const handleStatus = Session.prototype.handleStatus;
  const records = [];
  const ids = new WeakMap();
  const observation = globalThis.__nomusicPlayback = {
    samples: [],
    peaks: { pcmBytes: 0, decodedChunks: 0, activeRequests: 0, liveSessions: 0 },
  };
  Session.prototype.start = function (...args) {
    if (!ids.has(this)) {
      const record = { id: records.length + 1, ref: new WeakRef(this), statuses: 0 };
      records.push(record);
      ids.set(this, record);
    }
    return start.apply(this, args);
  };
  Session.prototype.handleStatus = function (...args) {
    const record = ids.get(this);
    if (record) record.statuses++;
    return handleStatus.apply(this, args);
  };
  observation.read = () => {
    const sessions = [];
    for (const record of records) {
      const session = record.ref.deref();
      if (!session) continue;
      const scheduler = session.scheduler;
      const buffers = new Set([...session.chunks.values()].map((entry) => entry.buffer));
      for (const buffer of scheduler?.stretchCache.values() || []) buffers.add(buffer);
      for (const source of scheduler?.activeSources || []) {
        if (source.buffer) buffers.add(source.buffer);
      }
      const pcmBytes = [...buffers].reduce((total, buffer) =>
        total + buffer.length * buffer.numberOfChannels * 4, 0);
      sessions.push({
        id: record.id, statuses: record.statuses, disposed: session.disposed,
        connected: session.video.isConnected, failed: session.failed,
        jobId: session.jobId, backend: session.config.backendUrl,
        time: session.video.currentTime, rate: session.video.playbackRate,
        paused: session.video.paused, held: session.playback.held,
        wantsPlay: session.playback.wantsPlay, streamEnded: session._streamEnded,
        decodedChunks: session.chunks.size, indices: [...session.chunks.keys()],
        activeRequests: session.loader.active.size,
        activeSources: scheduler?.activeSources.size || 0,
        stretchKeys: [...scheduler?.stretchCache.keys() || []],
        pcmBytes, sampleRates: [...new Set([...buffers].map((buffer) => buffer.sampleRate))],
        contextState: scheduler?.audioCtx?.state || null,
        requestAborted: session._requests.signal.aborted,
        gain: scheduler?.gain?.gain.value ?? null,
        bufferTimer: session.bufferTimer != null,
        streamOpen: session.eventSource != null,
      });
    }
    return { created: records.length, live: sessions.filter((session) => !session.disposed), sessions };
  };
  observation.sample = () => {
    const state = observation.read();
    const live = state.live;
    const sample = {
      at: performance.now(), time: live[0]?.time ?? null,
      rate: live[0]?.rate ?? null,
      pcmBytes: live.reduce((sum, session) => sum + session.pcmBytes, 0),
      decodedChunks: live.reduce((sum, session) => sum + session.decodedChunks, 0),
      activeRequests: live.reduce((sum, session) => sum + session.activeRequests, 0),
      liveSessions: live.length,
    };
    for (const key of Object.keys(observation.peaks)) {
      observation.peaks[key] = Math.max(observation.peaks[key], sample[key]);
    }
    observation.samples.push(sample);
    if (observation.samples.length > 2048) observation.samples.shift();
    return sample;
  };
  const timer = setInterval(observation.sample, 250);
  observation.restore = () => {
    clearInterval(timer);
    Session.prototype.start = start;
    Session.prototype.handleStatus = handleStatus;
  };
}

export async function runPlaybackScenarios({ page, worker, isolated, audioState,
  until, sleep, note, report, options, ready, boundary, expectAborts, expectHttp }) {
  assert.ok(ready.duration_seconds >= 175, "Playback suite needs the 180-second fixture");
  const evidence = report.playback = {
    limits: { retainedChunks: 8, activeRequests: 3, pcmBytes: 64 * 1024 * 1024, sourceAheadSeconds: 30 },
    injectedFaults: [], checks: [],
  };
  const read = () => isolated("__nomusicPlayback.read()");
  const live = async () => {
    const state = await read();
    assert.equal(state.live.length, 1, "Exactly one active session owns the video");
    return state.live[0];
  };
  const videoState = () => page.locator("video").evaluate((video) => ({
    time: video.currentTime, paused: video.paused, volume: video.volume,
    muted: video.muted, blocked: video.dataset.nomusicVolBlock || "",
  }));
  const mark = (name, data = {}) => {
    evidence.checks.push({ name, ...data });
    note(name, data);
  };
  async function pause() {
    expectAborts("explicit playback pause");
    await page.locator("#pause").click();
    await page.waitForFunction(() => document.querySelector("video").paused);
  }
  async function seek(time, rate = 1) {
    expectAborts("replace the retained playback window on seek");
    await page.locator("video").evaluate((video, args) => {
      video.playbackRate = args.rate;
      video.currentTime = args.time;
    }, { time, rate });
    await page.waitForFunction((target) => {
      const video = document.querySelector("video");
      return !video.seeking && Math.abs(video.currentTime - target) < 0.3;
    }, time);
  }
  async function freshAudio(description) {
    // Wait out already rendered audio quanta before clearing the observation.
    // Every check thereafter requires new samples, not a historical maximum.
    await sleep(150);
    await isolated("__nomusicSmoke.maxRms=0;__nomusicSmoke.samples=0");
    const before = (await audioState()).starts.length;
    await page.locator("#play").click();
    const result = await until(description, async () => {
      const audio = await audioState();
      const video = await videoState();
      return !video.paused && audio.samples >= 4 && audio.maxRms > 1e-8
        ? { rms: audio.maxRms, samples: audio.samples, newSources: audio.starts.length - before,
          time: video.time } : false;
    });
    return result;
  }
  async function settledWindow() {
    return until("chunk window settled", async () => {
      const state = await live();
      return state.activeRequests === 0 && state.decodedChunks > 0 ? state : false;
    });
  }
  async function off() {
    expectAborts("turn processing off");
    await page.locator(".nomusic-btn").click();
    await until("no active session", async () => (await read()).live.length === 0);
  }
  async function on() {
    await page.locator(".nomusic-btn").click();
    await until("completed artifact status received", async () => {
      const state = await read();
      return state.live.length === 1 && state.live[0].streamEnded ? state.live[0] : false;
    });
  }
  function assertBounds(state) {
    assert.ok(state.decodedChunks <= evidence.limits.retainedChunks, `Decoded window: ${JSON.stringify(state)}`);
    assert.ok(state.activeRequests <= evidence.limits.activeRequests, "Bounded fetch/decode slots");
    assert.ok(state.pcmBytes <= evidence.limits.pcmBytes, "Bounded unique retained PCM bytes");
  }

  // A new session must replay the fully processed real artifact from disk.
  await pause();
  await off();
  await seek(0);
  await on();
  await freshAudio("cached long playback starts");
  const replayStart = await videoState();
  const replaySamples = [];
  let previousStarts = (await audioState()).starts.length;
  await until("75 uninterrupted video seconds across retention windows", async () => {
    const state = await live();
    const audio = await audioState();
    assertBounds(state);
    assert.equal(state.failed, false);
    assert.equal(state.paused, false, "Cached sustained playback must not pause for missing audio");
    // The generated fixture's silent syllable gaps are shorter than this
    // approximately one-second observation window. Reset after every sample:
    // one early audible buffer cannot hide a silent later scheduler window.
    assert.ok(audio.samples >= 4 && audio.maxRms > 1e-8,
      `Fresh processed audio throughout cached playback at ${state.time}s`);
    const starts = audio.starts.slice(previousStarts);
    previousStarts = audio.starts.length;
    replaySamples.push({ time: state.time, pcmBytes: state.pcmBytes, decodedChunks: state.decodedChunks,
      activeRequests: state.activeRequests, indices: state.indices,
      rms: audio.maxRms, audioSamples: audio.samples, sourceStarts: starts });
    if (state.time - replayStart.time >= 75) return true;
    await isolated("__nomusicSmoke.maxRms=0;__nomusicSmoke.samples=0");
    await sleep(900);
    return false;
  }, 120000);
  await pause();
  const afterReplay = await settledWindow();
  assert.equal(afterReplay.indices.includes(0), false, "Old chunk 0 was evicted after long playback");
  mark("sustained-cached-playback-and-memory-window", { videoSeconds: afterReplay.time - replayStart.time,
    samples: replaySamples, sampleRates: afterReplay.sampleRates });

  // Chunk requests originate in the service worker. The browser harness owns
  // a test-only worker fetch interceptor, so faults and delayed responses are
  // injected without restoring page-side backend access.
  const transport = () => worker.evaluate(() => globalThis.__nomusicE2ETransport || []);
  const control = () => worker.evaluate(() => globalThis.__nomusicE2EControl || {});
  const chunkRequests = [];
  const seenFaultRecords = new Set();
  let fault = null;
  let delayed = null;
  async function setFault(next) {
    fault = next;
    await worker.evaluate((value) => { globalThis.__nomusicE2EControl.fault = value; }, next);
  }
  async function setDelayed(next) {
    delayed = next;
    await worker.evaluate((value) => {
      if (value === null && globalThis.__nomusicE2EControl.delayed) {
        globalThis.__nomusicE2EControl.delayed.released = true;
      }
      globalThis.__nomusicE2EControl.delayed = value;
    }, next);
  }
  function syncChunkRequests(records) {
    chunkRequests.length = 0;
    for (const item of records) {
      const match = /^\/chunk\/[^/]+\/(\d+)$/.exec(item.path);
      if (match) chunkRequests.push(Number(match[1]));
    }
  }
  async function collectFaults() {
    const state = (await read()).live[0];
    for (const [recordIndex, item] of (await transport()).entries()) {
      if (!item.faultName || seenFaultRecords.has(recordIndex)) continue;
      seenFaultRecords.add(recordIndex);
      evidence.injectedFaults.push({ name: item.faultName,
        index: Number(item.path.split("/").at(-1)), status: item.status,
        sessionId: state?.id, statuses: state?.statuses,
        streamEnded: state?.streamEnded });
    }
    syncChunkRequests(await transport());
  }
  try {
    const positions = [10, 90, 25, 145, 45, 110];
    const rates = [0.75, 1, 1.25, 1.5];
    for (const [index, position] of positions.entries()) {
      await pause();
      await seek(position, rates[index % rates.length]);
      const audio = await freshAudio(`fresh audio at ${position}s`);
      await collectFaults();
      const state = await settledWindow();
      assertBounds(state);
      mark("seek-rate-and-fresh-audio", { position, rate: rates[index % rates.length], audio,
        pcmBytes: state.pcmBytes, decodedChunks: state.decodedChunks,
        sampleRates: state.sampleRates, stretchKeys: state.stretchKeys });
    }
    assert.ok(chunkRequests.includes(0), "Seeking backward refetched previously evicted chunk 0");
    assert.ok((await audioState()).maxSourceAhead <= 30.1, "Direct arrivals respect the 30-second chunk-start horizon");

    // Settings update the next session only; the current artifact keeps its
    // original backend even during a seek which needs fresh network traffic.
    const sessionBeforeSettings = await live();
    await worker.evaluate(() => chrome.storage.sync.set({ backendUrl: "http://127.0.0.1:1" }));
    await until("settings update observed", () => isolated(`import(chrome.runtime.getURL("settings.js")).then(m=>m.settings.backendUrl==="http://127.0.0.1:1")`));
    await pause();
    await seek(12);
    const settingsAudio = await freshAudio("active configuration survives settings edit");
    const sessionAfterSettings = await live();
    assert.equal(sessionAfterSettings.id, sessionBeforeSettings.id);
    assert.equal(sessionAfterSettings.backend, options.backend);
    await worker.evaluate((backendUrl) => chrome.storage.sync.set({ backendUrl }), options.backend);
    await until("settings restored", () => isolated(`import(chrome.runtime.getURL("settings.js")).then(m=>m.settings.backendUrl===${JSON.stringify(options.backend)})`));
    mark("active-settings-snapshot", { sessionId: sessionAfterSettings.id, audio: settingsAudio });

    // The failed request targets the final chunk of an already READY job.
    await pause();
    const lastIndex = ready.total_chunks - 1;
    await setFault({ name: "final-ready-chunk-first-attempt", index: lastIndex, remaining: 1 });
    const beforeFinal = await live();
    await seek(lastIndex * boundary + 0.2);
    const finalAudio = await freshAudio("final chunk retries without another status poll");
    await collectFaults();
    const finalState = await settledWindow();
    const finalFault = evidence.injectedFaults.find((item) => item.name === fault.name);
    assert.ok(finalFault, "Injected the final-chunk failure");
    assert.equal(finalFault.streamEnded, true);
    assert.equal(finalState.statuses, beforeFinal.statuses, "Retry requires no new status event");
    assert.ok(chunkRequests.filter((index) => index === lastIndex).length >= 2);
    mark("final-chunk-retry-after-ready", { fault: finalFault, audio: finalAudio });

    // Exhaust the current chunk, wait beyond the former 2.5-second error
    // revert, and recover through the actual keyboard-accessible controls.
    await pause();
    await setFault({ name: "current-chunk-exhaustion", index: 0, remaining: 4 });
    await seek(1);
    await page.locator("#play").click();
    await page.getByRole("button", { name: "Retry", exact: true }).waitFor();
    await collectFaults();
    assert.equal(evidence.injectedFaults.filter((item) => item.name === fault.name).length, 4);
    await sleep(2800);
    assert.equal(await page.locator(".nomusic-btn").getAttribute("data-state"), "error");
    const suppressed = await videoState();
    assert.equal(suppressed.paused, true);
    assert.equal(suppressed.volume, 0);
    assert.equal(suppressed.blocked, "1");
    const failed = await live();
    assert.equal(failed.failed, true);
    assert.equal(failed.activeSources, 0);
    await setFault(null);
    const retry = page.getByRole("button", { name: "Retry", exact: true });
    await retry.focus();
    await retry.press("Enter");
    const retryAudio = await freshAudio("keyboard Retry restores processed audio");
    await until("retry reuses completed artifact", async () => (await live()).streamEnded);
    mark("terminal-error-and-keyboard-retry", { attempts: 4, persistentAfterMs: 2800, audio: retryAudio });

    await pause();
    await setFault({ name: "return-to-original-after-error", index: lastIndex, remaining: 4 });
    await seek(lastIndex * boundary + 0.2);
    await page.locator("#play").click();
    await page.getByRole("button", { name: "Return to original", exact: true }).waitFor();
    await collectFaults();
    await page.locator("video").evaluate((video) => { video.volume = 0; video.muted = true; video.pause(); });
    const original = page.getByRole("button", { name: "Return to original", exact: true });
    expectAborts("return to original after terminal failure");
    await original.focus();
    await original.press("Enter");
    await until("returned session is disposed", async () => (await read()).live.length === 0);
    const returned = await videoState();
    assert.equal(returned.volume, 0);
    assert.equal(returned.muted, true);
    assert.equal(returned.paused, true);
    assert.equal(returned.blocked, "");
    mark("return-to-original-preserves-latest-zero-mute-and-pause", returned);
    await setFault(null);

    // Restore volume for audible-signal measurement, still muted externally by
    // Chromium. Pause intent while selected must survive setup and buffering.
    await page.locator("video").evaluate((video) => { video.volume = 0.6; video.muted = false; });
    await seek(20);
    await on();
    await settledWindow();
    assert.equal((await videoState()).paused, true, "Selecting while user-paused stays paused");
    await freshAudio("play after an initially paused selection");
    await pause();
    const beforeMove = await live();
    await page.evaluate(() => {
      const host = document.createElement("div");
      host.id = "miniplayer-fixture";
      host.style.cssText = "position:relative;width:640px;height:360px";
      document.body.appendChild(host);
      host.appendChild(document.querySelector("video"));
      history.pushState({}, "", "/feed");
      document.dispatchEvent(new Event("yt-navigate-finish"));
    });
    await sleep(500);
    assert.equal((await live()).id, beforeMove.id, "Connected miniplayer move preserves session");
    const movedAudio = await freshAudio("processed playback after connected reparenting");
    mark("miniplayer-reparent-and-route-change-preserve-session", { sessionId: beforeMove.id, audio: movedAudio });

    // Hold real bytes from a distant old request across source replacement.
    // The bridge still resolves to the same completed backend artifact, but
    // the old media element resource/session must no longer own responses.
    await pause();
    await setDelayed({ index: 14, entered: false, fetched: false, delivered: false, released: false });
    await seek(14 * boundary + 0.2);
    await until("old real chunk response held", async () => (await control()).delayed?.fetched);
    expectAborts("source replacement");
    await page.locator("video").evaluate((video) => { video.src = "/clip.mp4?replacement=1"; });
    await until("replaced-source session disposed", async () => (await read()).live.length === 0);
    await page.waitForFunction(() => document.querySelector("video").readyState >= 2);
    await on();
    const replacement = await live();
    assert.notEqual(replacement.id, beforeMove.id);
    const replacementAudio = await freshAudio("new source uses a fresh playback session");
    await worker.evaluate(() => { globalThis.__nomusicE2EControl.delayed.released = true; });
    await until("late old response released", async () => (await control()).delayed?.delivered);
    const afterLate = await settledWindow();
    assert.equal(afterLate.id, replacement.id);
    assert.equal(afterLate.indices.includes(14), false, "Delayed old chunk cannot enter the new window");
    const retired = (await read()).sessions.find((session) => session.id === beforeMove.id);
    if (retired) {
      assert.equal(retired.disposed, true);
      assert.equal(retired.decodedChunks, 0);
      assert.equal(retired.activeRequests, 0);
    }
    mark("source-replacement-rejects-delayed-old-response", { oldSession: beforeMove.id,
      newSession: replacement.id, audio: replacementAudio,
      delayedIndex: delayed.index, deliveryError: delayed.deliveryError || null });

    // Removal itself emits no emptied event. Keep only numeric evidence of the
    // old video; the passive observer must not retain that detached element.
    await pause();
    await page.locator(".nomusic-btn__dl").click();
    await page.locator(".nomusic-menu:not([hidden])").waitFor();
    await page.evaluate(() => {
      window.__fixtureEmptied = 0;
      document.querySelector("video").addEventListener("emptied", () => window.__fixtureEmptied++);
    });
    expectAborts("remove video and retire its UI");
    await page.locator("video").evaluate((video) => video.remove());
    await until("removed session owns no work", async () => {
      const state = await read();
      return state.live.length === 0 && state.sessions.every((session) =>
        session.activeRequests === 0 && session.activeSources === 0 &&
        !session.bufferTimer && !session.streamOpen && session.requestAborted);
    });
    await until("all observed audio contexts closed", async () => (await audioState()).contexts.every((state) => state === "closed"));
    assert.equal(await page.locator(".nomusic-btn, .nomusic-menu").count(), 0);
    assert.equal(await page.evaluate(() => window.__fixtureEmptied), 0);
    mark("removed-video-cleanup-without-emptied", await read());

    const observation = await isolated("({peaks:__nomusicPlayback.peaks,samples:__nomusicPlayback.samples})");
    assert.ok(observation.peaks.pcmBytes <= evidence.limits.pcmBytes);
    assert.ok(observation.peaks.decodedChunks <= evidence.limits.retainedChunks);
    assert.ok(observation.peaks.activeRequests <= evidence.limits.activeRequests);
    assert.equal(observation.peaks.liveSessions, 1);
    evidence.observation = observation;
    evidence.maxSourceAheadSeconds = (await audioState()).maxSourceAhead;
    assert.ok(evidence.maxSourceAheadSeconds <= 30.1, "Every source start obeys the 30-second chunk-start horizon");
    syncChunkRequests(await transport());
    evidence.chunkRequests = chunkRequests;
    evidence.passed = true;
  } finally {
    await setFault(null);
    await setDelayed(null);
    await isolated("__nomusicPlayback.restore()");
  }
}
