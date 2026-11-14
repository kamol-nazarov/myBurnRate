"""Native auto-update: Ed25519, minisign, feed validation, download policy and install gating.

Nothing here touches the network or starts a process: the HTTP fetcher, the launcher, the
Authenticode check and the file lock are all injected.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import inspect
import itertools
import json
import sys
from pathlib import Path

import pytest

from spend_app import native_update as nu

ROOT = Path(__file__).resolve().parents[1]

# --- RFC 8032 section 7.1, tests 1-3 (public keys and signatures as published) ----------

RFC8032 = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "72",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "af82",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
        "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a",
    ),
]

# --- A complete minisign fixture produced with OpenSSL (Ed25519, -rawin), not with our code --

SEED = bytes(range(1, 33))
OSSL_PUB = bytes.fromhex(
    "79b5562e8fe654f94078b112e8a98ba7901f853ae695bed7e0e3910bad049664"
)
OSSL_DATA = b"BURNRATE native installer fixture\n" * 3
OSSL_TRUSTED = b"timestamp:1793236810\tfile:fixture-setup.exe"
OSSL_KEY_ID = bytes.fromhex("0807060504030201")
OSSL_SIG = {
    b"ED": bytes.fromhex(
        "7efd946a9a950f15e6c5b53c0714a6ac3ac7285061fe3ab3503393c1e4c96b0ace29c1ad616c6c0f04cb71a2a059416d21f928dfdacb3e1acdc1be8e47ad6c02"
    ),
    b"Ed": bytes.fromhex(
        "d0e092119f5349de410cd991f2dc3ddc5bdad8a245090c671b6a85de93b2262edc67e67a2d2c6a4f8f2fea743d19c64bfed06f12558c8056897dbd224621ac0d"
    ),
}
OSSL_GLOBAL = {
    b"ED": bytes.fromhex(
        "edc22e16c394e7e9917c5a20bdcbc5a22abc824358e601950b6fba5c14caae4647c1c77aef7c129e7388d0915aa27632010807c3edfdfe6238f6cbe7d20c6f0e"
    ),
    b"Ed": bytes.fromhex(
        "c624f131f0e950c94221a7e114fe19118dcec0b7b0ad0297acf87b7510bff441305deb81a8097c7bc47202715a54bb6d6c2dcfc1abf46b3f477c607b9550310b"
    ),
}


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def pubkey_file(key_id: bytes, key: bytes) -> str:
    text = (
        "untrusted comment: minisign public key: "
        + key_id[::-1].hex().upper()
        + "\n"
        + b64(b"Ed" + key_id + key)
        + "\n"
    )
    return b64(text.encode())


def sig_file(
    alg: bytes, key_id: bytes, sig: bytes, trusted: bytes, global_sig: bytes
) -> str:
    text = (
        "untrusted comment: signature from tauri secret key\n"
        + b64(alg + key_id + sig)
        + "\n"
        + "trusted comment: "
        + trusted.decode()
        + "\n"
        + b64(global_sig)
        + "\n"
    )
    return b64(text.encode())


# --- A test-only signer built on the module's curve arithmetic ----------------------------


def _encode(point) -> bytes:
    zinv = pow(point[2], nu._P - 2, nu._P)
    x, y = point[0] * zinv % nu._P, point[1] * zinv % nu._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def ed_public(seed: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _encode(nu._mul(a, nu.BASE))


def ed_sign(seed: bytes, message: bytes) -> bytes:
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    public = _encode(nu._mul(a, nu.BASE))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % nu._L
    r_enc = _encode(nu._mul(r, nu.BASE))
    k = (
        int.from_bytes(hashlib.sha512(r_enc + public + message).digest(), "little")
        % nu._L
    )
    return r_enc + ((r + k * a) % nu._L).to_bytes(32, "little")


def minisign(
    seed: bytes,
    data: bytes,
    *,
    alg: bytes = b"ED",
    key_id: bytes = OSSL_KEY_ID,
    trusted: bytes = b"timestamp:1\tfile:x",
) -> tuple[str, str]:
    """(public key file, signature file), both base64, for ``data``."""
    message = hashlib.blake2b(data, digest_size=64).digest() if alg == b"ED" else data
    sig = ed_sign(seed, message)
    global_sig = ed_sign(seed, sig + trusted)
    return pubkey_file(key_id, ed_public(seed)), sig_file(
        alg, key_id, sig, trusted, global_sig
    )


def ossl_files(alg: bytes) -> tuple[str, str]:
    return (
        pubkey_file(OSSL_KEY_ID, OSSL_PUB),
        sig_file(alg, OSSL_KEY_ID, OSSL_SIG[alg], OSSL_TRUSTED, OSSL_GLOBAL[alg]),
    )


# ---------------------------------------------------------------------------------------
# Ed25519
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(("seed", "message", "public", "signature"), RFC8032)
def test_rfc8032_vectors(seed, message, public, signature):
    msg, pub, sig = (
        bytes.fromhex(message),
        bytes.fromhex(public),
        bytes.fromhex(signature),
    )
    assert nu.ed25519_verify(pub, msg, sig) is True
    # our independent signer reproduces the published signature byte for byte
    assert ed_public(bytes.fromhex(seed)) == pub
    assert ed_sign(bytes.fromhex(seed), msg) == sig
    assert nu.ed25519_verify(pub, msg + b"\x00", sig) is False
    flipped = bytearray(sig)
    flipped[10] ^= 1
    assert nu.ed25519_verify(pub, msg, bytes(flipped)) is False


def test_openssl_signatures_match_the_independent_signer():
    assert ed_public(SEED) == OSSL_PUB
    prehash = hashlib.blake2b(OSSL_DATA, digest_size=64).digest()
    assert ed_sign(SEED, prehash) == OSSL_SIG[b"ED"]
    assert ed_sign(SEED, OSSL_DATA) == OSSL_SIG[b"Ed"]
    assert nu.ed25519_verify(OSSL_PUB, OSSL_DATA, OSSL_SIG[b"Ed"]) is True


def test_ed25519_rejects_non_canonical_s_and_bad_encodings():
    seed, message, public, signature = (
        bytes.fromhex(v)
        for v in (RFC8032[1][0], RFC8032[1][1], RFC8032[1][2], RFC8032[1][3])
    )
    s = int.from_bytes(signature[32:], "little")
    malleated = signature[:32] + (s + nu._L).to_bytes(
        32, "little"
    )  # S + L verifies mathematically
    assert nu.ed25519_verify(public, message, malleated) is False
    assert (
        nu.ed25519_verify(
            public, message, signature[:32] + nu._L.to_bytes(32, "little")
        )
        is False
    )
    assert nu.ed25519_verify(public, message, signature[:32] + b"\xff" * 32) is False
    # y >= p is non-canonical
    non_canonical = (nu._P + 1).to_bytes(32, "little")
    assert nu._decode_point(non_canonical) is None
    assert nu.ed25519_verify(non_canonical, message, signature) is False
    assert nu.ed25519_verify(public, message, non_canonical + signature[32:]) is False
    # a y with no matching x is not a point
    y = next(y for y in range(2, 100) if nu._recover_x(y, 0) is None)
    off_curve = y.to_bytes(32, "little")
    assert nu.ed25519_verify(off_curve, message, signature) is False
    assert nu.ed25519_verify(public, message, off_curve + signature[32:]) is False
    # x = 0 with the sign bit set is rejected
    assert nu._decode_point((1 | (1 << 255)).to_bytes(32, "little")) is None
    # wrong lengths
    assert nu.ed25519_verify(public[:31], message, signature) is False
    assert nu.ed25519_verify(public, message, signature[:63]) is False
    assert seed  # silence unused


# ---------------------------------------------------------------------------------------
# minisign
# ---------------------------------------------------------------------------------------


def test_pinned_key_matches_tauri_conf_and_parses():
    conf = json.loads(
        (ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    assert nu.PUBLIC_KEY_B64 == conf["plugins"]["updater"]["pubkey"]
    key = nu.parse_public_key(nu.PUBLIC_KEY_B64)
    assert len(key.key) == 32 and key.key_id[::-1].hex().upper() == "5CACF4C15C1FCD6D"
    assert nu.FEED_URL == (
        "https://github.com/kamol-nazarov/myBurnRate/releases/latest/download/native-latest.json"
    )
    endpoint = conf["plugins"]["updater"]["endpoints"][0]
    assert endpoint.rsplit("/", 1)[0] == nu.FEED_URL.rsplit("/", 1)[0]


@pytest.mark.parametrize("alg", [b"ED", b"Ed"])
def test_minisign_fixture_from_openssl_verifies(alg, tmp_path):
    pub, sig = ossl_files(alg)
    nu.verify_minisign(OSSL_DATA, sig, pub)
    path = tmp_path / "fixture-setup.exe"
    path.write_bytes(OSSL_DATA)
    nu.verify_minisign_file(path, sig, pub)


@pytest.mark.parametrize("alg", [b"ED", b"Ed"])
def test_minisign_signer_round_trip(alg):
    pub, sig = minisign(bytes(range(40, 72)), b"some installer bytes" * 100, alg=alg)
    nu.verify_minisign(b"some installer bytes" * 100, sig, pub)


def _expect_rejected(data, sig, pub):
    with pytest.raises(nu.SignatureError):
        nu.verify_minisign(data, sig, pub)


@pytest.mark.parametrize("alg", [b"ED", b"Ed"])
def test_minisign_rejects_every_kind_of_tampering(alg):
    pub, _ = ossl_files(alg)
    good = (alg, OSSL_KEY_ID, OSSL_SIG[alg], OSSL_TRUSTED, OSSL_GLOBAL[alg])

    def rebuild(**changes):
        parts = dict(
            zip(("alg", "key_id", "sig", "trusted", "global_sig"), good, strict=True)
        )
        parts.update(changes)
        return sig_file(**parts)

    # one flipped bit in the file, the signature, the trusted comment, the global signature
    _expect_rejected(OSSL_DATA[:-1] + b"\x00", rebuild(), pub)
    flipped = bytearray(OSSL_SIG[alg])
    flipped[3] ^= 0x10
    _expect_rejected(OSSL_DATA, rebuild(sig=bytes(flipped)), pub)
    _expect_rejected(OSSL_DATA, rebuild(trusted=OSSL_TRUSTED + b"!"), pub)
    flipped = bytearray(OSSL_GLOBAL[alg])
    flipped[60] ^= 1
    _expect_rejected(OSSL_DATA, rebuild(global_sig=bytes(flipped)), pub)
    # wrong key id, wrong or swapped algorithm tag
    _expect_rejected(OSSL_DATA, rebuild(key_id=b"\x00" * 8), pub)
    _expect_rejected(OSSL_DATA, rebuild(alg=b"XX"), pub)
    _expect_rejected(OSSL_DATA, rebuild(alg=b"Ed" if alg == b"ED" else b"ED"), pub)
    # S >= L
    s = int.from_bytes(OSSL_SIG[alg][32:], "little")
    malleated = OSSL_SIG[alg][:32] + (s + nu._L).to_bytes(32, "little")
    _expect_rejected(OSSL_DATA, rebuild(sig=malleated), pub)
    # the right signature under a different public key
    other = pubkey_file(OSSL_KEY_ID, ed_public(bytes(range(9, 41))))
    _expect_rejected(OSSL_DATA, rebuild(), other)


def test_minisign_rejects_truncated_and_garbage_input():
    pub, sig = ossl_files(b"ED")
    for bad in (
        "",
        "!!not base64!!",
        sig[:40],
        sig[:-8],
        b64(b"untrusted comment: x\nAAAA\n"),
        b64(b"\xff\xfe\x00"),
        b64(
            b"untrusted comment: x\n"
            + b64(b"short").encode()
            + b"\ntrusted comment: y\n"
            + b64(b"z" * 64).encode()
        ),
        b64(b"one\ntwo\nthree\nfour\n"),
        sig + "AAAA",
    ):
        _expect_rejected(OSSL_DATA, bad, pub)
    for bad_key in (
        "",
        "####",
        pub[:30],
        b64(b"untrusted comment: x\nRWQ=\n"),
        b64(b"no key line"),
    ):
        _expect_rejected(OSSL_DATA, sig, bad_key)


# ---------------------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------------------

ORDER = [
    "0.5.3",
    "0.6.0-alpha.1",
    "0.6.0-alpha.2",
    "0.6.0-alpha.10",
    "0.6.0-alpha.beta",
    "0.6.0-beta",
    "0.6.0-beta.2",
    "0.6.0-beta.11",
    "0.6.0-rc.1",
    "0.6.0",
    "0.6.1",
    "0.10.0",
    "1.0.0",
]


def test_version_ordering_table():
    keys = [nu.parse_version(v) for v in ORDER]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)
    for lower, higher in itertools.pairwise(ORDER):
        assert nu.is_newer(higher, lower) and not nu.is_newer(lower, higher)
    assert not nu.is_newer("0.6.0", "0.6.0")
    assert not nu.is_newer(
        "0.6.0-beta.2", "0.6.0"
    )  # a prerelease never beats its release
    assert nu.is_newer("0.6.0", "0.6.0-beta.2")
    assert nu.parse_version("v1.2.3+build.5") == nu.parse_version("1.2.3+other")
    assert nu.is_newer("0.4.0", "0.4.0b2")  # the old PEP 440 spelling still orders


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "1",
        "1.2",
        "1.2.3.4",
        "01.2.3",
        "1.2.3-",
        "1.2.3-01",
        "a.b.c",
        "1.2.3 beta",
        "x" * 80,
    ],
)
def test_invalid_versions_are_refused(bad):
    with pytest.raises(ValueError):
        nu.parse_version(bad)


# ---------------------------------------------------------------------------------------
# harness for feed / download / install
# ---------------------------------------------------------------------------------------

GH = "https://github.com/kamol-nazarov/myBurnRate/releases"
ASSET_HOST = "https://release-assets.githubusercontent.com/github-production-release-asset/1/abc?sig=1"
INSTALLER = b"MZ" + bytes(range(256)) * 40
KEY_SEED = bytes(range(100, 132))
NEW_VERSION = "9.9.9"


class Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.closed = False

    def iter_chunks(self, size):
        for i in range(0, len(self.body), size):
            yield self.body[i : i + size]

    def close(self):
        self.closed = True


class Net:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        route = self.routes[url]
        return route() if callable(route) else route


def asset_url(version=NEW_VERSION):
    return f"{GH}/download/v{version}/myBurnRate_{version}_x64-setup.exe"


def make_feed(
    data=INSTALLER,
    *,
    version=NEW_VERSION,
    alg=b"ED",
    seed=KEY_SEED,
    trusted=None,
    **overrides,
):
    if trusted is None:  # what `tauri signer sign` records for the final file name
        trusted = f"timestamp:1\tfile:myBurnRate_{version}_x64-setup.exe".encode()
    pub, sig = minisign(seed, data, alg=alg, trusted=trusted)
    entry = {
        "url": asset_url(version),
        "signature": sig,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
    }
    feed = {
        "version": version,
        "notes": "What changed.",
        "pub_date": "2026-11-01T00:00:00Z",
        "platforms": {"windows-x86_64": entry},
    }
    for key, value in overrides.items():
        if key == "entry":
            entry.update(value)
        else:
            feed[key] = value
    return pub, feed


def feed_routes(feed, data=INSTALLER, **extra):
    routes = {
        nu.FEED_URL: lambda: Resp(200, json.dumps(feed).encode()),
        asset_url(): lambda: Resp(200, data),
    }
    routes.update(extra)
    return Net(routes)


class Fixture:
    def __init__(
        self,
        tmp_path,
        net,
        pub,
        *,
        marker=True,
        launcher=None,
        auth=None,
        frozen=True,
        platform="win32",
        env=None,
        current="1.0.0",
    ):
        self.tmp = tmp_path
        self.launched = []
        app = tmp_path / "app"
        app.mkdir(parents=True, exist_ok=True)
        if marker:
            (app / nu.INSTALL_MARKER).write_text("{}", encoding="utf-8")
        self.updates = tmp_path / "data" / "updates"
        self.updater = nu.Updater(
            fetcher=net,
            launcher=launcher
            or (lambda path, args: self.launched.append((path, args))),
            authenticode=auth
            or (lambda path: {"status": "NotSigned", "thumbprint": "", "subject": ""}),
            env=env if env is not None else {},
            frozen=frozen,
            executable=app / "BURNRATE.exe",
            platform=platform,
            data_dir=tmp_path / "data",
            current_version=current,
            lock=lambda path: contextlib.nullcontext(),
            public_key=pub,
        )

    def leftovers(self):
        return (
            sorted(p.name for p in self.updates.glob("*"))
            if self.updates.exists()
            else []
        )


def fixture(tmp_path, **kwargs):
    pub, feed = make_feed(
        **{
            k: kwargs.pop(k)
            for k in ("data", "version", "alg", "entry", "trusted")
            if k in kwargs
        }
    )
    net = feed_routes(feed, kwargs.pop("served", INSTALLER))
    return Fixture(tmp_path, net, kwargs.pop("pub", pub), **kwargs), net


# ---------------------------------------------------------------------------------------
# status / kind
# ---------------------------------------------------------------------------------------


def test_install_kind_detection(tmp_path):
    cases = [
        ({"frozen": False, "marker": True}, "source", False),
        ({"frozen": True, "marker": False}, "portable", False),
        ({"frozen": True, "marker": True}, "installer", True),
        ({"frozen": True, "marker": True, "platform": "linux"}, "installer", False),
    ]
    for index, (opts, kind, can) in enumerate(cases):
        fx = Fixture(
            tmp_path / str(index), Net({}), nu.PUBLIC_KEY_B64, current="0.5.3", **opts
        )
        assert fx.updater.status() == {
            "kind": kind,
            "current": "0.5.3",
            "canAutoUpdate": can,
        }
    status = nu.update_status()
    assert set(status) == {"kind", "current", "canAutoUpdate"}
    assert (
        status["kind"] == "source" and status["canAutoUpdate"] is False
    )  # tests are not frozen


def test_public_signatures_match_the_host_contract():
    check = inspect.signature(nu.check_for_update)
    assert (
        list(check.parameters) == ["timeout"]
        and check.parameters["timeout"].default == 10.0
    )
    install = inspect.signature(nu.install_update)
    assert (
        list(install.parameters) == ["progress"]
        and install.parameters["progress"].default is None
    )


# ---------------------------------------------------------------------------------------
# feed validation
# ---------------------------------------------------------------------------------------


def parse(feed):
    return nu.parse_feed(json.dumps(feed).encode())


def test_feed_accepts_a_good_feed():
    _, feed = make_feed()
    parsed = parse(feed)
    assert parsed.version == NEW_VERSION and parsed.asset.size == len(INSTALLER)
    assert parsed.asset.sha256 == hashlib.sha256(INSTALLER).hexdigest()
    _, upper = make_feed(
        entry={"sha256": hashlib.sha256(INSTALLER).hexdigest().upper()}
    )
    assert parse(upper).asset.sha256 == hashlib.sha256(INSTALLER).hexdigest()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.pop("version"),
        lambda f: f.update(version="not-a-version"),
        lambda f: f.update(version=9),
        lambda f: f.pop("platforms"),
        lambda f: f.update(platforms=[]),
        lambda f: f["platforms"].clear(),
        lambda f: f["platforms"].update({"windows-x86_64": "nope"}),
        lambda f: f["platforms"]["windows-x86_64"].pop("url"),
        lambda f: f["platforms"]["windows-x86_64"].pop("signature"),
        lambda f: f["platforms"]["windows-x86_64"].update(signature=""),
        lambda f: f["platforms"]["windows-x86_64"].pop("sha256"),
        lambda f: f["platforms"]["windows-x86_64"].update(sha256="abc"),
        lambda f: f["platforms"]["windows-x86_64"].update(sha256="z" * 64),
        lambda f: f["platforms"]["windows-x86_64"].pop("size"),
        lambda f: f["platforms"]["windows-x86_64"].update(size=0),
        lambda f: f["platforms"]["windows-x86_64"].update(size=-5),
        lambda f: f["platforms"]["windows-x86_64"].update(size=True),
        lambda f: f["platforms"]["windows-x86_64"].update(size="123"),
        lambda f: f["platforms"]["windows-x86_64"].update(
            size=nu.MAX_INSTALLER_BYTES + 1
        ),
    ],
)
def test_feed_rejects_missing_or_malformed_fields(mutate):
    _, feed = make_feed()
    mutate(feed)
    with pytest.raises(nu.FeedError):
        parse(feed)


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"null", b"\xff\xfe", b"{}"])
def test_feed_rejects_non_feeds(raw):
    with pytest.raises(nu.FeedError):
        nu.parse_feed(raw)


@pytest.mark.parametrize(
    "url",
    [
        asset_url().replace("https://", "http://"),  # not https
        "https://github.com.evil.example/kamol-nazarov/myBurnRate/releases/download/v1/x.exe",
        "https://evil.example/kamol-nazarov/myBurnRate/releases/download/v1/x.exe",
        "https://objects.githubusercontent.com/kamol-nazarov/myBurnRate/releases/download/v1/x.exe",
        "https://github.com/someone-else/myBurnRate/releases/download/v1/x.exe",  # foreign repo
        "https://github.com/kamol-nazarov/other/releases/download/v1/x.exe",
        "https://github.com/kamol-nazarov/myBurnRate/archive/v1.zip",
        "https://github.com/kamol-nazarov/myBurnRate/releases/latest/download/x.exe",
        "https://github.com/kamol-nazarov/myBurnRate/releases/download/",
        "https://github.com/kamol-nazarov/myBurnRate/releases/download/../../../x.exe",
        "https://github.com/kamol-nazarov/myBurnRate/releases/download/v1/%2e%2e/x.exe",
        "https://user@github.com/kamol-nazarov/myBurnRate/releases/download/v1/x.exe",
        "https://github.com:8443/kamol-nazarov/myBurnRate/releases/download/v1/x.exe",
        "https://github.com/kamol-nazarov/myBurnRate/releases/download/v1/x.exe?x=1",
        "https://github.com/kamol-nazarov/myBurnRate/releases/download/v1/x.exe#frag",
        "https://github.com/kamol-nazarov/myBurnRate/releases/download/v1/x y.exe",
        "ftp://github.com/kamol-nazarov/myBurnRate/releases/download/v1/x.exe",
        "//github.com/kamol-nazarov/myBurnRate/releases/download/v1/x.exe",
        "",
        None,
        7,
    ],
)
def test_feed_rejects_foreign_or_unsafe_download_urls(url):
    _, feed = make_feed(entry={"url": url})
    with pytest.raises(nu.FeedError):
        parse(feed)


def test_notes_are_cleaned_and_capped():
    _, feed = make_feed(notes="line\x00one\x07\nline two\n" + "x" * 9000)
    notes = parse(feed).notes
    assert "\x00" not in notes and "\x07" not in notes and "\n" in notes
    assert len(notes) <= nu.MAX_NOTES


# ---------------------------------------------------------------------------------------
# check_for_update
# ---------------------------------------------------------------------------------------


def test_check_reports_a_newer_version(tmp_path):
    fx, net = fixture(tmp_path)
    result = fx.updater.check()
    assert result == {
        "ok": True,
        "current": "1.0.0",
        "available": True,
        "version": NEW_VERSION,
        "notes": "What changed.",
        "pageUrl": f"{GH}/tag/v{NEW_VERSION}",
        "canInstall": True,
    }
    assert net.calls == [nu.FEED_URL]


@pytest.mark.parametrize("current", ["9.9.9", "10.0.0", "9.9.9-beta.2"])
def test_check_ignores_equal_older_or_prerelease_builds(tmp_path, current):
    fx, _ = fixture(tmp_path, current=current)
    result = fx.updater.check()
    if current == "9.9.9-beta.2":  # the release is newer than its own prerelease
        assert result["available"] is True
    else:
        assert result["ok"] is True and result["available"] is False
        assert result["version"] == "" and result["canInstall"] is False
    assert result["pageUrl"].startswith(GH)


def test_prerelease_feed_is_not_newer_than_its_release(tmp_path):
    fx, _ = fixture(tmp_path, version="0.6.0-beta.2", current="0.6.0")
    assert fx.updater.check()["available"] is False


@pytest.mark.parametrize("kind", ["portable", "source"])
def test_portable_and_source_get_a_page_but_cannot_install(tmp_path, kind):
    fx, net = fixture(tmp_path, marker=False, frozen=(kind == "portable"))
    result = fx.updater.check()
    assert result["available"] is True and result["canInstall"] is False
    assert result["pageUrl"].startswith(
        "https://github.com/kamol-nazarov/myBurnRate/releases/"
    )
    out = fx.updater.install()
    assert out["ok"] is False and "error" in out
    assert net.calls == [nu.FEED_URL]  # the installer was never requested
    assert fx.launched == [] and fx.leftovers() == []


def test_opt_out_env_skips_the_network(tmp_path):
    fx, net = fixture(tmp_path, env={"BURNRATE_NO_UPDATE_CHECK": "1"})
    result = fx.updater.check()
    assert (
        result["ok"] is True
        and result["available"] is False
        and result["canInstall"] is False
    )
    assert fx.updater.install()["ok"] is False
    assert net.calls == [] and fx.launched == []


def test_opt_out_only_for_truthy_values(tmp_path):
    fx, _net = fixture(tmp_path, env={"BURNRATE_NO_UPDATE_CHECK": "0"})
    assert fx.updater.check()["available"] is True


# ---------------------------------------------------------------------------------------
# redirects and sizes
# ---------------------------------------------------------------------------------------

LATEST_DOWNLOAD = f"{GH}/download/v{NEW_VERSION}/native-latest.json"


def test_feed_redirect_chain_through_github_is_followed():
    _, feed = make_feed()
    body = json.dumps(feed).encode()
    net = Net(
        {
            nu.FEED_URL: lambda: Resp(
                302,
                headers={
                    "Location": f"/kamol-nazarov/myBurnRate/releases/download/v{NEW_VERSION}/native-latest.json"
                },
            ),
            LATEST_DOWNLOAD: lambda: Resp(302, headers={"Location": ASSET_HOST}),
            ASSET_HOST: lambda: Resp(200, body),
        }
    )
    updater = nu.Updater(fetcher=net, env={}, current_version="1.0.0", frozen=False)
    assert updater.check()["available"] is True
    assert net.calls == [nu.FEED_URL, LATEST_DOWNLOAD, ASSET_HOST]


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/native-latest.json",
        "https://github.com.evil.example/kamol-nazarov/myBurnRate/releases/download/v1/x",
        "http://objects.githubusercontent.com/x",  # downgrade to http
        "https://raw.githubusercontent.com/kamol-nazarov/myBurnRate/main/x",
        "https://github.com/someone-else/repo/releases/download/v1/x",
        "https://user@objects.githubusercontent.com/x",
        "https://objects.githubusercontent.com:444/x",
        "file:///c:/windows/system32/cmd.exe",
        "https://objects.githubusercontent.com.evil.example/x",
    ],
)
def test_redirect_to_foreign_hosts_is_refused(target, tmp_path):
    net = Net({nu.FEED_URL: lambda: Resp(302, headers={"Location": target})})
    updater = nu.Updater(fetcher=net, env={}, current_version="1.0.0", frozen=False)
    result = updater.check()
    assert result["ok"] is False and "redirected" in result["error"]
    assert net.calls == [nu.FEED_URL]  # never fetched the foreign target


def test_redirect_loops_and_missing_locations_fail():
    net = Net({nu.FEED_URL: lambda: Resp(302, headers={"Location": nu.FEED_URL})})
    updater = nu.Updater(fetcher=net, env={}, current_version="1.0.0", frozen=False)
    assert updater.check()["ok"] is False
    assert len(net.calls) <= nu.MAX_REDIRECTS + 1
    net = Net({nu.FEED_URL: lambda: Resp(302)})
    updater = nu.Updater(fetcher=net, env={}, current_version="1.0.0", frozen=False)
    assert updater.check()["ok"] is False


def test_responses_are_closed_and_bad_status_is_an_error():
    holder = {}

    def bad():
        holder["r"] = Resp(503)
        return holder["r"]

    updater = nu.Updater(
        fetcher=Net({nu.FEED_URL: bad}), env={}, current_version="1.0.0", frozen=False
    )
    assert updater.check()["ok"] is False and holder["r"].closed


def test_oversize_feed_is_refused_by_header_and_by_stream():
    big = b"{" + b" " * (nu.MAX_FEED_BYTES + 10)
    for response in (
        lambda: Resp(200, b"{}", {"Content-Length": str(nu.MAX_FEED_BYTES + 1)}),
        lambda: Resp(200, big),
    ):
        updater = nu.Updater(
            fetcher=Net({nu.FEED_URL: response}),
            env={},
            current_version="1.0.0",
            frozen=False,
        )
        result = updater.check()
        assert result["ok"] is False and "larger" in result["error"]


# ---------------------------------------------------------------------------------------
# install_update
# ---------------------------------------------------------------------------------------


def test_install_downloads_verifies_and_launches_once(tmp_path):
    fx, net = fixture(tmp_path)
    events = []
    result = fx.updater.install(progress=events.append)
    assert result == {"ok": True, "restarting": True}
    assert len(fx.launched) == 1
    path, args = fx.launched[0]
    assert path.parent == fx.updates and path.read_bytes() == INSTALLER
    assert path.name == f"myBurnRate_{NEW_VERSION}_x64-setup.exe"
    assert args == ["/S", "/UPDATE"]
    assert net.calls == [nu.FEED_URL, asset_url()]
    phases = [e["phase"] for e in events]
    assert phases[0] == "checking" and "downloading" in phases
    assert phases.index("verifying") < phases.index("launching") == len(phases) - 1
    pct = [e["percent"] for e in events if e["phase"] == "downloading"]
    assert pct == sorted(pct) and pct[-1] == 100.0
    assert all(set(e) == {"phase", "percent"} for e in events)


@pytest.mark.parametrize("alg", [b"ED", b"Ed"])
def test_install_accepts_both_signature_algorithms(tmp_path, alg):
    fx, _ = fixture(tmp_path, alg=alg)
    assert fx.updater.install()["ok"] is True and len(fx.launched) == 1


def test_install_follows_github_redirects_to_the_asset_host(tmp_path):
    pub, feed = make_feed()
    net = feed_routes(
        feed,
    )
    net.routes[asset_url()] = lambda: Resp(302, headers={"Location": ASSET_HOST})
    net.routes[ASSET_HOST] = lambda: Resp(200, INSTALLER)
    fx = Fixture(tmp_path, net, pub)
    assert fx.updater.install()["ok"] is True
    assert net.calls == [nu.FEED_URL, asset_url(), ASSET_HOST]


def test_progress_callback_errors_never_break_the_install(tmp_path):
    fx, _ = fixture(tmp_path)

    def boom(_event):
        raise RuntimeError("ui exploded")

    assert fx.updater.install(progress=boom)["ok"] is True


def _assert_no_install(fx, result):
    assert result["ok"] is False and result["error"]
    assert fx.launched == []
    assert fx.leftovers() == []  # the downloaded file (and any .part) is gone


def test_install_refuses_and_cleans_up_on_every_failure(tmp_path):
    _pub, _feed = make_feed()
    other_pub = minisign(bytes(range(7, 39)), INSTALLER)[0]
    tampered = INSTALLER[:-1] + b"\x00"
    cases = {
        "sha mismatch": {"served": tampered},
        "size mismatch (short)": {"served": INSTALLER[:-5]},
        "size mismatch (long)": {"served": INSTALLER + b"x"},
        "wrong key": {"pub": other_pub},
        "bad signature text": {"entry": {"signature": "AAAA"}},
        "foreign installer url": {"entry": {"url": "https://evil.example/x.exe"}},
    }
    for index, (label, opts) in enumerate(cases.items()):
        fx, _ = fixture(tmp_path / f"c{index}", **opts)
        _assert_no_install(fx, fx.updater.install())
        del label


def test_install_refuses_oversize_downloads(tmp_path):
    fx, _ = fixture(
        tmp_path, entry={"size": 100}
    )  # feed claims 100 bytes, server sends much more
    _assert_no_install(fx, fx.updater.install())
    big = Resp(200, b"x", {"Content-Length": str(nu.MAX_INSTALLER_BYTES + 1)})
    pub, feed = make_feed()
    net = feed_routes(feed)
    net.routes[asset_url()] = lambda: big
    fx = Fixture(tmp_path / "again", net, pub)
    _assert_no_install(fx, fx.updater.install())


def test_install_refuses_redirects_off_github(tmp_path):
    pub, feed = make_feed()
    net = feed_routes(feed)
    net.routes[asset_url()] = lambda: Resp(
        302, headers={"Location": "https://evil.example/x.exe"}
    )
    fx = Fixture(tmp_path, net, pub)
    _assert_no_install(fx, fx.updater.install())
    assert "https://evil.example/x.exe" not in net.calls


def test_install_deletes_the_file_when_the_launcher_fails(tmp_path):
    pub, feed = make_feed()

    def broken(path, args):
        raise OSError("cannot start")

    fx = Fixture(tmp_path, feed_routes(feed), pub, launcher=broken)
    result = fx.updater.install()
    assert result["ok"] is False and fx.leftovers() == []


def test_install_not_needed_when_already_current(tmp_path):
    fx, net = fixture(tmp_path, current="9.9.9")
    _assert_no_install(fx, fx.updater.install())
    assert net.calls == [nu.FEED_URL]


def test_stale_downloads_are_cleared_before_a_new_one(tmp_path):
    fx, _ = fixture(tmp_path)
    fx.updates.mkdir(parents=True)
    (fx.updates / "myBurnRate_0.1.0_x64-setup.exe").write_bytes(b"old")
    (fx.updates / "myBurnRate_0.2.0_x64-setup.exe.part").write_bytes(b"old")
    (fx.updates / "keep-me.txt").write_text("not ours", encoding="utf-8")
    assert fx.updater.install()["ok"] is True
    assert fx.leftovers() == ["keep-me.txt", "myBurnRate_9.9.9_x64-setup.exe"]


# ---------------------------------------------------------------------------------------
# Authenticode policy
# ---------------------------------------------------------------------------------------

SIGNED_APP = {
    "status": "Valid",
    "thumbprint": "AAA1",
    "subject": "CN=SignPath Foundation",
}


def auth_for(app, installer):
    def check(path):
        return app if Path(path).name == "BURNRATE.exe" else installer

    return check


@pytest.mark.parametrize(
    ("installer", "ok"),
    [
        (
            {"status": "NotSigned", "thumbprint": "", "subject": ""},
            False,
        ),  # never downgrade
        (None, False),
        (
            {"status": "Valid", "thumbprint": "BBB2", "subject": "CN=Someone Else"},
            False,
        ),
        (
            {
                "status": "HashMismatch",
                "thumbprint": "AAA1",
                "subject": "CN=SignPath Foundation",
            },
            False,
        ),
        (
            {
                "status": "NotTrusted",
                "thumbprint": "AAA1",
                "subject": "CN=SignPath Foundation",
            },
            False,
        ),
        (
            {
                "status": "Valid",
                "thumbprint": "AAA1",
                "subject": "CN=SignPath Foundation",
            },
            True,
        ),
        (
            {
                "status": "Valid",
                "thumbprint": "NEW9",
                "subject": "CN=SignPath Foundation",
            },
            True,
        ),  # renewed cert
    ],
)
def test_signed_app_requires_an_installer_from_the_same_publisher(
    tmp_path, installer, ok
):
    fx, _ = fixture(tmp_path, auth=auth_for(SIGNED_APP, installer))
    result = fx.updater.install()
    if ok:
        assert result["ok"] is True and len(fx.launched) == 1
    else:
        _assert_no_install(fx, result)


def test_unsigned_app_may_install_any_otherwise_valid_update(tmp_path):
    unsigned = {"status": "NotSigned", "thumbprint": "", "subject": ""}
    for index, installer in enumerate([unsigned, SIGNED_APP]):
        fx, _ = fixture(tmp_path / str(index), auth=auth_for(unsigned, installer))
        assert fx.updater.install()["ok"] is True


def test_unreadable_app_signature_fails_closed(tmp_path):
    fx, _ = fixture(tmp_path, auth=lambda path: None)
    _assert_no_install(fx, fx.updater.install())


def test_signer_policy_unit():
    with pytest.raises(nu.UpdateError):
        nu.check_signer_policy(None, None)
    nu.check_signer_policy(
        {"status": "NotSigned", "thumbprint": "", "subject": ""}, None
    )
    with pytest.raises(nu.UpdateError):
        nu.check_signer_policy(SIGNED_APP, None)
    nu.check_signer_policy(SIGNED_APP, dict(SIGNED_APP))


# ---------------------------------------------------------------------------------------
# never raises
# ---------------------------------------------------------------------------------------


class Weird(Exception):
    pass


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("x"),
        OSError("net down"),
        ValueError("v"),
        KeyError("k"),
        ZeroDivisionError(),
        TimeoutError(),
        UnicodeError("u"),
        RecursionError(),
        MemoryError(),
        Weird("w"),
        ConnectionResetError(),
    ],
)
def test_nothing_raises_whatever_the_fetcher_throws(tmp_path, exc):
    def boom(url, timeout):
        raise exc

    fx = Fixture(tmp_path, boom, nu.PUBLIC_KEY_B64)
    result = fx.updater.check()
    assert (
        result["ok"] is False and isinstance(result["error"], str) and result["error"]
    )
    assert set(result) >= {
        "ok",
        "error",
        "current",
        "available",
        "version",
        "notes",
        "pageUrl",
        "canInstall",
    }
    out = fx.updater.install()
    assert out["ok"] is False and isinstance(out["error"], str)
    assert fx.launched == [] and fx.leftovers() == []


def test_nothing_raises_when_the_stream_dies_mid_download(tmp_path):
    pub, feed = make_feed()

    class Dying(Resp):
        def iter_chunks(self, size):
            yield INSTALLER[:100]
            raise ConnectionResetError("reset")

    net = feed_routes(feed)
    net.routes[asset_url()] = lambda: Dying(200, b"")
    fx = Fixture(tmp_path, net, pub)
    _assert_no_install(fx, fx.updater.install())


def test_nothing_raises_with_a_broken_environment(tmp_path):
    fx, _ = fixture(tmp_path, current="garbage")
    assert fx.updater.check()["ok"] is False
    updater = nu.Updater(executable=Path("\0bad"), frozen=True, current_version="1.0.0")
    assert updater.status()["kind"] in {"portable", "installer", "source"}
    for func in (nu.update_status, nu.check_for_update, nu.install_update):
        assert callable(func)


def test_error_messages_never_echo_exception_internals(tmp_path):
    secret = "C:\\Users\\private\\token=abc123"

    def boom(url, timeout):
        raise RuntimeError(secret)

    fx = Fixture(tmp_path, boom, nu.PUBLIC_KEY_B64)
    assert secret not in json.dumps(fx.updater.check())
    assert secret not in json.dumps(fx.updater.install())


# ---------------------------------------------------------------------------------------
# real-pinned-key behaviour and the file lock
# ---------------------------------------------------------------------------------------


def test_a_signature_from_any_other_key_fails_against_the_pinned_key(tmp_path):
    _pub, feed = make_feed()
    fx = Fixture(tmp_path, feed_routes(feed), nu.PUBLIC_KEY_B64)  # default pinned key
    _assert_no_install(fx, fx.updater.install())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing semantics")
def test_the_download_is_locked_against_writes_while_it_is_checked(tmp_path):
    target = tmp_path / "locked.exe"
    target.write_bytes(b"data")
    with nu._deny_writes(target):
        assert target.read_bytes() == b"data"
        with pytest.raises(PermissionError):
            target.open("ab").close()
        with pytest.raises(PermissionError):
            target.unlink()
    target.unlink()  # released again


# ---------------------------------------------------------------------------------------
# the signature is bound to this exact installer and version
# ---------------------------------------------------------------------------------------

# Tauri's own signature for the published v0.5.3 installer (public data, no binary needed).
REAL_SIG_V053 = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVSdHpSOWN3ZlNzWEtKeDBPV0Fn"
    "NjRUY2x0bjlueTJmQmIyV3E5VzhvSTUvT1UvMElMVmI2YlJ3elVIQmc5N2JlVDVzcXdzSnYrOHRsMGFyVVNLdE5zM2tKTFhk"
    "bDVGdUE4PQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzkzMjM2ODEwCWZpbGU6QlVSTlJBVEVfMC41LjNfeDY0LXNl"
    "dHVwLmV4ZQl2ZXJzaW9uOjAuNS4zCjV1RThnOWNJRUQ2TUpSdG9CQkd4WjBUTXV6aS9GbnJZMURKWTM2Y3ZkUWp5ZzlHcnMx"
    "OVhGZlI1T1BDV2wyaHFHMHdpTXIyaFFXSHIzQ1p5dHJ4ZUNnPT0K"
)


def test_real_tauri_signature_names_the_file_and_version():
    parsed = nu.parse_signature(REAL_SIG_V053)
    assert parsed.algorithm == b"ED"
    assert (
        parsed.trusted_comment
        == b"timestamp:1793236810\tfile:BURNRATE_0.5.3_x64-setup.exe\tversion:0.5.3"
    )
    assert nu.trusted_fields(parsed.trusted_comment) == {
        "timestamp": "1793236810",
        "file": "BURNRATE_0.5.3_x64-setup.exe",
        "version": "0.5.3",
    }
    assert parsed.key_id == nu.parse_public_key(nu.PUBLIC_KEY_B64).key_id


def test_signed_file_name_is_enforced_by_verify_minisign():
    pub, sig = minisign(
        KEY_SEED, INSTALLER, trusted=b"timestamp:1\tfile:a.exe\tversion:1.2.3"
    )
    nu.verify_minisign(INSTALLER, sig, pub)  # no expectation: signature alone
    nu.verify_minisign(
        INSTALLER, sig, pub, expected_file="a.exe", expected_version="1.2.3"
    )
    for kwargs in (
        {"expected_file": "b.exe"},
        {"expected_file": "A.EXE"},
        {"expected_file": "a.exe", "expected_version": "1.2.4"},
    ):
        with pytest.raises(nu.SignatureError):
            nu.verify_minisign(INSTALLER, sig, pub, **kwargs)
    for trusted in (
        b"timestamp:1",
        b"timestamp:1\tfilename:a.exe",
        b"timestamp:1\tfile:",
    ):
        pub2, sig2 = minisign(KEY_SEED, INSTALLER, trusted=trusted)
        with pytest.raises(nu.SignatureError):
            nu.verify_minisign(INSTALLER, sig2, pub2, expected_file="a.exe")
    # the comment is covered by the global signature: editing the file name breaks it
    pub3, sig3 = ossl_files(b"ED")
    nu.verify_minisign(OSSL_DATA, sig3, pub3, expected_file="fixture-setup.exe")
    with pytest.raises(nu.SignatureError):
        nu.verify_minisign(OSSL_DATA, sig3, pub3, expected_file="other-setup.exe")


def test_tauri_installer_signature_is_not_a_native_installer(tmp_path):
    # Same key, same bytes, but signed as the Tauri installer's file name.
    fx, _ = fixture(
        tmp_path,
        trusted=f"timestamp:1\tfile:BURNRATE_{NEW_VERSION}_x64-setup.exe".encode(),
    )
    _assert_no_install(fx, fx.updater.install())


def test_an_older_native_signature_cannot_stand_in_for_a_newer_version(tmp_path):
    fx, _ = fixture(
        tmp_path, trusted=b"timestamp:1\tfile:myBurnRate_9.0.0_x64-setup.exe"
    )
    _assert_no_install(fx, fx.updater.install())
    fx2, _ = fixture(
        tmp_path / "ver",
        trusted=f"timestamp:1\tfile:{nu.installer_name(NEW_VERSION)}\tversion:9.0.0".encode(),
    )
    _assert_no_install(fx2, fx2.updater.install())


def test_signature_without_a_file_field_is_refused(tmp_path):
    fx, _ = fixture(tmp_path, trusted=b"timestamp:1")
    _assert_no_install(fx, fx.updater.install())


def test_signature_with_matching_file_and_version_installs(tmp_path):
    fx, _ = fixture(
        tmp_path,
        trusted=f"timestamp:1\tfile:{nu.installer_name(NEW_VERSION)}\tversion:{NEW_VERSION}".encode(),
    )
    assert fx.updater.install()["ok"] is True


@pytest.mark.parametrize(
    "url",
    [
        f"{GH}/download/v{NEW_VERSION}/BURNRATE_{NEW_VERSION}_x64-setup.exe",  # the Tauri installer
        f"{GH}/download/v{NEW_VERSION}/myBurnRate_9.0.0_x64-setup.exe",  # an older native build
        f"{GH}/download/v9.0.0/myBurnRate_{NEW_VERSION}_x64-setup.exe",  # tag mismatch
        f"{GH}/download/{NEW_VERSION}/myBurnRate_{NEW_VERSION}_x64-setup.exe",  # tag without v
        f"{GH}/download/v{NEW_VERSION}/sub/myBurnRate_{NEW_VERSION}_x64-setup.exe",
        f"{GH}/download/v{NEW_VERSION}/myBurnRate_{NEW_VERSION}_x64-setup.exe.sig",
        f"{GH}/download/v{NEW_VERSION}/burnrate-native_{NEW_VERSION}_x64-setup.exe",
        f"{GH}/download/v{NEW_VERSION}/myBurnRate_{NEW_VERSION}_x64-setup.exe/",
    ],
)
def test_feed_url_must_name_this_versions_native_installer(url):
    _, feed = make_feed(entry={"url": url})
    with pytest.raises(nu.FeedError):
        parse(feed)


def test_feed_url_for_a_prerelease_version_round_trips():
    _, feed = make_feed(version="0.6.0-beta.2")
    assert parse(feed).asset.url.endswith("myBurnRate_0.6.0-beta.2_x64-setup.exe")
    assert (
        nu.installer_name("v0.6.0-beta.2+build.7")
        == "myBurnRate_0.6.0-beta.2_x64-setup.exe"
    )


# ---------------------------------------------------------------------------------------
# the write lock must not stop the verified file from launching
# ---------------------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing semantics")
def test_lock_blocks_tampering_but_not_launching(tmp_path):
    import os
    import shutil
    import subprocess

    source = (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    )
    target = tmp_path / "harmless.exe"
    shutil.copy2(source, target)
    with nu._deny_writes(target):
        with pytest.raises(PermissionError):
            target.open("r+b").close()
        with pytest.raises(PermissionError):
            target.unlink()
        with pytest.raises(PermissionError):
            target.rename(tmp_path / "renamed.exe")
        started = subprocess.run(
            [str(target)], capture_output=True, check=False, timeout=30
        )
        assert started.returncode == 0 and started.stdout.strip()
        assert target.read_bytes() == source.read_bytes()
    target.rename(tmp_path / "renamed.exe")  # released afterwards


# ---------------------------------------------------------------------------------------
# packaging: the native feed, the signing helper, the build script and the NSIS script
# ---------------------------------------------------------------------------------------


def _load(name: str):
    import importlib.util

    path = ROOT / "packaging" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _signed_release_dir(tmp_path, version="0.6.0"):
    """A release folder holding the Tauri installer and the native installer, each with a .sig."""
    tauri = tmp_path / f"BURNRATE_{version}_x64-setup.exe"
    native = tmp_path / nu.installer_name(version)
    for file in (tauri, native):
        file.write_bytes(b"MZ" + file.name.encode())
        trusted = f"timestamp:1\tfile:{file.name}".encode()
        _, sig = minisign(KEY_SEED, file.read_bytes(), trusted=trusted)
        file.with_name(file.name + ".sig").write_text(sig, encoding="utf-8")
    return tauri, native


def test_installer_name_is_defined_identically_in_both_places():
    feed = _load("make_latest_json")
    for version in ("0.6.0", "v0.6.0", "0.6.0-beta.2", "1.0.0+build.5"):
        assert feed.native_installer_name(version) == nu.installer_name(version)


def test_native_installer_never_enters_the_tauri_feed(tmp_path):
    feed = _load("make_latest_json")
    tauri, native = _signed_release_dir(tmp_path)
    latest = feed.build_feed(tmp_path, tag="v0.6.0", repo="o/r")
    assert latest["platforms"]["windows-x86_64"]["url"].endswith(tauri.name)
    assert native.name not in json.dumps(latest)
    manifests = _load("render_manifests")
    assert (
        manifests.installer(tmp_path).name == tauri.name
    )  # still exactly one installer
    native.unlink()
    native.with_name(native.name + ".sig").unlink()
    again = feed.build_feed(tmp_path, tag="v0.6.0", repo="o/r")
    assert again["platforms"] == latest["platforms"]


def test_native_feed_describes_the_installer_and_is_parseable_by_the_app(tmp_path):
    feed = _load("make_latest_json")
    _, native = _signed_release_dir(tmp_path)
    document = feed.build_native_feed(
        tmp_path, tag="v0.6.0", repo="kamol-nazarov/myBurnRate", notes="Hi"
    )
    entry = document["platforms"]["windows-x86_64"]
    assert set(document) == {"version", "notes", "pub_date", "platforms"}
    assert set(entry) == {"url", "signature", "sha256", "size"}
    assert entry["sha256"] == hashlib.sha256(native.read_bytes()).hexdigest()
    assert entry["size"] == native.stat().st_size
    assert entry["url"] == f"{GH}/download/v0.6.0/{native.name}"
    parsed = nu.parse_feed(json.dumps(document).encode())  # what the app will accept
    assert parsed.version == "0.6.0" and parsed.asset.url == entry["url"]
    assert list(document["platforms"]) == ["windows-x86_64"]


def test_native_feed_refuses_a_signature_for_another_name(tmp_path):
    feed = _load("make_latest_json")
    tauri, native = _signed_release_dir(tmp_path)
    sig = native.with_name(native.name + ".sig")
    sig.write_text(
        tauri.with_name(tauri.name + ".sig").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="final name"):
        feed.build_native_feed(tmp_path, tag="v0.6.0", repo="o/r")
    with pytest.raises(SystemExit):
        feed.check_native_signature(native)
    sig.unlink()
    with pytest.raises(SystemExit, match="no update signature"):
        feed.build_native_feed(tmp_path, tag="v0.6.0", repo="o/r")
    native.unlink()
    with pytest.raises(SystemExit, match="No native installer"):
        feed.build_native_feed(tmp_path, tag="v0.6.0", repo="o/r")


def test_ci_signature_check_reads_the_real_tauri_style_comment():
    feed = _load("make_latest_json")
    assert feed.trusted_comment_fields(REAL_SIG_V053) == {
        "timestamp": "1793236810",
        "file": "BURNRATE_0.5.3_x64-setup.exe",
        "version": "0.5.3",
    }
    for bad in ("", "###", b64(b"only one line")):
        with pytest.raises(SystemExit):
            feed.trusted_comment_fields(bad)


# --- signing helper ----------------------------------------------------------------------


class FakeRun:
    """Stands in for subprocess.run; ``script(argv)`` may return (returncode, stdout)."""

    def __init__(self, script=None):
        self.calls = []
        self.script = script

    def __call__(self, argv, **kwargs):
        import subprocess

        self.calls.append((list(argv), kwargs.get("env") or {}))
        code, out = 0, ""
        if "ConvertTo-SecureString" in " ".join(argv):
            out = "A" * 40 + " 1"
        if self.script is not None:
            code, out = self.script(argv, len(self.calls), (code, out))
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


def _signer(tmp_path, env, run=None, **kwargs):
    sw = _load("sign_windows")
    tool = tmp_path / "signtool.exe"
    tool.write_text("x", encoding="utf-8")
    lines = []
    run = run or FakeRun()
    signer = sw.Signer(
        {"BURNRATE_SIGNTOOL": str(tool), **env},
        runner=run,
        sleep=lambda s: None,
        log=lines.append,
        **kwargs,
    )
    return sw, signer, run, lines


def _target(tmp_path):
    path = tmp_path / "BURNRATE.exe"
    path.write_bytes(b"MZ")
    return path


def test_unconfigured_build_is_unsigned_with_one_clear_line(tmp_path):
    sw, signer, run, lines = _signer(tmp_path, {})
    path = _target(tmp_path)
    assert signer.sign(path) is False and signer.sign(path) is False
    assert lines == [sw.UNSIGNED_NOTICE] and run.calls == []
    assert "UNSIGNED" in lines[0]


def test_require_signature_fails_when_nothing_is_configured(tmp_path):
    sw, signer, run, _ = _signer(tmp_path, {}, require=True)
    with pytest.raises(sw.SigningError, match="require-signature"):
        signer.check_ready()
    with pytest.raises(sw.SigningError):
        signer.sign(_target(tmp_path))
    assert run.calls == []


def test_thumbprint_signing_uses_sha256_timestamp_and_verifies(tmp_path):
    thumb = "ab" * 20
    sw, signer, run, lines = _signer(tmp_path, {"BURNRATE_SIGN_THUMBPRINT": thumb})
    assert signer.sign(_target(tmp_path)) is True
    sign_args, verify_args = run.calls[0][0], run.calls[1][0]
    assert sign_args[1] == "sign" and sign_args[sign_args.index("/fd") + 1] == "SHA256"
    assert sign_args[sign_args.index("/td") + 1] == "SHA256"
    assert sign_args[sign_args.index("/tr") + 1] == sw.DEFAULT_TIMESTAMP
    assert sign_args[sign_args.index("/sha1") + 1] == thumb and "/sm" not in sign_args
    assert verify_args[1:3] == ["verify", "/pa"]
    assert any("signed and verified" in line for line in lines)


def test_machine_store_and_custom_timestamp_server(tmp_path):
    env = {
        "BURNRATE_SIGN_THUMBPRINT": "cd" * 20,
        "BURNRATE_SIGN_MACHINE_STORE": "1",
        "BURNRATE_SIGN_TIMESTAMP_URL": "http://ts.example/rfc3161",
    }
    _, signer, run, _ = _signer(tmp_path, env)
    signer.sign(_target(tmp_path))
    args = run.calls[0][0]
    assert "/sm" in args and args[args.index("/tr") + 1] == "http://ts.example/rfc3161"


def test_azure_trusted_signing_passes_the_dlib_and_metadata(tmp_path):
    dlib, meta = tmp_path / "Azure.CodeSigning.Dlib.dll", tmp_path / "metadata.json"
    dlib.write_text("x", encoding="utf-8")
    meta.write_text("{}", encoding="utf-8")
    env = {
        "BURNRATE_SIGN_AZURE_DLIB": str(dlib),
        "BURNRATE_SIGN_AZURE_METADATA": str(meta),
    }
    sw, signer, run, _ = _signer(tmp_path, env)
    signer.sign(_target(tmp_path))
    args = run.calls[0][0]
    assert args[args.index("/dlib") + 1] == str(dlib) and args[
        args.index("/dmdf") + 1
    ] == str(meta)
    assert args[args.index("/tr") + 1] == sw.AZURE_TIMESTAMP and "/sha1" not in args


def test_pfx_password_never_reaches_a_command_line_or_the_log(tmp_path):
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")
    secret = "s3cr3t-Passw0rd!"
    env = {"BURNRATE_SIGN_PFX": str(pfx), "BURNRATE_SIGN_PFX_PASSWORD": secret}
    _sw, signer, run, lines = _signer(tmp_path, env)
    assert signer.sign(_target(tmp_path)) is True
    for argv, _env in run.calls:
        assert secret not in " ".join(argv)
    assert secret not in "\n".join(lines)
    imports = [e for argv, e in run.calls if "ConvertTo-SecureString" in " ".join(argv)]
    assert (
        imports and imports[0]["BURNRATE_SIGN_PFX_PASSWORD"] == secret
    )  # via the environment only
    sign_call = next(argv for argv, _ in run.calls if "/fd" in argv)
    assert sign_call[sign_call.index("/sha1") + 1] == "A" * 40
    assert any(
        "Remove-Item" in " ".join(argv) for argv, _ in run.calls
    )  # the imported cert is removed


def test_signing_failures_are_retried_then_reported_without_secrets(tmp_path):
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")
    secret = "hunter2-hunter2"

    def script(argv, _n, default):
        if "/fd" in argv:  # the signtool sign call
            return 1, f"timestamp server said {secret}"
        return default

    env = {"BURNRATE_SIGN_PFX": str(pfx), "BURNRATE_SIGN_PFX_PASSWORD": secret}
    sw, signer, run, _ = _signer(tmp_path, env, run=FakeRun(script))
    with pytest.raises(sw.SigningError) as raised:
        signer.sign(_target(tmp_path))
    assert secret not in str(raised.value) and "***" in str(raised.value)
    assert sum(1 for argv, _ in run.calls if "/fd" in argv) == 3


def test_a_flaky_timestamp_server_is_retried(tmp_path):
    def script(argv, n, default):
        return (1, "busy") if "/fd" in argv and n <= 2 else default

    _, signer, _, _ = _signer(
        tmp_path, {"BURNRATE_SIGN_THUMBPRINT": "ef" * 20}, run=FakeRun(script)
    )
    assert signer.sign(_target(tmp_path)) is True


def test_failed_verification_is_an_error(tmp_path):
    def script(argv, _n, default):
        return (1, "No signature found") if "verify" in argv else default

    sw, signer, _, _ = _signer(
        tmp_path, {"BURNRATE_SIGN_THUMBPRINT": "12" * 20}, run=FakeRun(script)
    )
    with pytest.raises(sw.SigningError, match="no valid Authenticode signature"):
        signer.sign(_target(tmp_path))
    with pytest.raises(sw.SigningError):
        signer.require_signed([_target(tmp_path)])


@pytest.mark.parametrize(
    "env",
    [
        {"BURNRATE_SIGN_THUMBPRINT": "nothex"},
        {"BURNRATE_SIGN_PFX": "missing.pfx", "BURNRATE_SIGN_PFX_PASSWORD": "x"},
        {
            "BURNRATE_SIGN_AZURE_DLIB": "missing.dll",
            "BURNRATE_SIGN_AZURE_METADATA": "missing.json",
        },
        {"BURNRATE_SIGN_THUMBPRINT": "ab" * 20, "BURNRATE_SIGN_PFX": "x.pfx"},
    ],
)
def test_bad_signing_configuration_is_rejected_before_building(tmp_path, env):
    sw, signer, _, _ = _signer(tmp_path, env)
    with pytest.raises(sw.SigningError):
        signer.check_ready()


def test_pfx_needs_a_password(tmp_path):
    pfx = tmp_path / "cert.pfx"
    pfx.write_bytes(b"pfx")
    sw, signer, _, _ = _signer(tmp_path, {"BURNRATE_SIGN_PFX": str(pfx)})
    with pytest.raises(sw.SigningError, match="PASSWORD"):
        signer.check_ready()


# --- build script ------------------------------------------------------------------------


def test_build_script_helpers(tmp_path, monkeypatch):
    build = _load("build_native")
    assert build.numeric_version("0.5.3") == "0.5.3.0"
    assert build.numeric_version("0.6.0-beta.2") == "0.6.0.0"
    with pytest.raises(SystemExit):
        build.numeric_version("garbage")
    tool = tmp_path / "makensis.exe"
    tool.write_text("x", encoding="utf-8")
    assert build.find_makensis(
        {"BURNRATE_MAKENSIS": str(tool)}, which=lambda n: None
    ) == str(tool)
    assert (
        build.find_makensis({"BURNRATE_MAKENSIS": "nope"}, which=lambda n: "elsewhere")
        is None
    )
    assert (
        build.find_makensis({}, which=lambda n: "C:/nsis/makensis.exe")
        == "C:/nsis/makensis.exe"
    )
    assert (
        build.find_makensis({"ProgramFiles(x86)": str(tmp_path)}, which=lambda n: None)
        is None
    )

    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv
        out = next(a for a in argv if a.startswith("/DOUTFILE=")).split("=", 1)[1]
        Path(out).write_bytes(b"installer")

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    out = tmp_path / "out" / "x-setup.exe"
    build.build_installer("makensis", tmp_path, "0.5.3", out)
    argv = seen["argv"]
    assert "/DVERSION=0.5.3" in argv and "/DVI_VERSION=0.5.3.0" in argv
    assert (
        argv[-1].endswith("burnrate-native.nsi") and f"/DSOURCE_DIR={tmp_path}" in argv
    )
    with pytest.raises(SystemExit):
        build.build_installer("makensis", tmp_path, "0.5.3 /DEVIL", out)


def test_nsis_script_keeps_program_files_and_user_data_apart():
    text = (ROOT / "packaging" / "burnrate-native.nsi").read_text(encoding="utf-8")
    assert "RequestExecutionLevel user" in text  # per-user: no UAC
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\BURNRATE"' in text
    assert "Uninstall\\BURNRATENative" in text  # not the Tauri app's key
    assert "/S" in text and "/UPDATE" in text and "BURNRATE.installed" in text
    assert 'RMDir /r "$INSTDIR"' not in text  # never wipes a chosen folder wholesale
    install = text[text.index('Section "Install"') : text.index("SectionEnd")]
    for line in install.splitlines():
        if line.strip().startswith(
            ("File", "Delete", "RMDir", "FileOpen", "CreateDirectory", "SetOutPath")
        ):
            assert "$LOCALAPPDATA\\BURNRATE" not in line, line
    uninstall = text[text.index('Section "Uninstall"') :]
    gate = uninstall.index("${If} $DeleteData == ${BST_CHECKED}")
    for line in uninstall.splitlines():
        if "$LOCALAPPDATA\\BURNRATE" in line and not line.strip().startswith(";"):
            assert uninstall.index(line) > gate, (
                line
            )  # user data is touched only behind the checkbox
    assert (
        "${NSD_CreateCheckbox}" in text and "NSD_SetState" not in text
    )  # unchecked by default
    assert "SetCompressor" in text and "Unicode true" in text


def test_release_verifier_requires_the_pinned_key_and_a_matching_name(
    tmp_path, monkeypatch
):
    verify = _load("verify_native_release")
    tauri, native = _signed_release_dir(tmp_path)
    with pytest.raises(
        nu.SignatureError
    ):  # right name, but not signed with the pinned key
        verify.main([str(native)])
    pub = minisign(KEY_SEED, b"")[0]
    original = nu.verify_minisign_file
    monkeypatch.setattr(
        nu,
        "verify_minisign_file",
        lambda path, sig, **kw: original(path, sig, pub, **kw),
    )
    assert verify.main([str(native)]) == 0
    # a signature made for another file name is refused even if the key is right
    sig = native.with_name(native.name + ".sig")
    sig.write_text(
        tauri.with_name(tauri.name + ".sig").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        verify.main([str(native)])
    assert verify.main([]) == 2


def test_nsis_update_gives_the_window_a_grace_period_before_the_force_kill():
    text = (ROOT / "packaging" / "burnrate-native.nsi").read_text(encoding="utf-8")
    install = text[text.index('Section "Install"') : text.index("SectionEnd")]
    wait = install.index("Call WaitForWindowExit")
    kill = install.index("Call StopBurnrate")
    gate = install.index("${If} ${Silent}")
    assert (
        gate < install.index('"/UPDATE"') < wait < kill
    )  # /S /UPDATE only, and before the kill
    uninstall = text[text.index('Section "Uninstall"') :]
    assert "WaitForWindowExit" not in uninstall and "Call un.StopBurnrate" in uninstall
    assert (
        "${If} $R7 < 12" in text and "Sleep 250" in text
    )  # about three seconds in all


# ---------------------------------------------------------------------------------------
# the myBurnRate_ name: security-bound, and it shares its ending with the Tauri installer
# ---------------------------------------------------------------------------------------


def test_installer_name_is_one_definition_and_disjoint_from_the_tauri_installer():
    import fnmatch

    assert nu.installer_name("0.6.0") == "myBurnRate_0.6.0_x64-setup.exe"
    assert (
        nu.INSTALLER_PREFIX == "myBurnRate_" and nu.INSTALLER_SUFFIX == "_x64-setup.exe"
    )
    assert _load("make_latest_json").NATIVE_PREFIX == nu.INSTALLER_PREFIX
    tauri = "BURNRATE_0.6.0_x64-setup.exe"
    # the two names do match the same glob, which is exactly why the prefixes are excluded
    assert fnmatch.fnmatch(nu.installer_name("0.6.0"), "*_x64-setup.exe")
    assert fnmatch.fnmatch(tauri, "*_x64-setup.exe")
    assert not tauri.casefold().startswith(nu.INSTALLER_PREFIX.casefold())
    assert not nu.installer_name("0.6.0").casefold().startswith("burnrate_")


def test_tauri_feed_and_manifests_take_only_the_tauri_installer_in_both_directions(
    tmp_path,
):
    feed = _load("make_latest_json")
    manifests = _load("render_manifests")
    tauri, native = _signed_release_dir(tmp_path)
    assert (tauri.name, native.name) == (
        "BURNRATE_0.6.0_x64-setup.exe",
        "myBurnRate_0.6.0_x64-setup.exe",
    )
    latest = feed.build_feed(tmp_path, tag="v0.6.0", repo="o/r")
    assert list(latest["platforms"]) == ["windows-x86_64"]
    assert latest["platforms"]["windows-x86_64"]["url"].endswith(
        "/BURNRATE_0.6.0_x64-setup.exe"
    )
    assert manifests.installer(tmp_path).name == tauri.name
    native_feed = feed.build_native_feed(tmp_path, tag="v0.6.0", repo="o/r")
    assert native_feed["platforms"]["windows-x86_64"]["url"].endswith(
        "/myBurnRate_0.6.0_x64-setup.exe"
    )
    assert "BURNRATE_0.6.0" not in json.dumps(native_feed)


def test_the_tauri_installers_signature_is_rejected_for_the_native_name(tmp_path):
    fx, _ = fixture(
        tmp_path,
        trusted=f"timestamp:1\tfile:BURNRATE_{NEW_VERSION}_x64-setup.exe".encode(),
    )
    _assert_no_install(fx, fx.updater.install())
    legacy = f"timestamp:1\tfile:BURNRATE-native_{NEW_VERSION}_x64-setup.exe".encode()
    fx2, _ = fixture(tmp_path / "legacy", trusted=legacy)
    _assert_no_install(fx2, fx2.updater.install())
    ok, _ = fixture(
        tmp_path / "ok",
        trusted=f"timestamp:1\tfile:myBurnRate_{NEW_VERSION}_x64-setup.exe".encode(),
    )
    assert ok.updater.install()["ok"] is True


@pytest.mark.parametrize(
    "name",
    [
        "BURNRATE_9.9.9_x64-setup.exe",  # the Tauri installer
        "BURNRATE-native_9.9.9_x64-setup.exe",  # the pre-rename native name
        "myburnrate_9.9.9_x64-setup.exe",
    ],
)
def test_feed_url_with_another_products_installer_name_is_refused(name):
    _, feed = make_feed(entry={"url": f"{GH}/download/v{NEW_VERSION}/{name}"})
    with pytest.raises(nu.FeedError):
        parse(feed)


def test_old_dev_installer_leftovers_are_cleaned_but_never_accepted(tmp_path):
    fx, _ = fixture(tmp_path)
    fx.updates.mkdir(parents=True)
    for name in (
        "BURNRATE-native_0.1.0_x64-setup.exe",
        "BURNRATE-native_0.2.0_x64-setup.exe.part",
        "myBurnRate_0.3.0_x64-setup.exe",
    ):
        (fx.updates / name).write_bytes(b"old")
    (fx.updates / "BURNRATE_0.5.3_x64-setup.exe").write_bytes(b"not ours")
    assert fx.updater.install()["ok"] is True
    assert fx.leftovers() == [
        "BURNRATE_0.5.3_x64-setup.exe",
        nu.installer_name(NEW_VERSION),
    ]


def test_nsis_presents_as_myburnrate_but_keeps_the_internal_ids():
    text = (ROOT / "packaging" / "burnrate-native.nsi").read_text(encoding="utf-8")
    assert (
        '!define APP_NAME "myBurnRate"' in text
        and '!define SHORTCUT_NAME "myBurnRate"' in text
    )
    assert 'DisplayName" "${APP_NAME}"' in text and "(native app)" not in text
    for kept in (
        '!define EXE_NAME "BURNRATE.exe"',
        '!define MARKER_NAME "BURNRATE.installed"',
        'InstallDir "$LOCALAPPDATA\\Programs\\BURNRATE"',
        "Uninstall\\BURNRATENative",
        '!define SETTINGS_KEY "Software\\BURNRATE Native"',
    ):
        assert kept in text, kept
    # a leftover dev-build shortcut is removed on install and uninstall, never created
    assert text.count('Delete "$SMPROGRAMS\\${LEGACY_SHORTCUT_NAME}.lnk"') == 2
    assert text.count('Delete "$DESKTOP\\${LEGACY_SHORTCUT_NAME}.lnk"') == 2
    assert (
        "CreateShortCut" in text and 'CreateShortCut "$SMPROGRAMS\\${LEGACY' not in text
    )
