"""RFC 3161 time-stamp authorities as a signal anchor (`TsaAnchor`, kind "tsa").

Ported from prototype 5b (RESULTS.md there has the measurements). Free public TSAs stamp a
Merkle root of event ids; anyone verifies offline with the token, the digest and a pinned
trust bundle. Used for signals (seconds of slack); votes and allocations use OTS.

Offline verification (`verify_token`), in order:
 1. the CMS SignedData parses and carries a TSTInfo (RFC 3161 section 2.4.2);
 2. the TSTInfo message imprint equals the expected SHA-256 digest;
 3. the CMS messageDigest attribute equals the hash of the TSTInfo and the signature over the
    signed attributes verifies with the signer certificate's key;
 4. the signer certificate carries the timeStamping extended key usage (marked critical per
    RFC 3161 section 2.3, or noted), and the TSTInfo TSA name is the signer's subject;
 5. the chain signer -> intermediates -> pinned root verifies signature by signature with every
    certificate valid AT genTime, not today (old tokens keep verifying after certificates expire);
 6. the TSTInfo policy OID is one the pinned entry accepts.

k-of-n: tokens from at least k DISTINCT OPERATORS (organisations, not URLs: DigiCert owns
QuoVadis, Sectigo now serves Entrust) must verify. Per operator only the earliest bound counts.
The anchored time is the k-th smallest of those bounds (genTime + accuracy, rounded up). If at
most k-1 operators lie, one of the k earliest bounds is honest, so the k-th is a true upper bound.

Not done: revocation checking, name constraints, certificate policies, path-length constraints
(prototype 5b has a CRL/OCSP snapshot tool; it is not part of this port). Keep the tokens: a
revoked TSA certificate is a reason to re-stamp with OTS, not something the verifier notices.

The stamper (`TsaStamper`) never contacts one TSA more often than every `min_gap_s` (default 15 s,
what Sectigo asks of scripted clients) and stops using a TSA for good on HTTP 429 or a TSA
refusal, until a person calls `resume`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
from asn1crypto import cms, parser, tsp
from asn1crypto import x509 as a1x509
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtensionOID
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .anchors import (
    AnchorError,
    MerkleTree,
    PathStep,
    TimeBound,
    _check_digest,
    root_from_path,
)
from .models import AnchorProof

# RFC 3161 section 2.4.2: id-kp-timeStamping
OID_KP_TIMESTAMPING = "1.3.6.1.5.5.7.3.8"
DEFAULT_TRUST_DIR = Path(__file__).resolve().parent / "tsa_trust"
DEFAULT_MIN_GAP_S = 15.0
MAX_CHAIN = 6
PROOF_VERSION = 1


class TsaError(AnchorError):
    """A token or response does not prove what it claims. The message says what and what to do."""


# ---------------------------------------------------------------- models


class TrustEntry(BaseModel):
    """One pinned timestamp authority: who runs it, which root vouches for it, which policies we
    accept from it. `operator` is the organisation the k-of-n rule counts, not the URL."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    operator: str
    root_file: str  # file name inside roots/ (PEM)
    root_sha256: str  # SHA-256 of the DER of the pinned root (hex), re-checked at load
    policies: tuple[str, ...]  # accepted TSTInfo policy OIDs; empty means none accepted
    root_source: str  # where the root came from, for the audit trail
    allow_sha1_signature: bool = False  # accept a SHA-1 CMS signature (Apple only); reviewed


class TokenResult(BaseModel):
    """A token that passed every check. Times are UTC."""

    model_config = ConfigDict(frozen=True)

    tsa: str
    operator: str
    gen_time: datetime
    accuracy_s: float | None  # None when the token does not state one
    policy: str
    serial: str  # hex
    imprint_hex: str
    signer_subject: str
    signer_not_after: datetime
    signer_eku_critical: bool
    chain_len: int
    token_bytes: int
    notes: tuple[str, ...] = ()

    def bound_s(self, assumed_accuracy_s: float) -> float:
        """Latest moment the stamped data can have come into existence, per this token alone."""
        acc = self.accuracy_s if self.accuracy_s is not None else assumed_accuracy_s
        return self.gen_time.timestamp() + acc


