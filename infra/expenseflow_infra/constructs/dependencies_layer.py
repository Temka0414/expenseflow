"""DependenciesLayer: third-party Python packages for the Lambdas, built WITHOUT Docker when possible."""

from __future__ import annotations

import hashlib
import logging
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Final

import jsii
from aws_cdk import AssetHashType, BundlingOptions, DockerImage, ILocalBundling, RemovalPolicy
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from expenseflow_infra import paths

_LOG = logging.getLogger(__name__)

TARGET_PLATFORM: Final = "manylinux2014_aarch64"
TARGET_PYTHON: Final = "3.13"


def pip_install_args(requirements: str, target: str) -> list[str]:
    """Arguments for `pip` that install Lambda (Linux arm64, CPython 3.13) wheels on any machine.

    pip normally picks wheels for the computer it runs on (e.g. Windows x86_64). These flags make it
    resolve for the Lambda runtime instead, so packages with native code (pydantic-core, jiter)
    get their `aarch64-linux-gnu` binaries:

    * `--platform/--implementation/--python-version` describe the target interpreter;
    * `--only-binary=:all:` is required by those flags: an sdist would be *built* for the local
      machine, so a package without a matching wheel fails loudly instead of shipping broken code;
    * `--no-compile` skips `.pyc` files, which would be compiled by the local Python version.

    Gotcha: environment markers such as `sys_platform == "win32"` in a requirement are still
    evaluated against the machine running pip, so keep requirements.txt free of OS markers.
    """
    return [
        "install",
        "--requirement",
        requirements,
        "--target",
        target,
        "--platform",
        TARGET_PLATFORM,
        "--implementation",
        "cp",
        "--python-version",
        TARGET_PYTHON,
        "--only-binary=:all:",
        "--no-compile",
        "--upgrade",
        "--disable-pip-version-check",
    ]


@jsii.implements(ILocalBundling)
class LocalPipBundler:
    """Builds the layer with the Python running `cdk synth` instead of a Docker container.

    CDK's asset bundling runs a command inside a Docker image by default. A construct can offer a
    *local* bundler first: CDK calls `try_bundle`, and only if it returns False does CDK fall back to
    Docker. Most developer machines (and GitHub runners) have pip but not necessarily Docker.
    """

    def __init__(self, requirements: Path) -> None:
        self._requirements = requirements

    def try_bundle(self, output_dir: str, *, image: DockerImage, **_: object) -> bool:
        """Install the requirements into `<output_dir>/python`, the folder Lambda adds to sys.path.

        jsii passes only the `BundlingOptions` fields declared as keyword-only parameters (`image`,
        used in the fallback log message); `**_` exists so the method matches the full
        `ILocalBundling.try_bundle` signature for type checkers.
        """
        target = Path(output_dir) / "python"
        command = [sys.executable, "-m", "pip", *pip_install_args(str(self._requirements), str(target))]
        try:
            subprocess.run(command, check=True)
        except (OSError, subprocess.CalledProcessError) as error:
            _LOG.warning("Local layer bundling failed (%s); CDK will try Docker (%s).", error, image.image)
            return False
        return True


class DependenciesLayer(lambda_.LayerVersion):
    """Lambda layer with the packages from `backend/requirements.txt` for Python 3.13 on arm64.

    Keeping dependencies in a layer keeps each function's own code package tiny (fast deploys, code
    visible in the console) and lets all functions share one copy of pydantic, Powertools, etc.

    The asset hash is computed from requirements.txt plus the pip target settings, not from the
    folder contents. Editing Lambda code therefore never re-runs pip or publishes a new layer
    version; changing a pinned version does. CDK skips bundling when `cdk.out/asset.<hash>` already
    exists, so delete `cdk.out` to force a rebuild.

    Gotcha (Windows): the deepest file of the layer (an `anthropic/types/beta/...` module) sits
    about 190 characters below `cdk.out`. Keep the repository path short (or enable Win32 long
    paths) so the 260-character MAX_PATH limit is never reached.
    """

    def __init__(self, scope: Construct, construct_id: str, *, requirements: Path | None = None) -> None:
        """Create the layer.

        Args:
            requirements: requirements.txt to install; defaults to `backend/requirements.txt`.
        """
        requirements = requirements or paths.BACKEND_REQUIREMENTS
        docker_command = shlex.join(
            ["pip", *pip_install_args(f"/asset-input/{requirements.name}", "/asset-output/python")]
        )
        super().__init__(
            scope,
            construct_id,
            code=lambda_.Code.from_asset(
                str(requirements.parent),
                asset_hash_type=AssetHashType.CUSTOM,
                asset_hash=_requirements_hash(requirements),
                bundling=BundlingOptions(
                    # Fallback when local bundling fails: AWS SAM's build image for the runtime.
                    image=lambda_.Runtime.PYTHON_3_13.bundling_image,
                    command=["bash", "-c", docker_command],
                    local=LocalPipBundler(requirements),
                ),
            ),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            compatible_architectures=[lambda_.Architecture.ARM_64],
            description=f"Python dependencies from {requirements.name} (Python {TARGET_PYTHON}, arm64)",
            # Functions keep working after an old layer version is deleted (Lambda keeps its own
            # copy), so superseded versions need not pile up.
            removal_policy=RemovalPolicy.DESTROY,
        )


def _requirements_hash(requirements: Path) -> str:
    # Normalise line endings so a Windows checkout (CRLF) and CI on Linux (LF) produce the same
    # hash, and therefore the same asset: otherwise every machine would publish its own layer version.
    content = requirements.read_text(encoding="utf-8").replace("\r\n", "\n")
    digest = hashlib.sha256(content.encode())
    digest.update(f"{TARGET_PLATFORM}/cp{TARGET_PYTHON}".encode())
    return digest.hexdigest()
