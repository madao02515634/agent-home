"""native: the Router's own harness subagent; superagent only writes its prompt and records it."""

from __future__ import annotations

from .base import Adaptor, LaunchPlan, LaunchRequest


class NativeAdaptor(Adaptor):
    name = "native"
    can_push = False
    can_probe = False  # the harness tells the Router when its subagent ends
    needs_pinned_model = False  # the subagent runs on the harness's own model

    def launch(self, req: LaunchRequest) -> LaunchPlan:
        return LaunchPlan(native_spawn={
            "description": f"{req.role} {req.stage}", "prompt": req.instructions, "cwd": str(req.workdir),
            "note": "Spawn this with your own harness subagent tool; the subagent must run the post command before it returns."})