class KofN(BaseModel):
    """Outcome of the k-of-n rule over several tokens for one imprint."""

    model_config = ConfigDict(frozen=True)

    k: int = Field(ge=1)
    accepted: bool
    existed_before: int | None  # unix seconds: the k-th smallest per-operator bound, rounded up
    operators: tuple[str, ...]  # distinct operators with a valid token, earliest bound first
    results: tuple[TokenResult, ...]
    rejected: tuple[str, ...]  # one line per token that failed or was not counted, with the reason


# ---------------------------------------------------------------- trust bundle


def der_sha256(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


@dataclass(frozen=True)
class PinnedRoot:
    entry: TrustEntry
    cert: x509.Certificate


class TrustStore:
    """The pinned roots. `version` names the bundle (date of its download); nothing is fetched at
    verify time. A new bundle can verify old proofs: chains are checked at genTime."""

    def __init__(self, roots: Sequence[PinnedRoot], version: str = "unversioned") -> None:
        self.roots = list(roots)
        self.version = version

    @classmethod
    def load(cls, directory: Path = DEFAULT_TRUST_DIR) -> TrustStore:
        spec = directory / "tsas.json"
        if not spec.exists():
            raise TsaError(f"trust file {spec} not found; restore it from the sa-core package")
        try:
            doc = json.loads(spec.read_text())
            version = str(doc["bundle_version"])
            raw_entries = list(doc["entries"])
        except (ValueError, KeyError, TypeError) as exc:
            raise TsaError(
                f"trust file {spec} is not a versioned bundle "
                f'{{"bundle_version", "entries"}}: {exc}'
            ) from exc
        out: list[PinnedRoot] = []
        for raw in raw_entries:
            entry = TrustEntry.model_validate(raw)
            pem = directory / "roots" / entry.root_file
            if not pem.exists():
                raise TsaError(f"root file {pem} for TSA {entry.name!r} is missing; restore it")
            cert = x509.load_pem_x509_certificate(pem.read_bytes())
            got = der_sha256(cert)
            if got != entry.root_sha256:
                raise TsaError(
                    f"root {pem.name} for TSA {entry.name!r} has SHA-256 {got}, pinned "
                    f"{entry.root_sha256}; the file was altered or replaced, do not use this bundle"
                )
            out.append(PinnedRoot(entry, cert))
        return cls(out, version)

    def operators(self) -> set[str]:
        return {r.entry.operator for r in self.roots}


# ---------------------------------------------------------------- offline verifier

_HASHES: dict[str, type[hashes.HashAlgorithm]] = {
    "sha1": hashes.SHA1,
    "sha224": hashes.SHA224,
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
}


def _hash(name: str, allow_sha1: bool = False) -> hashes.HashAlgorithm:
    if name not in _HASHES or (name == "sha1" and not allow_sha1):
        raise TsaError(
            f"hash algorithm {name!r} is not accepted for the token's signature; "
            "use a SHA-2 token (or set allow_sha1_signature on a pinned entry after review)"
        )
    return _HASHES[name]()


def _accuracy_s(tst: tsp.TSTInfo) -> float | None:
    if not tst["accuracy"].contents:
        return None
    a = tst["accuracy"].native
    if a["seconds"] is None and a["millis"] is None and a["micros"] is None:
        return None
    return float(a["seconds"] or 0) + (a["millis"] or 0) / 1e3 + (a["micros"] or 0) / 1e6


def _verify_signature(
    signer: x509.Certificate, si: cms.SignerInfo, message: bytes, sig_hash: hashes.HashAlgorithm
) -> None:
    sig = si["signature"].native
    algo = si["signature_algorithm"]
    name = algo.signature_algo
    pub = signer.public_key()
    try:
        if isinstance(pub, rsa.RSAPublicKey):
            if name == "rsassa_pss":
                params = algo["parameters"]
                h = _hash(params["hash_algorithm"]["algorithm"].native)
                salt = params["salt_length"].native
                pub.verify(sig, message, padding.PSS(padding.MGF1(h), salt), h)
            elif name == "rsassa_pkcs1v15":
                pub.verify(sig, message, padding.PKCS1v15(), sig_hash)
            else:
                raise TsaError(f"RSA signer with unsupported signature algorithm {name!r}")
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            if name != "ecdsa":
                raise TsaError(f"EC signer with unsupported signature algorithm {name!r}")
            pub.verify(sig, message, ec.ECDSA(sig_hash))
        else:
            raise TsaError(
                f"signer key type {type(pub).__name__} is not supported (RSA and EC are)"
            )
    except InvalidSignature as exc:
        raise TsaError(
            "token signature does not verify with the signer certificate's key; "
            "the token was altered or the certificate does not belong to it"
        ) from exc


def _sig_hash(si: cms.SignerInfo, allow_sha1: bool) -> hashes.HashAlgorithm:
    try:
        return _hash(si["signature_algorithm"].hash_algo, allow_sha1)
    except ValueError:
        return _hash(si["digest_algorithm"]["algorithm"].native, allow_sha1)


def _name_set(name: a1x509.Name) -> frozenset[tuple[str, str]]:
    """A distinguished name as an unordered set of (attribute OID, text): TSAs encode the same name
    with different string types and in different order in the TSTInfo and in the certificate."""
    out: set[tuple[str, str]] = set()
    for rdn in name.chosen:
        for atv in rdn:
            out.add((atv["type"].dotted, str(atv["value"].native).strip()))
    return frozenset(out)


def _signer_cert(si: cms.SignerInfo, certs: list[x509.Certificate]) -> x509.Certificate:
    sid = si["sid"]
    for c in certs:
        if sid.name == "issuer_and_serial_number":
            want = sid.chosen
            if c.serial_number == want["serial_number"].native and (
                c.issuer.public_bytes() == want["issuer"].dump()
            ):
                return c
        else:
            ski = sid.chosen.native
            try:
                ext = c.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_KEY_IDENTIFIER)
            except x509.ExtensionNotFound:
                continue
            if ext.value.digest == ski:  # type: ignore[attr-defined]
                return c
    raise TsaError("signer certificate named in the token is not among the token's certificates")


