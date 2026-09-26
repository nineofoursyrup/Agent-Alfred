"""Synthetic mechanism fixtures, never approved calibration or release gold."""

from .schema import CONTRACT, GROUPS, digest

SKIP = '{"retrieve":false,"query":null,"reason_code":"greeting"}'


def offline_batch(candidate=None):
    candidate = candidate or {
        "commit": "unknown",
        "tree": "unknown",
        "files": {},
        "dependencies": {},
        "environment": "offline_fixture",
    }
    model = {
        "endpoint_id": "opencode-go",
        "model_id": "deepseek-v4-flash",
        "wire_style": "openai",
        "provider_version": "unknown",
    }
    profile = {
        "product_models": [model],
        "judge_model": {**model, "model_id": "fixture-judge"},
        "parameters": {"max_tokens": 256, "max_steps": 8},
        "inputs": {"persona": "Synthetic acceptance fixture."},
    }
    profile["id"] = digest(profile)
    cases = []
    for group in GROUPS:
        case = {
            "id": group,
            "group": group,
            "input": f"{group} fixture task",
            "gold": f"{group} fixture answer",
            "forbidden": ["invented_execution"],
            "source": {"kind": "synthetic", "reference": "phase-a-example-v1"},
            "applicability": {
                "completion": True,
                "correctness": True,
                "selection": group in ("skills", "routing"),
            },
            "operation": "chat",
            "setup": {},
            "script": [SKIP, f"{group} fixture answer"],
        }
        if group in ("memory", "aggregation"):
            case["setup"] = {"fact": "fixture coffee preference"}
        if group == "memory":
            case["script"][0] = (
                '{"retrieve":true,"query":"coffee","reason_code":"personal_information"}'
            )
        if group == "tools":
            case["script"].insert(
                1, {"tool": "draft_message", "arguments": {"body": "Synthetic draft"}}
            )
        if group == "skills":
            case["input"] = "/skills Fixture\nFollow the fixture procedure"
            case["setup"] = {"skill": "Reply with the fixture answer."}
        if group == "routing":
            case["setup"] = {"routing": True}
            case["script"] = [SKIP, "full", "routing fixture answer"]
        if group == "aggregation":
            case["operation"] = "aggregate"
            case["script"] = ["aggregation fixture answer [[S1]]"]
        case["material_id"] = digest({"input": case["input"], "gold": case["gold"]})
        cases.append(case)
    return {
        "schema_version": 1,
        "contract": CONTRACT,
        "batch_id": "offline-example",
        "phase": "offline_fixture",
        "simulation": True,
        "parent": None,
        "candidate": candidate,
        "candidate_id": digest(candidate),
        "profiles": [profile],
        "cases": cases,
        "rubric": None,
        "calibration": None,
        "coverage": [],
        "gates": [],
        "results": [],
        "grades": [],
        "adjudications": [],
        "authorization": None,
        "references": {},
    }
