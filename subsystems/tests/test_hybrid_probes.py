"""hybrid_probes hermetic 测试（docs/01 §4，H3）。

零真机：FakeDhAx/FakeDevicectl 脚本化探针证据，钉死——
① 三态语义（satisfied/unsatisfied/unknown）与能力圈边界（两个恒 unknown）；
② repair 后有界复探，转不回竖屏按 on_failure 姿态收口，绝不无限转；
③ human_gate/purge 启动参数两条特殊分流；缺 pid 的 terminate fail-loud。
"""

from __future__ import annotations

import struct

import pytest

from testpilot_adapters.devicectl_control import DevicectlControlError
from testpilot_adapters.dh_ax import DhAxActionError
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.hybrid_plan import (
    HybridPlanError,
    PlanRepair,
    PlanStep,
    Precondition,
)
from testpilot_core.enums import StepActor
from testpilot_workers.hybrid_probes import (
    MAX_ROTATE_ATTEMPTS,
    ProbeContext,
    apply_repair,
    evaluate_preconditions,
)

_UDID = "00008140-001A39590162801C"
_BUNDLE = "com.example.AlarmKitSpike"
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _png_bytes(width: int, height: int) -> bytes:
    return (
        _PNG_SIG
        + struct.pack(">I4s", 13, b"IHDR")
        + struct.pack(">II", width, height)
        + b"\x00" * 5
    )


class FakeDhAx:
    def __init__(self, *, readiness=None, readiness_error=None,
                 rotate_error=None) -> None:
        self._readiness = readiness
        self._readiness_error = readiness_error
        self._rotate_error = rotate_error
        self.home_calls = 0
        self.rotate_calls: list[int] = []

    async def readiness(self) -> dict:
        if self._readiness_error is not None:
            raise self._readiness_error
        return dict(self._readiness)

    async def home(self) -> dict:
        self.home_calls += 1
        return {"ok": True, "verb": "home"}

    async def rotate_left(self, times: int = 1) -> dict:
        self.rotate_calls.append(times)
        if self._rotate_error is not None:
            raise self._rotate_error
        return {"ok": True, "verb": "rotate-left", "times": times}


class FakeDevicectl:
    def __init__(self, *, portrait: bool = True,
                 capture_error: Exception | None = None,
                 bad_png: bool = False) -> None:
        self._portrait = portrait
        self._capture_error = capture_error
        self._bad_png = bad_png
        self._root = None
        self.screenshots = 0
        self.terminate_calls: list[tuple[str, int, bool]] = []

    async def capture_screenshot(self, udid: str):
        self.screenshots += 1
        if self._capture_error is not None:
            raise self._capture_error
        path = self._root / f"shot-{self.screenshots}.png"
        if self._bad_png:
            path.write_bytes(b"not-a-png")
        else:
            w, h = (1206, 2622) if self._portrait else (2622, 1206)
            path.write_bytes(_png_bytes(w, h))
        return path

    async def terminate_pid(self, udid, pid, *, kill=False):
        self.terminate_calls.append((udid, pid, kill))

    async def launch_app(self, *args, **kwargs):
        return "{}"

    def bind_root(self, path):
        self._root = path
        path.mkdir(parents=True, exist_ok=True)


class _Box:
    """最小容器：对齐 factory.Container 的 get(name) 装配口径。"""

    def __init__(self, tmp_path, adapters):
        self.base_dir = tmp_path
        self._adapters = adapters

    def get(self, name):
        return self._adapters.get(name)


def _container(tmp_path, dh=None, dev=None) -> _Box:
    if dev is not None:
        dev.bind_root(tmp_path / "shots")
    return _Box(tmp_path, {"dh_ax": dh, "devicectl": dev})


def _ctx() -> ProbeContext:
    return ProbeContext(udid=_UDID, bundle_id=_BUNDLE)


def _good_readiness(*, locked: bool = False) -> dict:
    return {
        "ok": not locked, "verb": "readiness", "pid": 83446,
        "screen_locked": locked, "ax_trusted": True,
        "windows": [{"title": "DeviceHub", "width": 320, "height": 480}],
        "buttons": {"home": True, "screenshot": True, "rotate_left": True},
    }


def _step(preconditions: list[Precondition], *,
          actor=StepActor.AUTO_DH_AX, action="home") -> PlanStep:
    return PlanStep(
        step_id="s-x", actor=actor, action=action,
        preconditions=preconditions,
        idempotency_key="k-x",
        **({"prompt": "p"} if actor.value.startswith("human") else {}))


# ── 单探针三态 ────────────────────────────────────────────────────────────────