def _valid_at(c: x509.Certificate, t: datetime) -> bool:
    return c.not_valid_before_utc <= t <= c.not_valid_after_utc


def _is_ca(c: x509.Certificate) -> bool:
    try:
        bc = c.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS).value
    except x509.ExtensionNotFound:
        return False
    return bool(bc.ca)  # type: ignore[attr-defined]


def _issued_by(child: x509.Certificate, parent: x509.Certificate) -> bool:
    try:
        child.verify_directly_issued_by(parent)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def _build_chain(
    signer: x509.Certificate,
    pool: list[x509.Certificate],
    roots: list[PinnedRoot],
    at: datetime,
) -> tuple[PinnedRoot, list[x509.Certificate]]:
    """Walk signer -> pinned root. Returns the pinned root and the certificates walked (signer
    first). Raises TsaError naming the first problem."""
    chain = [signer]
    cur = signer
    for _ in range(MAX_CHAIN):
        if not _valid_at(cur, at):
            raise TsaError(
                f"certificate {cur.subject.rfc4514_string()!r} was not valid at the token's "
                f"genTime {at.isoformat()} (valid {cur.not_valid_before_utc.date()} to "
                f"{cur.not_valid_after_utc.date()})"
            )
        fp = der_sha256(cur)
        for r in roots:
            if fp == r.entry.root_sha256 or _issued_by(cur, r.cert):
                if not _valid_at(r.cert, at):
                    raise TsaError(
                        f"pinned root {r.entry.name!r} was not valid at genTime {at.isoformat()}"
                    )
                return r, chain
        nxt = next(
            (
                p
                for p in pool
                if p is not cur and _is_ca(p) and p not in chain and _issued_by(cur, p)
            ),
            None,
        )
        if nxt is None:
            raise TsaError(
                f"no path from {signer.subject.rfc4514_string()!r} to a pinned root: nothing "
                f"issued {cur.subject.rfc4514_string()!r} among the token's certificates or the "
                "pinned roots. The TSA is untrusted for this bundle."
            )
        chain.append(nxt)
        cur = nxt
    raise TsaError(f"certificate chain longer than {MAX_CHAIN}; refusing")


