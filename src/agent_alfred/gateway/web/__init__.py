"""The Web entry: Dashboard HTTP + SSE.

The Dashboard is one of the two v1 Gateways (the other is the CLI), so it
lives under ``gateway/``. Nothing here renders a page -- the nine pages are a
later work ticket. This package is the transport skeleton that makes events
reach a browser and lets a reconnecting browser recover without duplicates
and without silent holes.
"""