async def test_device_unlocked_three_states(tmp_path):
    from testpilot_workers.hybrid_probes import _probe_device_unlocked

    ok = await _probe_device_unlocked(
        _container(tmp_path, dh=FakeDhAx(readiness=_good_readiness())), _ctx())
    assert ok.status == "satisfied"

    locked = await _probe_device_unlocked(
        _container(tmp_path,
                   dh=FakeDhAx(readiness=_good_readiness(locked=True))), _ctx())
    assert locked.status == "unsatisfied"

    unavailable = await _probe_device_unlocked(
        _container(tmp_path, dh=FakeDhAx(
            readiness_error=AdapterUnavailableError("Mac locked (exit 6)"))),
        _ctx())
    assert unavailable.status == "unknown"

    dematerialized = await _probe_device_unlocked(
        _container(tmp_path, dh=FakeDhAx(
            readiness_error=DhAxActionError("exit 4 工具条不可达"))), _ctx())
    assert dematerialized.status == "unknown"  # E31：取不到锁屏态，不许猜


async def test_window_materialized_three_states(tmp_path):
    from testpilot_workers.hybrid_probes import _probe_window_materialized

    ok = await _probe_window_materialized(
        _container(tmp_path, dh=FakeDhAx(readiness=_good_readiness())), _ctx())
    assert ok.status == "satisfied"

    broken = await _probe_window_materialized(
        _container(tmp_path, dh=FakeDhAx(
            readiness_error=DhAxActionError("exit 4"))), _ctx())
    assert broken.status == "unsatisfied"

    unavailable = await _probe_window_materialized(
        _container(tmp_path, dh=FakeDhAx(
            readiness_error=AdapterUnavailableError("no process (exit 2)"))),
        _ctx())
    assert unavailable.status == "unknown"


async def test_portrait_probe_uses_pixels(tmp_path):
    from testpilot_workers.hybrid_probes import _probe_portrait

    portrait = await _probe_portrait(
        _container(tmp_path, dev=FakeDevicectl(portrait=True)), _ctx())
    assert portrait.status == "satisfied"

    landscape = await _probe_portrait(
        _container(tmp_path, dev=FakeDevicectl(portrait=False)), _ctx())
    assert landscape.status == "unsatisfied"

    capt_fail = await _probe_portrait(
        _container(tmp_path, dev=FakeDevicectl(
            capture_error=DevicectlControlError("exit 1"))), _ctx())
    assert capt_fail.status == "unknown"

    bad_png = await _probe_portrait(
        _container(tmp_path, dev=FakeDevicectl(bad_png=True)), _ctx())
    assert bad_png.status == "unknown"


async def test_host_side_unknowns_are_always_unknown(tmp_path):
    from testpilot_workers.hybrid_probes import (
        _probe_app_not_running,
        _probe_no_active_alarms,
        _probe_notification_permission,
    )

    container = _container(tmp_path)
    assert (await _probe_app_not_running(container, _ctx())).status == "unknown"
    assert (await _probe_no_active_alarms(container, _ctx())).status == "unknown"
    assert (await _probe_notification_permission(
        container, _ctx())).status == "unknown"


# ── repair 行为 ───────────────────────────────────────────────────────────────


async def test_rotate_repair_stops_as_soon_as_portrait(tmp_path):
    # 前两次横屏、第三次竖屏：应恰好转 3 次、截 3 张，不复探多余动作
    dev = FakeDevicectl(portrait=False)
    dh = FakeDhAx()
    container = _container(tmp_path, dh=dh, dev=dev)

    async def flaky_capture(udid):
        path = tmp_path / "shots" / f"shot-{dev.screenshots + 1}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        dev.screenshots += 1
        w, h = (1206, 2622) if dev.screenshots >= 3 else (2622, 1206)
        path.write_bytes(_png_bytes(w, h))
        return path

    dev.capture_screenshot = flaky_capture  # type: ignore[method-assign]
    outcome = await apply_repair(
        container, PlanRepair(verb="dh_rotate_left"), _ctx())
    assert outcome.ok is True
    assert dh.rotate_calls == [1, 1, 1]
    assert outcome.detail["attempts"] == 3


async def test_rotate_repair_bounded_when_never_portrait(tmp_path):
    dh = FakeDhAx()
    dev = FakeDevicectl(portrait=False)
    container = _container(tmp_path, dh=dh, dev=dev)
    outcome = await apply_repair(
        container, PlanRepair(verb="dh_rotate_left"), _ctx())
    assert outcome.ok is False
    assert outcome.detail["attempts"] == MAX_ROTATE_ATTEMPTS
    assert len(dh.rotate_calls) == MAX_ROTATE_ATTEMPTS


async def test_terminate_repair_requires_explicit_pid(tmp_path):
    container = _container(
        tmp_path, dh=FakeDhAx(), dev=FakeDevicectl())
    with pytest.raises(HybridPlanError, match="params.pid"):
        await apply_repair(
            container, PlanRepair(verb="devicectl_terminate"), _ctx())

    ok = await apply_repair(
        container, PlanRepair(verb="devicectl_terminate",
                              params={"pid": 4321, "kill": True}), _ctx())
    assert ok.ok is True
    assert container.get("devicectl").terminate_calls == [
        (_UDID, 4321, True)]