def verify_token(
    token_der: bytes,
    digest: bytes,
    trust: TrustStore,
    extra_certs: Iterable[bytes] = (),
) -> TokenResult:
    """Verify one RFC 3161 token (the CMS ContentInfo, as in TimeStampResp) for `digest`.

    `extra_certs` are DER intermediates to add when the token did not embed its chain."""
    try:
        ci = cms.ContentInfo.load(token_der)
        if ci["content_type"].native != "signed_data":
            raise TsaError(f"token content type is {ci['content_type'].native}, not signed_data")
        sd = ci["content"]
        eci = sd["encap_content_info"]
        if eci["content_type"].native != "tst_info":
            raise TsaError(f"encapsulated type {eci['content_type'].native} is not tst_info")
        tst = tsp.TSTInfo.load(eci["content"].contents)
        sis = sd["signer_infos"]
        if len(sis) != 1:
            raise TsaError(f"token has {len(sis)} signers; exactly 1 expected")
        si = sis[0]
        certs = [x509.load_der_x509_certificate(c.chosen.dump()) for c in sd["certificates"]]
        certs += [x509.load_der_x509_certificate(d) for d in extra_certs]
    except TsaError:
        raise
    except Exception as exc:
        raise TsaError(
            f"token does not parse as RFC 3161 CMS ({type(exc).__name__}: {exc})"
        ) from exc

    mi = tst["message_imprint"]
    if mi["hash_algorithm"]["algorithm"].native != "sha256":
        raise TsaError(
            f"token imprint algorithm is {mi['hash_algorithm']['algorithm'].native}, not sha256"
        )
    if mi["hashed_message"].native != digest:
        raise TsaError(
            f"token covers imprint {mi['hashed_message'].native.hex()}, expected {digest.hex()}: "
            "the token is for different data"
        )

    attrs = si["signed_attrs"]
    if not attrs.contents:
        raise TsaError("token has no signed attributes; cannot bind the signature to the TSTInfo")
    digest_name = si["digest_algorithm"]["algorithm"].native
    signer = _signer_cert(si, certs)
    gen = tst["gen_time"].native.astimezone(UTC)
    root, chain = _build_chain(signer, certs, trust.roots, gen)  # names the pinned entry
    allow_sha1 = root.entry.allow_sha1_signature
    h = hashes.Hash(_hash(digest_name, allow_sha1))
    h.update(eci["content"].contents)
    content_hash = h.finalize()
    md = None
    for a in attrs:
        if a["type"].native == "message_digest":
            md = a["values"][0].native
    if md != content_hash:
        raise TsaError("CMS messageDigest attribute does not match the TSTInfo; token was altered")
    _verify_signature(signer, si, attrs.untag().dump(), _sig_hash(si, allow_sha1))

    try:
        eku = signer.extensions.get_extension_for_oid(ExtensionOID.EXTENDED_KEY_USAGE)
    except x509.ExtensionNotFound as exc:
        raise TsaError(
            "signer certificate has no extended key usage; not a timestamping cert"
        ) from exc
    if OID_KP_TIMESTAMPING not in [o.dotted_string for o in eku.value]:  # type: ignore[attr-defined]
        raise TsaError("signer certificate lacks the timeStamping extended key usage")
    notes: list[str] = []
    if not eku.critical:
        notes.append("signer EKU is not marked critical (RFC 3161 section 2.3 requires it)")
    if allow_sha1 and digest_name == "sha1":
        notes.append(
            "CMS signature uses SHA-1 (accepted for this pinned entry; imprint is SHA-256)"
        )
    if tst["tsa"].contents:
        gn = tst["tsa"]
        if gn.name == "directory_name":
            if _name_set(gn.chosen) != _name_set(a1x509.Name.load(signer.subject.public_bytes())):
                raise TsaError("TSTInfo names a TSA that is not the signer certificate's subject")
        else:
            notes.append(f"TSTInfo tsa name is a {gn.name}; not compared to the certificate")

    policy = tst["policy"].dotted
    if policy not in root.entry.policies:
        raise TsaError(
            f"policy OID {policy} is not accepted for {root.entry.name!r} "
            f"(accepted: {', '.join(root.entry.policies) or 'none'}); add it to the trust bundle "
            "only after reading that TSA's policy"
        )
    return TokenResult(
        tsa=root.entry.name,
        operator=root.entry.operator,
        gen_time=gen,
        accuracy_s=_accuracy_s(tst),
        policy=policy,
        serial=format(tst["serial_number"].native, "x"),
        imprint_hex=digest.hex(),
        signer_subject=signer.subject.rfc4514_string(),
        signer_not_after=signer.not_valid_after_utc,
        signer_eku_critical=eku.critical,
        chain_len=len(chain) + 1,
        token_bytes=len(token_der),
        notes=tuple(notes),
    )


