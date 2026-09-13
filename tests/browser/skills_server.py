"""Real Skill Dashboard; only the model boundary is deterministic."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from agent_alfred.events import AttemptCommitted, AttemptStarted
from agent_alfred.model import ScriptedModel, ScriptedModelFactory
from agent_alfred.runtime.skill_selector import SELECTOR_SYSTEM
from agent_alfred.wiring import build_dashboard


class SkillModel:
    def respond(self, request, *, events=None, deadline=None):
        system = "\n".join(b.text for b in request.system or ())
        if system.startswith(SELECTOR_SYSTEM):
            text = '{"skills":["A","B","C"]}'
        elif "Decide whether long-term memory" in system:
            text = '{"retrieve":false,"query":null,"reason_code":"greeting"}'
        else:
            text = "离线 Skill 回复"
        result = ScriptedModel([text]).respond(request, deadline=deadline)
        attempt = result.attempts[0]
        if events is not None:
            events.emit(
                AttemptStarted(attempt_id=attempt.attempt_id, model=request.model)
            )
            events.emit(
                AttemptCommitted(
                    attempt_id=attempt.attempt_id,
                    blocks=result.response.blocks,
                    usage=attempt.usage,
                )
            )
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--threshold", type=int)
    args = parser.parse_args()
    dashboard = build_dashboard(
        state_dir=args.state,
        port=args.port,
        skill_builtin=args.state / "builtin",
        factory=ScriptedModelFactory(SkillModel()),
    )
    try:
        dashboard.start()
        print("ready", flush=True)
        for line in sys.stdin:
            if line.strip() == "stop":
                break
            print("ok " + line.strip(), flush=True)
    finally:
        assert dashboard.close()


if __name__ == "__main__":
    main()
