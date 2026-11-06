import hashlib, json, time

class EvidenceRecord:
    """Hash-chained evidence record. Corruption cannot hide."""
    def __init__(self, payload, prev_hash="genesis", ts=None):
        if not isinstance(payload, dict):
            raise ValueError("payload must be dict")
        self.payload = payload
        self.prev_hash = prev_hash
        self.ts = time.time() if ts is None else float(ts)
        self.hash = self._hash()

    def _hash(self):
        body = json.dumps({"payload": self.payload, "prev": self.prev_hash,
                           "ts": self.ts}, sort_keys=True, default=str)
        return hashlib.sha256(body.encode()).hexdigest()

    def verify(self, prev):
        if self.prev_hash != prev.hash:
            raise ValueError("chain break: prev_hash mismatch")
        if self.hash != self._hash():
            raise ValueError("hash mismatch: payload tampered")
        return True

    def to_dict(self):
        return {"payload": self.payload, "prev_hash": self.prev_hash,
                "ts": self.ts, "hash": self.hash}
