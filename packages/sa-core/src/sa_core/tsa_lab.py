"""Local RFC 3161 TSA (from prototype 5b): a test double and the demo's throwaway time-stamp authority.

Real CMS tokens, real signatures, a controllable clock, its own throwaway CA. Lets the verifier be tested
with tokens from "organisations" that lie about time, use an untrusted CA, or hold expired certificates,
none of which the public TSAs allow. `tsa_farm` serves three of them over HTTP and writes the `--tsa-config`
file the CLI reads. Never a source of real time evidence: the CAs live for one run.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from asn1crypto import cms, core, tsp
from asn1crypto import x509 as a1x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .tsa import PinnedRoot, TrustEntry, TrustStore, der_sha256

_serials = itertools.count(1000)
DEFAULT_START = datetime(2020, 1, 1, tzinfo=UTC)
DEFAULT_END = datetime(2060, 1, 1, tzinfo=UTC)


def _name(cn: str, org: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


class LocalCa:
    """Self-signed root, as a pinned root would be."""

    def __init__(
        self, org: str, not_before: datetime = DEFAULT_START, not_after: datetime = DEFAULT_END
    ) -> None:
        self.org = org
        self.key = ec.generate_private_key(ec.SECP256R1())
        subject = _name(f"{org} Root", org)
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(self.key.public_key())
            .serial_number(next(_serials))
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.key, hashes.SHA256())
        )

    def pin(self, operator: str, policy: str, name: str | None = None) -> PinnedRoot:
        entry = TrustEntry(
            name=name or operator,
            operator=operator,
            root_file="(memory)",
            root_sha256=der_sha256(self.cert),
            policies=(policy,),
            root_source="local test CA",
        )
        return PinnedRoot(entry, self.cert)


class LocalTsa:
    def __init__(
        self,
        ca: LocalCa,
        clock: Callable[[], float],
        policy: str = "1.2.3.4.5",
        skew_s: float = 0.0,
        accuracy_s: int | None = 1,
        not_before: datetime = DEFAULT_START,
        not_after: datetime = DEFAULT_END,
        eku_timestamping: bool = True,
    ) -> None:
        self.ca = ca
        self.clock = clock
        self.policy = policy
        self.skew_s = skew_s  # added to the clock: a negative value is a TSA that backdates
        self.accuracy_s = accuracy_s
        self.key = ec.generate_private_key(ec.SECP256R1())
        b = (
            x509.CertificateBuilder()
            .subject_name(_name(f"{ca.org} TSA", ca.org))
            .issuer_name(ca.cert.subject)
            .public_key(self.key.public_key())
            .serial_number(next(_serials))
            .not_valid_before(not_before)
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        )
        if eku_timestamping:
            b = b.add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True
            )
        self.cert = b.sign(ca.key, hashes.SHA256())

    def issue(
        self, digest: bytes, gen_unix: float | None = None, nonce: int | None = None
    ) -> bytes:
        """A TimeStampToken (CMS ContentInfo DER) for `digest` at the TSA's clock (+skew)."""
        t = (self.clock() if gen_unix is None else gen_unix) + self.skew_s
        gen = datetime.fromtimestamp(int(t), tz=UTC)
        fields: dict[str, object] = {
            "version": "v1",
            "policy": self.policy,
            "message_imprint": {
                "hash_algorithm": {"algorithm": "sha256"},
                "hashed_message": digest,
            },
            "serial_number": next(_serials),
            "gen_time": gen,
        }
        if nonce is not None:
            fields["nonce"] = nonce
        if self.accuracy_s is not None:
            fields["accuracy"] = {"seconds": self.accuracy_s}
        tst_der = tsp.TSTInfo(fields).dump()
        attrs = cms.CMSAttributes(
            [
                cms.CMSAttribute({"type": "content_type", "values": ["tst_info"]}),
                cms.CMSAttribute(
                    {"type": "message_digest", "values": [hashlib.sha256(tst_der).digest()]}
                ),
            ]
        )
        sig = self.key.sign(attrs.dump(), ec.ECDSA(hashes.SHA256()))
        a1cert = a1x509.Certificate.load(self.cert.public_bytes(serialization.Encoding.DER))
        si = cms.SignerInfo(
            {
                "version": "v1",
                "sid": cms.SignerIdentifier(
                    {
                        "issuer_and_serial_number": {
                            "issuer": a1cert.issuer,
                            "serial_number": a1cert.serial_number,
                        }
                    }
                ),
                "digest_algorithm": {"algorithm": "sha256"},
                "signed_attrs": attrs,
                "signature_algorithm": {"algorithm": "sha256_ecdsa"},
                "signature": sig,
            }
        )
        root = a1x509.Certificate.load(self.ca.cert.public_bytes(serialization.Encoding.DER))
        sd = cms.SignedData(
            {
                "version": "v3",
                "digest_algorithms": [{"algorithm": "sha256"}],
                "encap_content_info": {
                    "content_type": "tst_info",
                    "content": core.ParsableOctetString(tst_der),
                },
                "certificates": [
                    cms.CertificateChoices({"certificate": a1cert}),
                    cms.CertificateChoices({"certificate": root}),
                ],
                "signer_infos": [si],
            }
        )
        out: bytes = cms.ContentInfo({"content_type": "signed_data", "content": sd}).dump()
        return out


