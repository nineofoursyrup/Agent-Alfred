"""Table-driven assembly of the two supported wire codecs."""

from types import SimpleNamespace

from agent_alfred.anthropic_native import AnthropicAdapter
from agent_alfred.endpoints import ModelRoute, list_endpoints, resolve_model
from agent_alfred.model import EndpointUnconfigured, ModelRef, ModelUnsupported
from agent_alfred.openai_compatible import OpenAICompatibleAdapter
from agent_alfred.retry import RetryPolicy, SystemSleeper
from agent_alfred.runtime.transport import VersionedTransportPool
from agent_alfred.stream_fallback import StreamFallback
from agent_alfred.support_overrides import SupportOverrides


class _RouteClient:
    """Keep the table's exact relative path while the SDK owns HTTP and SSE."""

    def __init__(self, sdk, route, response_type, stream_type):
        self._sdk = sdk
        self._route = route
        self._response_type = response_type
        self._stream_type = stream_type
        self.messages = self
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        timeout = kwargs.pop("timeout", None)
        streaming = kwargs.get("stream", False)
        return self._sdk.post(
            self._route.path,
            body=kwargs,
            cast_to=self._response_type,
            options={"timeout": timeout} if timeout is not None else {},
            stream=streaming,
            stream_cls=self._stream_type,
        )


class EndpointClientFactory:
    def __init__(
        self, *, clock, endpoints=None, support_overrides=None, http_client=None
    ):
        self._clock = clock
        self._endpoints = list_endpoints() if endpoints is None else tuple(endpoints)
        self.support_overrides = support_overrides or SupportOverrides()
        self._http_client = http_client
        self._pool = VersionedTransportPool(self._build_transport)

    def _route(self, snapshot):
        endpoint = next(
            (row for row in self._endpoints if row.endpoint_id == snapshot.endpoint_id),
            None,
        )
        if endpoint is None:
            raise EndpointUnconfigured("endpoint_unconfigured")
        support = resolve_model(
            snapshot.endpoint_id,
            snapshot.model_id,
            wire_style_override=snapshot.wire_style,
            endpoints=self._endpoints,
            overrides=self.support_overrides,
        )
        if support.support == "unsupported" or support.wire_style not in (
            "openai",
            "anthropic",
        ):
            raise ModelUnsupported("model_unsupported")
        route = endpoint.models.get(snapshot.model_id)
        if route is None or route.style != snapshot.wire_style:
            route = ModelRoute(
                snapshot.wire_style,
                {
                    "openai": "/chat/completions",
                    "anthropic": "/messages",
                }[snapshot.wire_style],
            )
        return endpoint, route

    def _build_transport(self, snapshot):
        endpoint, route = self._route(snapshot)
        options = dict(
            base_url=endpoint.base_url, api_key=snapshot.api_key, max_retries=0
        )
        if self._http_client is not None:
            options["http_client"] = self._http_client
        if route.style == "openai":
            from openai import OpenAI, Stream
            from openai.types.chat import ChatCompletion, ChatCompletionChunk

            class TerminalStream(Stream[ChatCompletionChunk]):
                # openai 3.3.1 consumes [DONE] internally. Observe the SDK's
                # decoded SSE boundary without duplicating its byte parser.
                completed = False

                def _iter_events(self):
                    for event in super()._iter_events():
                        if event.data == "[DONE]":
                            self.completed = True
                        yield event

            return _RouteClient(
                OpenAI(**options), route, ChatCompletion, TerminalStream
            )
        from anthropic import Anthropic, Stream
        from anthropic.types import Message, RawMessageStreamEvent

        return _RouteClient(
            Anthropic(**options, auth_token=None),
            route,
            Message,
            Stream[RawMessageStreamEvent],
        )

    def create(self, snapshot):
        if not snapshot.api_key or not snapshot.api_key.strip():
            raise EndpointUnconfigured("endpoint_unconfigured")
        _, route = self._route(snapshot)
        transport = self._pool.client_for(snapshot)
        model = ModelRef(snapshot.endpoint_id, snapshot.model_id)
        adapter = {"openai": OpenAICompatibleAdapter, "anthropic": AnthropicAdapter}[
            route.style
        ]
        return RetryPolicy(
            StreamFallback(
                adapter(client=transport, model=model, stream=True),
                nonstream=adapter(client=transport, model=model, stream=False),
                clock=self._clock,
                stream=snapshot.stream,
                stream_fallback=snapshot.stream_fallback,
                per_attempt_timeout_s=snapshot.per_attempt_timeout_s,
            ),
            clock=self._clock,
            sleeper=SystemSleeper(),
        )
