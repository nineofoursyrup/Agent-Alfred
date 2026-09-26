"""Explicit, fixed-target SigV4 adapter for private P2 REST APIs."""

import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .common import BoundaryError, check_id, decode, encode

_HOST = re.compile(r"[a-z0-9]+\.execute-api\.[a-z0-9-]+\.amazonaws\.com\Z")
_ACTIONS = {"submit", "invoke", "finish", "status", "proposal", "preflight", "canary"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class PrivateApi:
    """The URL and action set are installed by the platform administrator."""

    def __init__(self, base_url, region, *, session=None, opener=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not _HOST.fullmatch(parsed.hostname or "")
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"/[a-zA-Z0-9_-]+", parsed.path)
            or not isinstance(region, str)
            or region not in parsed.hostname
        ):
            raise BoundaryError("private_api_target_invalid")
        self.base_url = base_url.rstrip("/")
        self.region = region
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )
        if session is None:
            import botocore.session

            session = botocore.session.get_session()
        self._session = session

    def call(self, action, payload):
        if action not in _ACTIONS:
            raise BoundaryError("private_api_action_invalid")
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        raw = encode(payload)
        url = self.base_url + "/v1/" + action
        credentials = self._session.get_credentials()
        if credentials is None:
            raise BoundaryError("runtime_identity_unavailable")
        signed = AWSRequest(
            method="POST",
            url=url,
            data=raw,
            headers={"Content-Type": "application/json"},
        )
        SigV4Auth(
            credentials.get_frozen_credentials(), "execute-api", self.region
        ).add_auth(signed)
        request = urllib.request.Request(
            url, data=raw, headers=dict(signed.headers), method="POST"
        )
        try:
            with self._opener.open(request, timeout=8) as response:
                if response.status != 200:
                    raise BoundaryError("private_api_rejected")
                return decode(response.read(256 * 1024 + 1))
        except urllib.error.HTTPError as error:
            # Distinguish a gateway/IAM 403 from our canary business 403.
            # The latter proves the protected function was reached and is
            # therefore a failed network/IAM isolation probe.
            if error.code == 403:
                try:
                    body = decode(error.read(2048))
                except BoundaryError:
                    body = None
                if isinstance(body, dict) and "error" in body:
                    raise BoundaryError("private_api_application_denied") from None
            # The caller sees a stable code, never provider headers or secrets.
            raise BoundaryError("private_api_rejected:" + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise BoundaryError("private_api_state_unknown") from error


class RemoteAuthorityDispatch:
    """Concrete AuthorityDispatch surface for a controlled Fargate task only.

    Constructing this object never changes the default production
    UnconfiguredAuthority. A task must receive a fixed private API address
    and IAM role from the independent platform administrator.
    """

    def __init__(self, private_api):
        if type(private_api) is not PrivateApi:
            raise BoundaryError("private_api_required")
        self.api = private_api

    def submit_job(self, proposal_ref):
        return self.api.call("submit", proposal_ref)

    def invoke(self, job_id, role, attempt_id, request_descriptor):
        return self.api.call(
            "invoke",
            {
                "job_id": check_id(job_id),
                "role": role,
                "attempt_id": check_id(attempt_id),
                "request_descriptor": request_descriptor,
            },
        )

    def finish(self, job_id, role):
        return self.api.call("finish", {"job_id": check_id(job_id), "role": role})

    def status(self, job_id):
        return self.api.call("status", {"job_id": check_id(job_id)})
