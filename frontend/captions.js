(() => {
  "use strict";

  function normalizeAlignment(alignment) {
    if (!alignment || !Array.isArray(alignment.chars)) return null;
    const starts = alignment.char_start_times_ms;
    const durations = alignment.char_durations_ms;
    if (!Array.isArray(starts) || !Array.isArray(durations)) return null;
    const length = Math.min(alignment.chars.length, starts.length, durations.length);
    if (!length) return null;
    const chars = [];
    const validStarts = [];
    const validDurations = [];
    for (let index = 0; index < length; index += 1) {
      const start = Number(starts[index]);
      const duration = Number(durations[index]);
      const char = String(alignment.chars[index] || "");
      if (!char || !Number.isFinite(start) || !Number.isFinite(duration)) continue;
      if (start < 0 || duration < 0) continue;
      chars.push(char);
      validStarts.push(start);
      validDurations.push(duration);
    }
    return chars.length
      ? { chars, starts: validStarts, durations: validDurations }
      : null;
  }

  function createTimeline(currentTime, onProgress) {
    const cues = [];
    let nextCue = 0;
    let text = "";
    let animationFrame = 0;

    function add(alignment, baseTime, autoStart = true) {
      const normalized = normalizeAlignment(alignment);
      if (!normalized) return 0;
      let duration = 0;
      normalized.chars.forEach((char, index) => {
        const start = normalized.starts[index] / 1000;
        const charDuration = normalized.durations[index] / 1000;
        cues.push({ char, at: baseTime + start });
        duration = Math.max(duration, start + charDuration);
      });
      if (autoStart) start();
      return duration;
    }

    function start() {
      if (animationFrame || nextCue >= cues.length) return;
      animationFrame = window.requestAnimationFrame(tick);
    }

    function tick() {
      animationFrame = 0;
      const now = currentTime();
      let changed = false;
      while (nextCue < cues.length && cues[nextCue].at <= now + 0.012) {
        text += cues[nextCue].char;
        nextCue += 1;
        changed = true;
      }
      if (changed) onProgress(text);
      if (nextCue < cues.length) start();
    }

    function stop() {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    }

    return { add, start, stop };
  }

  window.OS1Captions = Object.freeze({ createTimeline, normalizeAlignment });
})();