def k_of_n(
    tokens: Sequence[bytes],
    digest: bytes,
    trust: TrustStore,
    k: int = 2,
    assumed_accuracy_s: float = 1.0,
) -> KofN:
    """Accept when tokens from at least k DISTINCT operators verify for `digest`.

    Per operator only the token with the earliest bound counts. The anchored time is the k-th
    smallest of those per-operator bounds (see the module docstring for why the k-th)."""
    if k < 1:
        raise TsaError(f"k must be at least 1, got {k}")
    ok: list[TokenResult] = []
    bad: list[str] = []
    for i, tok in enumerate(tokens):
        try:
            ok.append(verify_token(tok, digest, trust))
        except TsaError as exc:
            bad.append(f"token {i}: {exc}")
    best: dict[str, TokenResult] = {}
    for r in ok:
        cur = best.get(r.operator)
        if cur is None or r.bound_s(assumed_accuracy_s) < cur.bound_s(assumed_accuracy_s):
            best[r.operator] = r
    ranked = sorted(best.values(), key=lambda r: r.bound_s(assumed_accuracy_s))
    dup = len(ok) - len(best)
    if dup:
        bad.append(f"{dup} verified token(s) from an operator already counted; counted once")
    accepted = len(ranked) >= k
    when: int | None = None
    if accepted:
        t = ranked[k - 1].bound_s(assumed_accuracy_s)
        when = int(t) if t == int(t) else int(t) + 1
    return KofN(
        k=k,
        accepted=accepted,
        existed_before=when,
        operators=tuple(r.operator for r in ranked),
        results=tuple(ranked),
        rejected=tuple(bad),
    )


# ---------------------------------------------------------------- RFC 3161 client

_STATUS_OK = {"granted", "granted_with_mods"}


class Endpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    url: str
    operator: str  # the organisation that signs, by the signer certificate (not the URL's brand)
    min_gap_s: float = DEFAULT_MIN_GAP_S


# Probed anonymously on 2026-10-12 (prototype 5b). Entrust's URL is now served by Sectigo and
# QuoVadis' by DigiCert, so those share an operator with the endpoint of the same name.
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint(name="digicert", url="http://timestamp.digicert.com", operator="digicert"),
    Endpoint(name="sectigo", url="http://timestamp.sectigo.com", operator="sectigo"),
    Endpoint(
        name="globalsign",
        url="http://timestamp.globalsign.com/tsa/r6advanced1",
        operator="globalsign",
    ),
    Endpoint(name="freetsa", url="https://freetsa.org/tsr", operator="freetsa"),
    Endpoint(name="apple", url="http://timestamp.apple.com/ts01", operator="apple"),
    Endpoint(
        name="entrust", url="http://timestamp.entrust.net/TSS/RFC3161sha2TS", operator="sectigo"
    ),
    Endpoint(name="swisssign", url="http://tsa.swisssign.net", operator="swisssign"),
    Endpoint(name="certum", url="http://time.certum.pl", operator="certum"),
    Endpoint(name="identrust", url="http://timestamp.identrust.com", operator="identrust"),
    Endpoint(
        name="quovadis",
        url="http://tsa.quovadisglobal.com/TSS/HttpTspServer",
        operator="digicert",
    ),
    Endpoint(name="sslcom", url="http://ts.ssl.com", operator="sslcom"),
    Endpoint(name="dfn", url="https://zeitstempel.dfn.de", operator="dfn"),
)
# Three distinct organisations that answered anonymously with ECDSA/RSA-SHA2 tokens.
DEFAULT_ENDPOINTS: tuple[Endpoint, ...] = ENDPOINTS[:3]


