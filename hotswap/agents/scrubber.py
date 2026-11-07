"""The scrubber: proposes removal of clear debris before final verification."""
from __future__ import annotations

import asyncio

from hotswap.agents.base import AgentProcess, run_agent
from hotswap.scrub import find_debris
from hotswap.verify import Sandbox


class Scrubber(AgentProcess):
    role = "scrubber"

    async def handle(self, command: str, args: dict) -> None:
        if command != "scrub":
            return
        self.set("working", note="reading the task diff", progress=True)
        context = await self.call("/api/scrub/context", method="GET")
        box = await asyncio.to_thread(Sandbox, context["read_url"])
        try:
            await asyncio.to_thread(box.checkout, context["head"], None)
            proposals = find_debris(box.path, context["added"], set(context["plan_files"]), set(context["public_names"]))
        finally:
            await asyncio.to_thread(box.remove)
        if proposals:
            listing = "\n".join(f"- `{p['file']}`: {p['reason']}" for p in proposals)
            await self.say(f"Proposed removals, limited to lines this task added:\n{listing}\nFinal verification runs after these.")
        else:
            await self.say("No debris found in this task's diff.")
        result = await self.call("/api/ops/scrub", {"agent": self.role, "proposals": proposals})
        self.set("done", note=f"{len(result.get('applied', []))} removals applied", progress=True)


if __name__ == "__main__":
    run_agent(Scrubber)
