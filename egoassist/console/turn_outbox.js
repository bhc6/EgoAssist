// Retain sent text until the server acknowledges its durable turn ID.
class TurnOutbox {
  constructor(storage, userId, sessionId) {
    this.storage = storage;
    this.key = "egoassist.pending." + encodeURIComponent(userId) + "." + encodeURIComponent(sessionId);
  }

  entries() {
    const items = JSON.parse(this.storage.getItem(this.key) || "[]");
    if (!Array.isArray(items) || items.some(item => item.type !== "control" ||
        typeof item.clientRequestId !== "string" || typeof item.transcript !== "string")) {
      throw Error("Invalid pending text requests");
    }
    return items;
  }

  add(request) {
    const items = this.entries();
    const known = items.find(item => item.clientRequestId === request.clientRequestId);
    if (known) {
      if (known.transcript !== request.transcript) throw Error("Pending request ID conflict");
      return;
    }
    if (items.length >= 16) throw Error("Too many unacknowledged requests; reconnect first");
    this.storage.setItem(this.key, JSON.stringify([...items, request]));
  }

  acknowledge(turnId) {
    const items = this.entries().filter(item => item.clientRequestId !== turnId);
    if (items.length) this.storage.setItem(this.key, JSON.stringify(items));
    else this.storage.removeItem(this.key);
  }

  static clearUser(storage, userId) {
    const prefix = "egoassist.pending." + encodeURIComponent(userId) + ".";
    for (const key of Object.keys(storage)) {
      if (key.startsWith(prefix)) storage.removeItem(key);
    }
  }
}
