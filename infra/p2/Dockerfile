ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /opt/p2
COPY requirements.txt /opt/p2/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes -r /opt/p2/requirements.txt -t /opt/p2/python
COPY agent_alfred /opt/p2/python/agent_alfred
COPY input.json /opt/p2/input.json
ENV PYTHONPATH=/opt/p2/python PYTHONDONTWRITEBYTECODE=1
USER 10001
CMD ["python", "-m", "agent_alfred.p2.worker"]
