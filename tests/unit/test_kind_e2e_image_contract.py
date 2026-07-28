from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import kind_e2e  # noqa: E402
import kind_runtime  # noqa: E402


IMAGE_TAG = "git-" + ("a" * 40)
APPLICATION_IMAGES = tuple(
    f"{name}:{IMAGE_TAG}"
    for name in ("hooklane-api", "hooklane-worker", "hooklane-mock-sink")
)


def capture_helm(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        kind_runtime,
        "helm",
        lambda *arguments, **_kwargs: calls.append(arguments),
    )
    return calls


def assert_image_overrides(arguments: tuple[str, ...]) -> None:
    assert arguments[-6:] == kind_runtime.image_overrides(IMAGE_TAG)
    assert arguments[-6:] == (
        "--set-string",
        f"api.image.tag={IMAGE_TAG}",
        "--set-string",
        f"worker.image.tag={IMAGE_TAG}",
        "--set-string",
        f"mockSink.image.tag={IMAGE_TAG}",
    )
    assert "0.1.1" not in arguments


def test_deploy_e2e_release_passes_all_current_image_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_helm(monkeypatch)
    monkeypatch.setattr(kind_e2e, "wait_all_workloads", lambda: None)
    monkeypatch.setattr(kind_e2e, "verify_application_image_tags", lambda _tag: None)

    kind_e2e.deploy_e2e_release(IMAGE_TAG)

    assert len(calls) == 1
    assert_image_overrides(calls[0])


def test_configure_sink_helm_passes_all_current_image_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_helm(monkeypatch)
    monkeypatch.setattr(kind_e2e, "wait_rollout", lambda _resource: None)
    monkeypatch.setattr(kind_e2e, "wait_for_mock_sink_rollout", lambda: None)
    monkeypatch.setattr(kind_e2e, "verify_application_image_tags", lambda _tag: None)

    kind_e2e.configure_sink_helm("server_error", 0, IMAGE_TAG)

    assert len(calls) == 1
    assert_image_overrides(calls[0])


def test_restore_normal_release_passes_all_current_image_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = capture_helm(monkeypatch)
    monkeypatch.setattr(kind_e2e, "wait_all_workloads", lambda: None)
    monkeypatch.setattr(kind_e2e, "wait_for_mock_sink_rollout", lambda: None)
    monkeypatch.setattr(kind_e2e, "verify_application_image_tags", lambda _tag: None)

    kind_e2e.restore_normal_release(IMAGE_TAG)

    assert len(calls) == 1
    assert_image_overrides(calls[0])


def test_e2e_helm_argument_builder_keeps_one_tag_for_every_application_image() -> None:
    arguments = kind_e2e.e2e_helm_arguments(IMAGE_TAG, "upgrade", "hooklane")

    assert arguments[:2] == ("upgrade", "hooklane")
    assert_image_overrides(arguments)


@pytest.mark.parametrize("invalid_tag", ("latest", "", "git-abc123"))
def test_kind_image_tag_contract_rejects_mutable_empty_and_short_tags(invalid_tag: str) -> None:
    with pytest.raises(ValueError):
        kind_runtime.resolve_image_tag(invalid_tag)


def test_release_baseline_remains_valid_without_being_used_by_e2e_overrides() -> None:
    assert kind_runtime.resolve_image_tag("0.1.1") == "0.1.1"
    assert "0.1.1" not in kind_e2e.e2e_helm_arguments(IMAGE_TAG, "upgrade")


def test_kind_loads_only_the_current_application_images_without_docker_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[list[str]] = []
    monkeypatch.setattr(kind_runtime, "require_cluster", lambda: None)

    def fake_run(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        loaded.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(kind_runtime, "run", fake_run)

    kind_runtime.load_images(IMAGE_TAG)

    assert [command[3] for command in loaded] == list(APPLICATION_IMAGES)
    assert all("0.1.1" not in command for command in loaded)


def test_current_tag_does_not_require_release_baseline_image() -> None:
    assert kind_runtime.application_images(IMAGE_TAG) == APPLICATION_IMAGES
    assert all(image.endswith(IMAGE_TAG) for image in APPLICATION_IMAGES)


def test_all_deployment_revisions_use_the_same_current_image_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_by_deployment = {
        "hooklane-api": "hooklane-api:" + IMAGE_TAG,
        "hooklane-worker": "hooklane-worker:" + IMAGE_TAG,
        "hooklane-mock-sink": "hooklane-mock-sink:" + IMAGE_TAG,
    }

    def resource(image: str) -> dict[str, object]:
        return {
            "spec": {
                "template": {
                    "spec": {"containers": [{"image": image}]},
                },
            },
        }

    def fake_kubectl_json(*arguments: str) -> dict[str, object]:
        if "deployment" in arguments:
            deployment_name = arguments[-1]
            return resource(expected_by_deployment[deployment_name])
        component = arguments[-1].rsplit("=", maxsplit=1)[-1]
        image = expected_by_deployment[f"hooklane-{component}"]
        return {"items": [resource(image), resource(image)]}

    monkeypatch.setattr(kind_e2e, "kubectl_json", fake_kubectl_json)

    kind_e2e.verify_application_image_tags(IMAGE_TAG)


def test_deployment_revision_fallback_to_release_baseline_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_kubectl_json(*arguments: str) -> dict[str, object]:
        if "deployment" in arguments:
            return {
                "spec": {
                    "template": {
                        "spec": {"containers": [{"image": "hooklane-api:0.1.1"}]},
                    },
                },
            }
        return {"items": []}

    monkeypatch.setattr(kind_e2e, "kubectl_json", fake_kubectl_json)

    with pytest.raises(RuntimeError, match="expected hooklane-api:" + IMAGE_TAG):
        kind_e2e.verify_application_image_tags(IMAGE_TAG)
