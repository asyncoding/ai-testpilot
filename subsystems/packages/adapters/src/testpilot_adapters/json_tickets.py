"""JSON 工单适配器（TicketPort）——不是写死的假数据，而是真实端口实现。

读：fixtures/tickets/*.json（生产替代：Jira/Redmine/禅道）；
写：落到运行时数据目录 store/ticket-store/*.json，请求经策略层守卫与幂等键防重；
未来换 JiraAdapter 时工作流层不改代码（docs/08 FR-CON-004）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from testpilot_core.enums import ActionLevel
from testpilot_core.errors import AdapterUnavailableError
from testpilot_core.models import Approval, CommentDraft, Issue, IssueDraft
from testpilot_core.policy import assert_writable
from testpilot_core.urn import make_urn

_SOURCE_SYSTEM = "json-tickets"


class JsonTicketAdapter:
    def __init__(
        self,
        read_dirs: list[Path],
        *,
        write_dir: Path,
        approval_provider: Callable[[str], Approval | None] | None = None,
        kill_switch: Callable[[], bool] | None = None,
        source_system: str = _SOURCE_SYSTEM,
    ) -> None:
        self.read_dirs = read_dirs
        self.write_dir = write_dir
        self.approval_provider = approval_provider
        self._kill_switch = kill_switch
        self.source_system = source_system
        self.write_dir.mkdir(parents=True, exist_ok=True)

    def _approval(self, approval_id: str) -> Approval | None:
        if self.approval_provider is None:
            return None
        return self.approval_provider(approval_id)

    async def search(self, query: str, *, project: str, limit: int = 10) -> list[Issue]:
        issues: list[Issue] = []
        for d in self.read_dirs:
            if not d.exists():
                raise AdapterUnavailableError(f"工单夹具目录不存在: {d}")
            for path in sorted(d.glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                fields = {k: v for k, v in data.items() if k in Issue.model_fields}
                if fields.get("version") is not None:
                    fields["version"] = str(fields["version"])
                issues.append(
                    Issue(
                        urn=make_urn(self.source_system, "issue", fields["external_id"], fields.get("version")),
                        **fields,
                    )
                )
        q = query.strip().lower()
        if q:
            issues = [
                i
                for i in issues
                if q in i.title.lower() or q in i.symptom.lower() or q in " ".join(i.repro_steps).lower()
            ]
        return issues[:limit]

    async def create(
        self, draft: IssueDraft, idempotency_key: str, approval_id: str
    ) -> str:
        assert_writable(
            ActionLevel.R3_APPROVED_EXTERNAL,
            self._approval(approval_id),
            global_kill_switch=self._kill_switch() if self._kill_switch else False,
        )
        path = self.write_dir / f"{idempotency_key}.json"
        if path.exists():  # 幂等：重复键不产生第二条
            return json.loads(path.read_text(encoding="utf-8"))["urn"]
        external_id = f"DRAFT-{abs(hash(idempotency_key)) % 100000:05d}"
        urn = make_urn(self.source_system, "issue", external_id, "1")
        record = {
            "urn": urn,
            "external_id": external_id,
            "title": draft.title,
            "status": "open",
            "severity": "",
            "symptom": draft.symptom,
            "repro_steps": draft.repro_steps,
            "idempotency_key": idempotency_key,
            "approval_id": approval_id,
            "evidence_urns": draft.evidence_urns,
            "run_id": draft.run_id,
            "uncovered_layers": draft.uncovered_layers,
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return urn

    async def add_comment(
        self, issue_ref: str, comment: CommentDraft, idempotency_key: str, approval_id: str
    ) -> str:
        assert_writable(
            ActionLevel.R2_REVIEWED_WRITE,
            self._approval(approval_id),
            global_kill_switch=self._kill_switch() if self._kill_switch else False,
        )
        path = self.write_dir / f"comment-{idempotency_key}.json"
        if path.exists():
            return json.loads(path.read_text())["urn"]
        urn = make_urn(self.source_system, "comment", idempotency_key, "1")
        path.write_text(
            json.dumps(
                {"urn": urn, "issue_ref": issue_ref, "body": comment.body, "run_id": comment.run_id},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return urn

    async def transition(
        self, issue_ref: str, transition: str, idempotency_key: str, approval_id: str
    ) -> None:
        assert_writable(
            ActionLevel.R3_APPROVED_EXTERNAL,
            self._approval(approval_id),
            global_kill_switch=self._kill_switch() if self._kill_switch else False,
        )
        path = self.write_dir / f"transition-{idempotency_key}.json"
        if path.exists():
            return
        path.write_text(
            json.dumps({"issue_ref": issue_ref, "transition": transition}, ensure_ascii=False),
            encoding="utf-8",
        )
