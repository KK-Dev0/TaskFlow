/* TaskFlow focus engine: Pomodoro, stopwatch, countdown.
   State lives in localStorage with absolute timestamps, so a session survives
   page reloads, tab sleeping and navigation (session recovery). */
(function () {
  'use strict';
  const KEY = 'tf_timer_v1';
  let cfg = { pomo_work: 25, pomo_short: 5, pomo_long: 15, pomo_cycles: 4 };
  let hooks = { onTick() {}, onPhaseEnd() {}, log: async () => {} };
  let st = null, loop = null;

  const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {} };
  const load = () => { try { return JSON.parse(localStorage.getItem(KEY)); } catch (e) { return null; } };

  function fresh(mode, extra) {
    const s = Object.assign({
      mode: mode || 'pomodoro', phase: 'work', running: false, startedAt: 0, accMs: 0, targetMs: 0,
      taskId: null, cycle: 0, segId: uid(), sessionStart: null,
    }, extra || {});
    s.targetMs = targetFor(s);
    return s;
  }
  function targetFor(s) {
    if (s.mode === 'stopwatch') return 0;
    if (s.mode === 'countdown') return (s.countdownMin || 10) * 60000;
    if (s.phase === 'short') return cfg.pomo_short * 60000;
    if (s.phase === 'long') return cfg.pomo_long * 60000;
    return cfg.pomo_work * 60000;
  }
  const elapsed = () => st.accMs + (st.running ? Date.now() - st.startedAt : 0);

  function snapshot() {
    if (!st) return null;
    const e = elapsed();
    return {
      mode: st.mode, phase: st.phase, running: st.running, elapsedMs: e, targetMs: st.targetMs,
      remainingMs: st.targetMs ? Math.max(0, st.targetMs - e) : 0,
      progress: st.targetMs ? Math.min(1, e / st.targetMs) : 0,
      taskId: st.taskId, cycle: st.cycle, cycles: cfg.pomo_cycles,
      active: st.running || st.accMs > 0,
    };
  }

  async function logWork(ms) {
    if (ms < 10000 || st.phase !== 'work') return;
    try {
      await hooks.log({
        task_id: st.taskId || null, duration_sec: Math.round(ms / 1000), kind: st.mode,
        started_at: new Date(Date.now() - ms).toISOString(), client_id: st.segId,
      });
    } catch (e) { /* retried implicitly: same client_id keeps it idempotent */ }
  }

  function startLoop() { if (!loop) loop = setInterval(tick, 250); }
  function stopLoop() { if (loop) { clearInterval(loop); loop = null; } }

  function tick() {
    if (!st) return;
    if (st.running && st.targetMs && elapsed() >= st.targetMs) { complete(); return; }
    hooks.onTick(snapshot());
  }

  async function complete() {
    const finishedPhase = st.phase, mode = st.mode, taskId = st.taskId;
    const ms = st.targetMs;
    await logWork(ms);
    st.running = false; st.accMs = 0; st.segId = uid();
    let next = null;
    if (mode === 'pomodoro') {
      if (finishedPhase === 'work') {
        st.cycle += 1;
        const long = st.cycle % cfg.pomo_cycles === 0;
        st.phase = long ? 'long' : 'short';
        next = st.phase;
      } else {
        st.phase = 'work';
        next = 'work';
      }
      st.targetMs = targetFor(st);
      if (finishedPhase === 'work') { st.running = true; st.startedAt = Date.now(); } // breaks auto-start
    } else {
      st = fresh(mode, { taskId, countdownMin: st.countdownMin });
    }
    save();
    hooks.onPhaseEnd({ finished: finishedPhase, mode, next, taskId, minutes: Math.round(ms / 60000) });
    hooks.onTick(snapshot());
  }

  const api = {
    init(settings, h) {
      cfg = Object.assign(cfg, settings || {});
      hooks = Object.assign(hooks, h || {});
      st = load();
      if (st && st.running && st.targetMs && elapsed() >= st.targetMs) {
        // finished while the page was closed
        st.accMs = st.targetMs; st.running = false;
        complete();
      }
      if (!st) st = fresh('pomodoro');
      startLoop();
      hooks.onTick(snapshot());
    },
    updateSettings(s) {
      cfg = Object.assign(cfg, s);
      if (st && !st.running && st.accMs === 0) { st.targetMs = targetFor(st); save(); }
    },
    snapshot,
    setMode(mode, opts) {
      if (st && snapshot().active) return false; // finish or reset first
      st = fresh(mode, Object.assign({ taskId: st ? st.taskId : null, cycle: st ? st.cycle : 0 }, opts || {}));
      save(); hooks.onTick(snapshot()); return true;
    },
    setCountdown(min) {
      if (st.mode !== 'countdown' || snapshot().active) return;
      st.countdownMin = Math.max(1, Math.min(600, Math.round(min)));
      st.targetMs = targetFor(st); save(); hooks.onTick(snapshot());
    },
    setTask(id) { st.taskId = id || null; save(); },
    start() {
      if (st.running) return;
      st.running = true; st.startedAt = Date.now();
      if (!st.sessionStart) st.sessionStart = new Date().toISOString();
      save(); startLoop(); hooks.onTick(snapshot());
    },
    pause() {
      if (!st.running) return;
      st.accMs = elapsed(); st.running = false; save(); hooks.onTick(snapshot());
    },
    async finish() { // stop early and keep the time worked
      const ms = elapsed();
      st.running = false;
      await logWork(ms);
      const keep = { taskId: st.taskId, countdownMin: st.countdownMin, cycle: st.cycle };
      st = fresh(st.mode, keep); save(); hooks.onTick(snapshot());
      return ms;
    },
    reset() { // discard without logging
      const keep = { taskId: st.taskId, countdownMin: st.countdownMin };
      st = fresh(st.mode, keep); save(); hooks.onTick(snapshot());
    },
    skipPhase() {
      if (st.mode !== 'pomodoro') return;
      st.running = false; st.accMs = 0; st.segId = uid();
      if (st.phase === 'work') { st.cycle += 1; st.phase = st.cycle % cfg.pomo_cycles === 0 ? 'long' : 'short'; }
      else st.phase = 'work';
      st.targetMs = targetFor(st); save(); hooks.onTick(snapshot());
    },
  };
  window.TFTimer = api;
})();