async def test_purge_args_repair_is_io_free(tmp_path):
    container = _container(tmp_path)
    outcome = await apply_repair(
        container, PlanRepair(verb="app_launch_purge_args"), _ctx())
    assert outcome.launch_arguments == ["--ak-purge-dates"]
    custom = await apply_repair(
        container,
        PlanRepair(verb="app_launch_purge_args",
                   params={"arguments": ["--ak-purge-dates", "--ak-reset"]}),
        _ctx())
    assert custom.launch_arguments == ["--ak-purge-dates", "--ak-reset"]
    with pytest.raises(HybridPlanError, match="非空字符串列表"):
        await apply_repair(
            container,
            PlanRepair(verb="app_launch_purge_args",
                       params={"arguments": []}), _ctx())


async def test_dh_home_repair(tmp_path):
    dh = FakeDhAx()
    container = _container(tmp_path, dh=dh)
    outcome = await apply_repair(
        container, PlanRepair(verb="dh_home"), _ctx())
    assert outcome.ok and dh.home_calls == 1


# ── 步骤级收口 ────────────────────────────────────────────────────────────────


async def test_all_satisfied_proceeds(tmp_path):
    container = _container(
        tmp_path, dh=FakeDhAx(readiness=_good_readiness()),
        dev=FakeDevicectl(portrait=True))
    resolution = await evaluate_preconditions(
        container,
        _step([Precondition(kind="device_unlocked"),
              Precondition(kind="portrait_orientation",
                           repair=PlanRepair(verb="dh_rotate_left"))]),
        _ctx())
    assert resolution.proceed is True
    assert resolution.blocked_reason is None
    # 已满足的前置不触发 repair：探针本身截 1 张，但 rotate 一次都不调
    assert container.get("devicectl").screenshots == 1
    assert container.get("dh_ax").rotate_calls == []


async def test_unsatisfied_repaired_then_reprobe_satisfied(tmp_path):
    # 初始横屏，rotate repair 后转竖屏
    dev = FakeDevicectl(portrait=False)
    container = _container(tmp_path, dh=FakeDhAx(), dev=dev)

    async def capture(udid):
        path = tmp_path / "shots" / f"shot-{dev.screenshots + 1}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        dev.screenshots += 1
        w, h = (1206, 2622) if dev.screenshots >= 2 else (2622, 1206)
        path.write_bytes(_png_bytes(w, h))
        return path

    dev.capture_screenshot = capture  # type: ignore[method-assign]
    resolution = await evaluate_preconditions(
        container,
        _step([Precondition(
            kind="portrait_orientation",
            repair=PlanRepair(verb="dh_rotate_left"))]),
        _ctx())
    assert resolution.proceed is True
    kinds = [(e.get("kind"), e.get("status", e.get("repair")))
             for e in resolution.audit]
    assert any("reprobe" in e for e in resolution.audit)
    assert ("portrait_orientation", "unsatisfied") in kinds


async def test_repair_failure_blocked_posture(tmp_path):
    container = _container(
        tmp_path, dh=FakeDhAx(), dev=FakeDevicectl(portrait=False))
    resolution = await evaluate_preconditions(
        container,
        _step([Precondition(
            kind="portrait_orientation", on_failure="blocked",
            repair=PlanRepair(verb="dh_rotate_left"))]),
        _ctx())
    assert resolution.proceed is False
    assert resolution.blocked_reason is not None
    assert "portrait_orientation" in resolution.blocked_reason


async def test_repair_failure_fail_loud_raises(tmp_path):
    container = _container(
        tmp_path, dh=FakeDhAx(), dev=FakeDevicectl(portrait=False))
    with pytest.raises(HybridPlanError, match="fail_loud"):
        await evaluate_preconditions(
            container,
            _step([Precondition(
                kind="portrait_orientation", on_failure="fail_loud",
                repair=PlanRepair(verb="dh_rotate_left"))]),
            _ctx())


async def test_purge_repair_collects_launch_arguments(tmp_path):
    container = _container(tmp_path)
    resolution = await evaluate_preconditions(
        container,
        _step([Precondition(
            kind="no_active_alarms",
            repair=PlanRepair(verb="app_launch_purge_args"))]),
        _ctx())
    assert resolution.proceed is True
    assert resolution.launch_arguments == ["--ak-purge-dates"]


async def test_human_gate_repair_returns_human_request(tmp_path):
    container = _container(tmp_path)
    resolution = await evaluate_preconditions(
        container,
        _step([Precondition(
            kind="notification_permission", on_failure="blocked",
            repair=PlanRepair(verb="human_gate"), note="请确认通知授权")]),
        _ctx())
    assert resolution.proceed is False
    assert resolution.human_request is not None
    assert resolution.human_request["kind"] == "notification_permission"


async def test_unknown_without_repair_blocked(tmp_path):
    container = _container(tmp_path)
    resolution = await evaluate_preconditions(
        container, _step([Precondition(
            kind="app_not_running", on_failure="blocked")]), _ctx())
    assert resolution.proceed is False
    assert resolution.blocked_reason is not None


async def test_missing_adapter_is_loud(tmp_path):
    container = _Box(tmp_path, {})
    with pytest.raises(HybridPlanError, match="devicectl 适配器未装配"):
        await evaluate_preconditions(
            container,
            _step([Precondition(kind="portrait_orientation")]), _ctx())