def store_of(*roots: PinnedRoot) -> TrustStore:
    return TrustStore(list(roots))


def respond(tsa: LocalTsa, request: bytes) -> bytes:
    """A TimeStampResp (status granted) for a DER TimeStampReq, echoing its nonce."""
    req = tsp.TimeStampReq.load(request)
    token = tsa.issue(req["message_imprint"]["hashed_message"].native, nonce=req["nonce"].native)
    resp = tsp.TimeStampResp(
        {
            "status": {"status": "granted"},
            "time_stamp_token": cms.ContentInfo.load(token),
        }
    )
    out: bytes = resp.dump()
    return out


def refusal(status: str = "rejection", text: str = "too many requests") -> bytes:
    """A TimeStampResp without a token (asn1crypto's spec insists on one, so wrap by hand)."""
    info: bytes = tsp.PKIStatusInfo({"status": status, "status_string": [text]}).dump()
    return bytes([0x30, len(info)]) + info


FARM_POLICY = "1.2.3.4.5"
FARM_ORGS = ("alpha", "beta", "gamma")


@contextmanager
def tsa_farm(
    directory: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    farm_gap_s: float = 15.0,
    advertise: str | None = None,
) -> Iterator[Path]:
    """Start three local TSAs (three organisations, throwaway CAs); yield the path of the `--tsa-config` JSON.

    Writes a trust directory next to it, so provider, followers, `sa record` and `sa-api` verify in separate
    processes exactly as with the public TSAs. The servers stop on exit. `farm_gap_s` is the minimum gap
    between batches (loopback TSAs may be hit faster than the 15 s public TSAs ask for: the demo uses 1 s).
    `host` is where to bind; `advertise` is the host name the config tells clients to use (a container name).
    A farm that is not on loopback writes `"lab": true`, which tells the stamper this is a throwaway lab.
    """
    directory.mkdir(parents=True, exist_ok=True)
    roots = directory / "trust" / "roots"
    roots.mkdir(parents=True, exist_ok=True)
    cas = {org: LocalCa(org) for org in FARM_ORGS}
    tsas = {org: LocalTsa(cas[org], time.time, policy=FARM_POLICY) for org in FARM_ORGS}
    entries = []
    for org, ca in cas.items():
        pinned = ca.pin(org, FARM_POLICY)
        (roots / f"{org}.pem").write_bytes(ca.cert.public_bytes(serialization.Encoding.PEM))
        entries.append({**pinned.entry.model_dump(mode="json"), "root_file": f"{org}.pem"})
    (directory / "trust" / "tsas.json").write_text(
        json.dumps({"bundle_version": "e2e-lab", "entries": entries})
    )

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            org = self.path.strip("/")
            if org not in tsas:
                self.send_error(404, f"no TSA {org!r}; known: {sorted(tsas)}")
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            out = respond(tsas[org], body)
            self.send_response(200)
            self.send_header("Content-Type", "application/timestamp-reply")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    bound = server.server_address[1]
    config = directory / "tsa-config.json"
    config.write_text(
        json.dumps(
            {
                "k": 2,
                "min_gap_s": farm_gap_s,
                "lab": (advertise or host) not in {"127.0.0.1", "localhost", "::1"},
                "trust_dir": str(directory / "trust"),
                "endpoints": [
                    {
                        "name": o,
                        "url": f"http://{advertise or host}:{bound}/{o}",
                        "operator": o,
                        "min_gap_s": farm_gap_s,
                    }
                    for o in FARM_ORGS
                ],
            }
        )
    )
    try:
        yield config
    finally:
        server.shutdown()
        server.server_close()
