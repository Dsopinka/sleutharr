"""Guards on the one string that names a release.

`core/context.py::VERSION` is the source of truth for the image tag, the git tag and the
version the UI reports. Registries are much less forgiving about that string than Python
is, and the failure arrives late -- after a full build, from a registry, in wording that
does not mention what is actually wrong. These run in a tenth of a second instead.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.test import TestCase

from core.context import VERSION

REPO = Path(__file__).resolve().parent.parent.parent


class VersionIsAUsableDockerTag(TestCase):
    def test_it_is_plain_semver(self):
        self.assertRegex(
            VERSION,
            r"^\d+\.\d+\.\d+$",
            "VERSION must be X.Y.Z: the workflow derives the image tags from it, and "
            "anything else either fails the push or sorts wrongly in the registry.",
        )

    def test_it_is_a_legal_docker_tag(self):
        """Docker's own rule, which is stricter than it looks."""
        self.assertLessEqual(len(VERSION), 128)
        self.assertRegex(VERSION, r"^[a-zA-Z0-9_][a-zA-Z0-9._-]*$")

    def test_no_leading_v(self):
        """The git tag is `vX.Y.Z`; the image tag is `X.Y.Z`.

        Putting the v in both would publish `v2.11.1` while every doc, compose file and
        Unraid template asks for `2.11.1`.
        """
        self.assertFalse(VERSION.startswith("v"))


class TheWorkflowAndTheAppAgree(TestCase):
    """The release protocol only holds if CI reads the file the app reads."""

    def setUp(self):
        self.workflow = (REPO / ".github/workflows/docker.yml").read_text()

    def test_the_workflow_reads_the_version_from_context(self):
        self.assertIn("core/context.py", self.workflow)

    def test_the_regex_the_workflow_uses_actually_matches(self):
        """The workflow greps VERSION out of the source; prove that grep still works.

        Renaming or re-indenting the assignment would leave a workflow that fails at
        release time on a repository whose tests all pass.
        """
        source = (REPO / "core/context.py").read_text()
        found = re.search(r'^VERSION\s*=\s*"([^"]+)"', source, re.M)
        self.assertIsNotNone(found, "the workflow's VERSION lookup would find nothing")
        self.assertEqual(found.group(1), VERSION)

    def test_it_refuses_to_publish_to_one_registry_by_accident(self):
        """The divergence guard is the point of the protocol; keep it wired in."""
        self.assertIn("allow_single_registry", self.workflow)
        self.assertIn("Manifest.Digest", self.workflow)
