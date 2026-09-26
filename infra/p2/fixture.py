"""Generate the fixed synthetic canary input; no provider or user data."""

from agent_alfred.evals.acceptance.admission import proposal
from agent_alfred.p2.common import encode


def manifest():
    batch = {
        "schema_version": 1,
        "simulation": True,
        "phase": "offline_fixture",
        "parent": None,
        "batch_id": "p2-synthetic-batch",
        "candidate_id": "a" * 64,
        "cases": [],
        "rubric": {},
        "judge_profile": None,
        "candidate": {"files": {}},
        "profiles": [
            {
                "id": "p2-profile",
                "execution_policy": {"max_retries": 0, "sdk_max_retries": 0},
                "product_models": [
                    {
                        "endpoint_id": "p2-private-canary",
                        "model_id": "p2-canary-product",
                    }
                ],
                "judge_model": {
                    "endpoint_id": "p2-private-canary",
                    "model_id": "p2-canary-judge",
                },
            }
        ],
        "authorization": {
            "max_requests": 3,
            "max_output_tokens": 16,
            "total_seconds": 600,
            "cost": {"amount": 0, "source": "p2-private-canary"},
        },
    }
    return {
        "proposal": proposal(
            batch,
            output_scope="/p2/synthetic/evidence",
            operations=["product"],
            nonce="b" * 32,
        ),
        "batch": batch,
        "descriptor": {
            "profile_id": "p2-profile",
            "endpoint_id": "p2-private-canary",
            "model_id": "p2-canary-product",
            "max_tokens": 8,
            "body": {"input": "synthetic-only"},
        },
    }


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.write_bytes(encode(manifest()))


if __name__ == "__main__":
    main()
