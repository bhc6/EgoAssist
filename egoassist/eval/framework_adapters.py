"""Optional framework discovery; unavailable integrations fail closed."""
from __future__ import annotations
import importlib.util
from dataclasses import dataclass

@dataclass(frozen=True)
class FrameworkCapability:
    name: str; package: str; available: bool; reason: str

def discover_frameworks(names=("mem0", "graphiti_core", "letta")):
    result=[]
    for name in names:
        available=importlib.util.find_spec(name) is not None
        result.append(FrameworkCapability(name, name, available, "installed" if available else "optional dependency not installed"))
    return result

class UnavailableAdapter:
    def __init__(self, capability): self.capability=capability; self.name=capability.name
    def ingest(self, records): raise RuntimeError(f"{self.name} adapter unavailable: {self.capability.reason}")
    def retrieve(self, sample): raise RuntimeError(f"{self.name} adapter unavailable: {self.capability.reason}")
