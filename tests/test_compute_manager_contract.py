"""Consumer-side contract tests for the Compute Manager integration (checkpoint C0).

The canonical contracts live in ``edx-compute-manager/contracts``. This repository keeps a read-only, hash-locked
snapshot of the fixtures it needs (``tests/contracts/compute-manager``) and replays them through the REAL
``Schema2ManifestParser``, ``ReadinessCallbackClient``, ``Provisioner`` and ``RemoteBootstrap._verify_hardware``,
so the verdicts recorded in the contract (what the Runtime accepts and rejects) are measured, not assumed.

Standard library only, offline, nothing is installed, started or downloaded. Run from the repository root:
``python -B -m unittest discover -s tests -p test_compute_manager_contract.py``.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from installer.provisioning import Provisioner, ProvisioningError
from installer.remote_bootstrap import (
    IDENTIFIER,
    ManifestValidationError,
    ReadinessCallbackClient,
    RemoteBootstrap,
    ReadinessError,
    RemoteManifest,
    Schema2ManifestParser,
    _require_identifier,
)

SNAPSHOT = Path(__file__).resolve().parent / "contracts" / "compute-manager"
SIBLING = Path(__file__).resolve().parents[2] / "infra" / "edx-compute-manager" / "contracts"
MANIFESTS = SNAPSHOT / "fixtures" / "bootstrap-manifest"
READINESS = SNAPSHOT / "fixtures" / "readiness" / "request"
SECRET_WORDS = ("token", "secret", "password", "authorization", "credential", "bearer")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


class SnapshotTests(unittest.TestCase):
    def test_snapshot_is_exactly_what_its_lock_says(self):
        lock = load(SNAPSHOT / "SOURCE.lock.json")
        self.assertEqual(lock["consumer"], "runtime")
        self.assertEqual(lock["contract_version"], 1)
        on_disk = {
            p.relative_to(SNAPSHOT).as_posix()
            for p in SNAPSHOT.rglob("*") if p.is_file() and p.name != "SOURCE.lock.json"
        }
        self.assertEqual(on_disk, set(lock["files"]))
        for relative, expected in lock["files"].items():
            self.assertEqual(digest(SNAPSHOT / relative), expected, relative)

    def test_snapshot_is_not_stale_when_checked_out_next_to_the_compute_manager(self):
        if not SIBLING.is_dir():
            self.skipTest("edx-compute-manager is not checked out next to this repository")
        lock = load(SNAPSHOT / "SOURCE.lock.json")
        for relative, expected in lock["files"].items():
            self.assertEqual(
                digest(SIBLING / relative), expected,
                f"{relative} changed in the canonical contracts: regenerate the snapshot (contracts/tools/vendor.py)",
            )


class ManifestContractTests(unittest.TestCase):
    """The Runtime is a tolerant reader: the contract records, per fixture, what it must accept or reject."""

    def parse(self, payload, environment):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bootstrap.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return Schema2ManifestParser(environment).load(path)

    def test_manifest_fixtures_replayed_through_the_real_parser(self):
        spec = load(MANIFESTS / "expectations.json")
        environment = dict(spec["runtime_environment"])
        self.assertGreaterEqual(len(spec["cases"]), 13)
        for case in spec["cases"]:
            payload = load(MANIFESTS / case["file"])
            with self.subTest(case["file"]):
                if case["runtime_parser"] == "accept":
                    manifest = self.parse(payload, environment)
                    self.assertIsInstance(manifest, RemoteManifest)
                    self.assertTrue(manifest.capabilities)
                else:
                    with self.assertRaises(ManifestValidationError):
                        self.parse(payload, environment)

    def test_canonical_manifest_is_parsed_to_the_documented_values(self):
        manifest = self.parse(load(MANIFESTS / "valid" / "canonical.json"), {"EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": "https://compute.example"})
        self.assertEqual(manifest.instance_id, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(manifest.provider_instance_id, "5000")
        self.assertEqual(manifest.capabilities, ("minimax-h3-fast",))
        self.assertTrue(manifest.readiness_url.endswith("/api/v1/internal/instances/11111111-1111-4111-8111-111111111111/readiness"))

    def test_tolerant_reader_cases_normalise_instead_of_failing(self):
        environment = {"EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": "https://compute.example"}
        upper = self.parse(load(MANIFESTS / "invalid" / "capability-uppercase.json"), environment)
        self.assertEqual(upper.capabilities, ("minimax-h3-fast",))
        integer = self.parse(load(MANIFESTS / "invalid" / "provider-id-integer.json"), environment)
        self.assertEqual(integer.provider_instance_id, "5000")
        extra = self.parse(load(MANIFESTS / "invalid" / "extra-top-level-key.json"), environment)
        self.assertEqual(extra.instance_id, "11111111-1111-4111-8111-111111111111")

    def test_known_incompatibility_i1_empty_capabilities_is_refused(self):
        """I-1. The Compute Manager can emit this for a request without required_capabilities (its own
        tests/test_contracts.py pins that side). The Runtime refuses it, so such an instance never confirms
        readiness. When the Compute Manager stops emitting it this test stays true."""
        payload = load(MANIFESTS / "invalid" / "empty-capabilities.json")
        with self.assertRaisesRegex(ManifestValidationError, "capabilities"):
            self.parse(payload, {"EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": "https://compute.example"})

    def test_wrong_instance_callback_is_refused_by_the_cross_field_rule(self):
        payload = load(MANIFESTS / "invalid" / "callback-wrong-instance.json")
        with self.assertRaisesRegex(ManifestValidationError, "instance_id"):
            self.parse(payload, {"EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": "https://compute.example"})

    def test_an_untrusted_origin_is_refused_even_for_a_valid_manifest(self):
        payload = load(MANIFESTS / "valid" / "canonical.json")
        with self.assertRaisesRegex(ManifestValidationError, "nao autorizada"):
            self.parse(payload, {"EDX_COMPUTE_MANAGER_TRUSTED_ORIGINS": "https://elsewhere.example"})

    def test_no_valid_manifest_fixture_carries_anything_credential_shaped(self):
        """Valid manifests never hold a credential. (Some invalid fixtures mention one on purpose, e.g. a rejected
        ``?token=`` query, so only ``valid/`` is scanned.)"""
        paths = sorted((MANIFESTS / "valid").glob("*.json"))
        self.assertTrue(paths)
        for path in paths:
            text = json.dumps(load(path)).lower()
            for word in SECRET_WORDS:
                self.assertNotIn(word, text, f"{path.name} contains {word!r}")


class ReadinessContractTests(unittest.TestCase):
    def test_the_runtime_sends_exactly_the_contract_body(self):
        spec = load(READINESS / "expectations.json")
        sent = []

        class Response:
            status = 200

            def read(self, _limit=-1):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        def opener(request, timeout):
            sent.append(request)
            return Response()

        manifest = RemoteManifest(
            "11111111-1111-4111-8111-111111111111", "5000", ("minimax",),
            "https://compute.example/api/v1/internal/instances/11111111-1111-4111-8111-111111111111/readiness", "a" * 64,
        )
        ReadinessCallbackClient(opener=opener, sleeper=lambda _s: None).send(manifest, "bearer-for-test")
        expected = load(READINESS / spec["runtime_sends"])
        self.assertEqual(json.loads(sent[0].data), expected)
        self.assertEqual(sent[0].get_method(), "POST")
        self.assertEqual(sent[0].get_header("Content-type"), "application/json")
        self.assertNotIn(b"bearer-for-test", sent[0].data)  # the credential travels only in the header

    def test_contract_valid_provider_ids_are_accepted_by_the_runtime_grammar(self):
        for case in load(READINESS / "expectations.json")["cases"]:
            if case["contract"] == "valid":
                value = load(READINESS / case["file"])["provider_instance_id"]
                with self.subTest(case["file"]):
                    self.assertEqual(_require_identifier(value, "provider_instance_id"), value)

    def test_identifier_grammars_agree_except_for_the_documented_tolerances(self):
        """The contract grammar is the intersection of the Compute Manager's and the Runtime's (I-4). Anything the
        contract rejects must be rejected here too, except these two known, harmless tolerances."""
        samples = load(SNAPSHOT / "fixtures" / "identifiers" / "ids.json")

        def runtime_accepts(value, lower=False):
            try:
                _require_identifier(value, "x")
                return True
            except ManifestValidationError:
                return False

        for value in samples["providerInstanceId"]["valid"] + samples["capabilityId"]["valid"]:
            self.assertTrue(runtime_accepts(value), value)
        self.assertEqual(
            {v for v in samples["providerInstanceId"]["invalid"] if runtime_accepts(v)}, {"x" * 65},
            "provider id: the Runtime tolerates up to 128 characters, the contract 64 (I-4)",
        )
        self.assertEqual(
            {v for v in samples["capabilityId"]["invalid"] if runtime_accepts(v)}, {"MiniMax", "x" * 65},
            "capability id: the Runtime lower-cases and allows 128 characters",
        )
        self.assertTrue(IDENTIFIER.fullmatch("a"))


class CapabilityAndHardwareContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provisioner = Provisioner()
        cls.ids = load(SNAPSHOT / "fixtures" / "integration" / "capability-ids.json")

    def test_every_control_plane_capability_id_resolves_in_the_runtime(self):
        for capability in self.ids["canonical_control_plane_ids"] + self.ids["runtime_resolvable_extra_examples"]:
            with self.subTest(capability):
                profile = self.provisioner.resolve_profile(capability)
                self.assertIn(profile, self.provisioner.profiles)

    def test_known_incompatibility_i2_minimax_h3_does_not_resolve(self):
        for capability in self.ids["runtime_unresolvable_examples"]:
            with self.subTest(capability), self.assertRaises(ProvisioningError):
                self.provisioner.resolve_profile(capability)

    def test_known_incompatibility_i3_vram_boundary_is_strict(self):
        """I-3. A profile that needs 12 GiB demands vram_total >= 12288 MiB as reported by ComfyUI. The Compute Manager
        measures VRAM in Vast gigabytes (MiB / 1000), so a 12 GiB card is 12.288 there and passes its filter. Whether such a
        card actually reports >= 12288 MiB is HYPOTHESIS I-3 and needs a real GPU; this test pins only the exact boundary of
        the real check so nobody relaxes or tightens it unknowingly."""
        profile = self.provisioner.resolve_profile("minimax")
        requirements = self.provisioner.profile_requirements(profile)
        self.assertEqual(requirements["min_vram_mb"], 12 * 1024)
        mib = 1024 * 1024
        RemoteBootstrap._verify_hardware(
            self.provisioner, [profile], {"devices": [{"vram_total": 12 * 1024 * mib}]}, "nvidia")
        with self.assertRaisesRegex(ReadinessError, "VRAM"):
            RemoteBootstrap._verify_hardware(
                self.provisioner, [profile], {"devices": [{"vram_total": 12 * 1024 * mib - 1}]}, "nvidia")

    def test_the_declared_krea2_minimum_is_below_the_control_plane_seed(self):
        """The Control Plane seeds krea2 at 16384 MB; the Runtime profile declares 6 GiB minimum (commit 9ff5e4d). The
        seed is conservative, not wrong; this pins the Runtime side of that divergence."""
        requirements = self.provisioner.profile_requirements(self.provisioner.resolve_profile("krea2"))
        self.assertEqual(requirements["min_vram_mb"], 6 * 1024)


class EnvDeliveryContractTests(unittest.TestCase):
    """CM5 contract replayed through the REAL environment materialiser (RT1): what the Compute Manager sets in the instance environment
    becomes exactly the schema 2 manifest and a private credential file, or is refused with nothing written. The accept/reject verdicts and
    the expected manifests are the ones recorded in the contract (fixtures/bootstrap-env), not assumed here."""

    ENV = SNAPSHOT / "fixtures" / "bootstrap-env"

    def run_case(self, case, root):
        environment = {**load(self.ENV / case["file"]), **case["provider_environment"]}
        manifest, token = root / "workspace" / ".edx" / "bootstrap.json", root / "run" / "secrets" / "token"
        return environment, manifest, token

    def test_every_fixture_is_accepted_or_refused_as_the_contract_records(self):
        from installer.remote_env import materialize
        from installer.remote_bootstrap import RemoteBootstrapError

        spec = load(self.ENV / "expectations.json")
        self.assertGreaterEqual(len(spec["cases"]), 19)
        self.assertEqual(spec["runtime_provider_variables"], {"vast": "CONTAINER_ID"})
        for case in spec["cases"]:
            with self.subTest(case["file"]), tempfile.TemporaryDirectory() as tmp:
                environment, manifest_path, token_path = self.run_case(case, Path(tmp))
                if case["runtime"] == "accept":
                    materialize(environment, manifest_path, token_path)
                    parsed = Schema2ManifestParser(environment).load(manifest_path)
                    expected = case["manifest"]
                    self.assertEqual(parsed.instance_id, expected["instance_id"])
                    self.assertEqual(parsed.provider_instance_id, expected["provider_instance_id"])
                    self.assertEqual(list(parsed.capabilities), [c.lower() for c in expected["capabilities"]])
                    self.assertEqual(parsed.readiness_url, expected["callback"]["readiness_url"])
                    self.assertEqual(token_path.read_text(encoding="utf-8"), environment["EDX_BOOTSTRAP_READINESS_TOKEN"])
                    self.assertNotIn(environment["EDX_BOOTSTRAP_READINESS_TOKEN"], manifest_path.read_text(encoding="utf-8"))
                else:
                    with self.assertRaises(RemoteBootstrapError) as raised:
                        materialize(environment, manifest_path, token_path)
                    token = environment.get("EDX_BOOTSTRAP_READINESS_TOKEN")
                    if token:
                        self.assertNotIn(token, str(raised.exception))  # names variables, never values
                    self.assertEqual([p for p in Path(tmp).rglob("*") if p.is_file()], [], "a refused delivery writes nothing")

    def test_the_snapshot_never_ships_the_administrative_token_in_a_valid_fixture(self):
        spec = load(self.ENV / "expectations.json")
        for case in spec["cases"]:
            if case["contract"] == "valid":
                self.assertNotIn("EDX_COMPUTE_MANAGER_SERVICE_TOKEN", load(self.ENV / case["file"]), case["file"])

    def test_the_contract_manifest_equals_the_canonical_manifest_fixture_shape(self):
        canonical = load(SNAPSHOT / "fixtures" / "bootstrap-manifest" / "valid" / "canonical.json")
        spec = load(self.ENV / "expectations.json")
        generated = next(c for c in spec["cases"] if c["file"] == "valid/canonical.json")["manifest"]
        self.assertEqual(set(generated), set(canonical))
        self.assertEqual(set(generated["callback"]), set(canonical["callback"]))


if __name__ == "__main__":
    unittest.main()