def build_request(digest: bytes, nonce: int, cert_req: bool = True) -> bytes:
    if len(digest) != 32:
        raise TsaError(f"digest must be 32 bytes (SHA-256), got {len(digest)}; hash the data first")
    req = tsp.TimeStampReq(
        {
            "version": 1,
            "message_imprint": {
                "hash_algorithm": {"algorithm": "sha256"},
                "hashed_message": digest,
            },
            "nonce": nonce,
            "cert_req": cert_req,
        }
    )
    out: bytes = req.dump()
    return out


class TsaRefused(TsaError):
    """The TSA said no (HTTP 429 or a rejection status). The stamper stops using it."""


def _status_info(raw: bytes) -> tsp.PKIStatusInfo:
    """Parse only the PKIStatusInfo of a TimeStampResp. A refusing TSA sends no token, and
    asn1crypto's TimeStampResp spec insists on one, so the outer structure is split by hand."""
    _, _, _, _, body, _ = parser.parse(raw)
    _, _, _, hdr, content, trailer = parser.parse(body)
    return tsp.PKIStatusInfo.load(hdr + content + trailer)


def parse_response(raw: bytes, nonce: int | None = None) -> bytes:
    """Return the TimeStampToken DER, or raise TsaRefused/TsaError with the TSA's own status text."""
    try:
        info = _status_info(raw)
        status = info["status"].native
    except Exception as exc:
        raise TsaError(
            f"response is not a TimeStampResp ({exc}); first bytes {raw[:16].hex()}. "
            "Check the URL answers RFC 3161 over POST."
        ) from exc
    if status not in _STATUS_OK:
        text = info["status_string"].native
        fail = info["fail_info"].native
        raise TsaRefused(f"TSA refused: status={status} text={text} fail_info={fail}")
    resp = tsp.TimeStampResp.load(raw)
    token = resp["time_stamp_token"]
    if token.contents is None or not len(token.contents):
        raise TsaError("TSA granted but returned no token; try again later")
    tst = tsp.TSTInfo.load(token["content"]["encap_content_info"]["content"].contents)
    if nonce is not None and tst["nonce"].native != nonce:
        raise TsaError(
            f"nonce mismatch: sent {nonce}, token has {tst['nonce'].native}. "
            "A replayed or wrong token; discard it."
        )
    out: bytes = token.dump()
    return out


def request_token(
    url: str,
    digest: bytes,
    *,
    timeout_s: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """POST one RFC 3161 request and return the token. One request per call, no retries.

    Raises `TsaRefused` on HTTP 429 or a TSA rejection, `TsaError` on anything else."""
    nonce = secrets.randbits(63)
    body = build_request(digest, nonce)
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True, transport=transport) as c:
            r = c.post(url, content=body, headers={"Content-Type": "application/timestamp-query"})
    except httpx.HTTPError as exc:
        raise TsaError(
            f"{url}: {type(exc).__name__}: {exc}; check the network, retry later"
        ) from exc
    if r.status_code == 429:
        raise TsaRefused(f"{url}: HTTP 429 (rate limited); the stamper stops using this TSA")
    if r.status_code != 200:
        raise TsaError(f"{url}: HTTP {r.status_code}; the TSA did not accept the request")
    return parse_response(r.content, nonce)


# ---------------------------------------------------------------- proof format


