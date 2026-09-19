"""Disposable statistics reader; termination also releases SQLite lock waits."""

import json
import sys
from datetime import datetime

from agent_alfred.routing_statistics.service import StatisticsError, read_statistics


def main():
    request = json.load(sys.stdin)
    request["as_of"] = datetime.fromisoformat(request["as_of"])
    try:
        result = read_statistics(**request)
        print(json.dumps({"result": result}), flush=True)
    except StatisticsError as exc:
        print(json.dumps({"code": exc.code, "status": exc.status}), flush=True)


if __name__ == "__main__":
    main()
