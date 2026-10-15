// Plays the alert sound (service workers can't play audio).
chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type !== 'beep') return;
  const ctx = new AudioContext();
  for (const offset of [0, 0.25, 0.5]) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = 880;
    osc.connect(gain).connect(ctx.destination);
    const t = ctx.currentTime + offset;
    gain.gain.setValueAtTime(0.3, t);
    gain.gain.exponentialRampToValueAtTime(0.001, t + 0.2);
    osc.start(t);
    osc.stop(t + 0.2);
  }
  setTimeout(() => ctx.close(), 1500);
});