class TsaProofData(BaseModel):
    """`AnchorProof.data` of kind "tsa": JSON. The tokens cover `root`, the Merkle root of the
    batch; `path` leads from the proof's digest to it (empty for a batch of one)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: int
    bundle: str  # trust bundle version the stamper verified against (informational)
    root: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: tuple[PathStep, ...]
    tokens: tuple[str, ...]  # base64 DER TimeStampTokens


class TsaAnchor:
    """Signal anchor: k-of-n RFC 3161 tokens over a Merkle root. `verify` is offline."""

    kind = "tsa"

    def __init__(
        self,
        trust: TrustStore | None = None,
        *,
        k: int = 2,
        assumed_accuracy_s: float = 1.0,
        stamper: TsaStamper | None = None,
    ) -> None:
        if k < 1:
            raise TsaError(f"k must be at least 1, got {k}")
        self.trust = trust if trust is not None else TrustStore.load()
        self.k = k
        self.assumed_accuracy_s = assumed_accuracy_s
        self.stamper = stamper

    def stamp_batch(self, digests: Sequence[bytes]) -> list[AnchorProof]:
        if self.stamper is None:
            raise TsaError(
                "this TsaAnchor is verify-only (no TsaStamper); pass stamper=TsaStamper(...)"
            )
        return self.stamper.stamp_batch(digests)

    def stamp(self, digest: bytes) -> AnchorProof:
        return self.stamp_batch([digest])[0]

    def verify(self, proof: AnchorProof, digest: bytes) -> TimeBound:
        _check_digest(proof, digest)
        if proof.kind != self.kind:
            raise TsaError(f"proof kind {proof.kind!r} is not {self.kind!r}")
        try:
            data = TsaProofData.model_validate_json(proof.data)
            tokens = [base64.b64decode(t, validate=True) for t in data.tokens]
        except (ValidationError, ValueError) as exc:
            raise TsaError(f"malformed tsa proof data: {exc}") from exc
        if data.v != PROOF_VERSION:
            raise TsaError(
                f"tsa proof version {data.v} is not supported (expected {PROOF_VERSION})"
            )
        if root_from_path(digest, data.path).hex() != data.root:
            raise TsaError(
                "Merkle path does not lead to the claimed root: tampered or wrong digest"
            )
        res = k_of_n(tokens, bytes.fromhex(data.root), self.trust, self.k, self.assumed_accuracy_s)
        if not res.accepted or res.existed_before is None:
            raise TsaError(
                f"{len(res.operators)} distinct operator(s) verified, need {self.k}. "
                f"Rejected: {'; '.join(res.rejected) or 'none'}"
            )
        return TimeBound(not_after=res.existed_before, kind=self.kind)


# ---------------------------------------------------------------- stamper


def _is_loopback(url: str) -> bool:
    return (urlparse(url).hostname or "") in {"127.0.0.1", "localhost", "::1"}


class TsaStamper:
    """Batches digests into a Merkle root and has it stamped by every active endpoint.

    - Never contacts one endpoint more often than every `max(min_gap_s, endpoint.min_gap_s)`;
      `flush()` just returns nothing until every active endpoint is ready, so the batch grows.
    - Stops using an endpoint for good on HTTP 429 or a TSA refusal (`stopped`), until `resume`.
    - Every token is verified against the trust bundle before it is put in a proof.
    """

    def __init__(
        self,
        endpoints: Sequence[Endpoint] = DEFAULT_ENDPOINTS,
        trust: TrustStore | None = None,
        *,
        k: int = 2,
        min_gap_s: float = DEFAULT_MIN_GAP_S,
        allow_fast_gap: bool = False,
        clock: Callable[[], float] = time.monotonic,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = 20.0,
        assumed_accuracy_s: float = 1.0,
    ) -> None:
        operators = {e.operator for e in endpoints}
        if len(operators) < k:
            raise TsaError(
                f"{len(endpoints)} endpoint(s) belong to {len(operators)} operator(s) "
                f"({', '.join(sorted(operators))}); k={k} needs at least {k} distinct operators"
            )
        if (
            min_gap_s < DEFAULT_MIN_GAP_S
            and not allow_fast_gap
            and not all(_is_loopback(e.url) for e in endpoints)
        ):
            raise TsaError(
                f"min_gap_s={min_gap_s} is below {DEFAULT_MIN_GAP_S}: public TSAs ask scripted "
                "clients for 15 s or more between requests (only loopback TSAs, as in the demo, "
                'or a config with "lab": true, may be hit faster)'
            )
        self.endpoints = tuple(endpoints)
        self.trust = trust if trust is not None else TrustStore.load()
        self.k = k
        self.min_gap_s = min_gap_s
        self.clock = clock
        self.transport = transport
        self.timeout_s = timeout_s
        self.assumed_accuracy_s = assumed_accuracy_s
        self.pending: list[bytes] = []
        self.stopped: dict[str, str] = {}  # endpoint name -> why it was stopped
        self.last_errors: list[str] = []
        self._last_request: dict[str, float] = {}

    def add(self, digest: bytes) -> None:
        if len(digest) != 32:
            raise TsaError(f"digest must be 32 bytes, got {len(digest)}")
        self.pending.append(digest)

    def resume(self, name: str) -> None:
        """Re-enable a stopped endpoint after a person has read why it was stopped."""
        if name not in self.stopped:
            raise TsaError(f"endpoint {name!r} is not stopped; stopped: {sorted(self.stopped)}")
        del self.stopped[name]

    def active(self) -> list[Endpoint]:
        return [e for e in self.endpoints if e.name not in self.stopped]

    def seconds_until_ready(self) -> float:
        """0 when every active endpoint may be contacted now, else the longest wait."""
        now = self.clock()
        waits = [
            self._last_request[e.name] + max(self.min_gap_s, e.min_gap_s) - now
            for e in self.active()
            if e.name in self._last_request
        ]
        return max([0.0, *waits])

    def flush(self) -> list[AnchorProof]:
        """Stamp the pending batch if every active endpoint is ready; else return [] and keep it."""
        if not self.pending or self.seconds_until_ready() > 0:
            return []
        proofs = self.stamp_batch(self.pending)
        self.pending = []
        return proofs

    def stamp_batch(self, digests: Sequence[bytes]) -> list[AnchorProof]:
        active = self.active()
        if len({e.operator for e in active}) < self.k:
            raise TsaError(
                f"only {len({e.operator for e in active})} operator(s) active, need {self.k}; "
                f"stopped: {self.stopped or 'none'}. Fix the cause, then call resume(name)"
            )
        wait = self.seconds_until_ready()
        if wait > 0:
            raise TsaError(
                f"rate limit: the next request to every TSA is allowed in {wait:.1f} s "
                f"(minimum gap {self.min_gap_s:.0f} s); batch more digests and retry then"
            )
        tree = MerkleTree(digests)
        tokens: list[bytes] = []
        verified: list[TokenResult] = []
        self.last_errors = []
        for ep in active:
            self._last_request[ep.name] = self.clock()
            try:
                tok = request_token(
                    ep.url, tree.root, timeout_s=self.timeout_s, transport=self.transport
                )
                verified.append(verify_token(tok, tree.root, self.trust))
            except TsaRefused as exc:
                self.stopped[ep.name] = str(exc)
                self.last_errors.append(f"{ep.name}: stopped: {exc}")
                continue
            except TsaError as exc:
                self.last_errors.append(f"{ep.name}: {exc}")
                continue
            tokens.append(tok)
        operators = {r.operator for r in verified}
        if len(operators) < self.k:
            raise TsaError(
                f"{len(operators)} distinct operator(s) returned a verifiable token, need "
                f"{self.k}: {'; '.join(self.last_errors) or 'no detail'}"
            )
        data = [base64.b64encode(t).decode() for t in tokens]
        return [
            AnchorProof(
                kind=TsaAnchor.kind,
                digest=d.hex(),
                data=json.dumps(
                    {
                        "v": PROOF_VERSION,
                        "bundle": self.trust.version,
                        "root": tree.root.hex(),
                        "path": [s.model_dump() for s in tree.path(i)],
                        "tokens": data,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            for i, d in enumerate(digests)
        ]
