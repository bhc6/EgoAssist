/* Device playback evidence, separate from provider generation progress. */
class PlaybackAudit {
  constructor(send) { this.send = send; this.responses = new Map(); }
  queued(id, source, start, rate) {
    if (!id) return;
    const row = this.responses.get(id) || {played: 0, queued: 0, chunks: []};
    row.chunks.push({source, start, offset: row.queued, samples: source.buffer.length, rate});
    row.queued += source.buffer.length;
    this.responses.set(id, row);
    while (this.responses.size > 8) this.responses.delete(this.responses.keys().next().value);
  }
  report(now, status = "playing", onlyId = null) {
    for (const [responseId, row] of this.responses) {
      if (onlyId && responseId !== onlyId) continue;
      if (!onlyId && !row.chunks.length && row.sent === row.played) continue;
      for (const chunk of row.chunks) {
        const samples = Math.max(0, Math.min(chunk.samples, Math.floor((now - chunk.start) * chunk.rate)));
        if (samples > 0) row.played = Math.max(row.played, chunk.offset + samples);
      }
      row.chunks = row.chunks.filter(chunk => now < chunk.start + chunk.samples / chunk.rate);
      if (row.sent !== row.played || status !== "playing") {
        this.send({type: "playback_progress", responseId, playedSamples: row.played, status});
        row.sent = row.played;
      }
      if (status === "paused") { row.chunks = []; row.queued = row.played; }
    }
  }
  resume(id, samples) { this.responses.set(id, {played: samples, queued: samples, chunks: []}); }
  clear() { this.responses.clear(); }
}
