"""Delivery coordinator for unified monthly funding workflow cards.

Resolves per-chat adoption precedence, pins audience safely, and synchronizes
rendered workflow cards through the lifecycle manager.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from maestro.integrations.telegram.ui.card_state import CardCopy, card_adoption_event
from maestro.integrations.telegram.ui.cards import render_funding_workflow_card
from maestro.integrations.telegram.ui.funding_workflow import (
    FundingWorkflowCardModel,
    funding_workflow_card_key,
)

if TYPE_CHECKING:
    from maestro.integrations.telegram.ui.lifecycle import CardLifecycleManager
    from maestro.state.store import StateStore

WorkflowCardSyncOutcome = Literal[
    "sent", "edited", "skipped", "failed", "unknown", "blocked"
]


@dataclass(frozen=True)
class FundingWorkflowCardSyncResult:
    card_key: str
    outcomes: Mapping[int, WorkflowCardSyncOutcome]

    def outcome_for(self, chat_id: int) -> WorkflowCardSyncOutcome:
        return self.outcomes.get(chat_id, "blocked")


def _request_card_key(request_id: str, phase: str) -> str:
    if phase == "budget":
        return f"budget-request:{request_id}"
    return f"funding-request:{request_id}"


class FundingWorkflowCardDelivery:
    def __init__(self, store: StateStore, lifecycle: CardLifecycleManager) -> None:
        self.store = store
        self.lifecycle = lifecycle

    def sync(
        self,
        run_id: str,
        model: FundingWorkflowCardModel,
    ) -> FundingWorkflowCardSyncResult:
        """Synchronize the workflow card to all configured chats respecting adoption precedence."""
        target_card_key = funding_workflow_card_key(model.workflow_id)
        configured_chats = tuple(self.lifecycle.chat_ids)

        target_state_rows = self.store.load_card_delivery_state(target_card_key)
        target_copies: dict[int, CardCopy] = {
            row["chat_id"]: CardCopy(**row) for row in target_state_rows
        }

        # 1. Resolve per-chat adoption precedence
        for chat_id in configured_chats:
            # 1.1 Existing target workflow copy wins
            if chat_id in target_copies:
                continue

            # 1.2 Inspect current request's request-scoped copy
            curr_ref = model.lineage[0] if model.lineage else None
            adopted = False
            if curr_ref is not None:
                curr_card_key = _request_card_key(curr_ref.request_id, curr_ref.phase)
                curr_rows = self.store.load_card_delivery_state(curr_card_key)
                curr_copies = {row["chat_id"]: CardCopy(**row) for row in curr_rows}
                if chat_id in curr_copies:
                    source_copy = curr_copies[chat_id]
                    # 1.3 Adopt current confirmed, unknown, or failed evidence unchanged
                    adopt_ev = card_adoption_event(
                        target_card_key,
                        chat_id,
                        source=source_copy,
                        source_request_id=curr_ref.request_id,
                        source_phase=curr_ref.phase,
                    )
                    self.store.record_card_event(run_id, adopt_ev)
                    target_copies[chat_id] = CardCopy(
                        card_key=target_card_key,
                        chat_id=chat_id,
                        message_id=source_copy.message_id,
                        stage=source_copy.stage,
                        render_hash=source_copy.render_hash,
                        delivery=source_copy.delivery,
                        operation_id=f"adopt:{source_copy.operation_id}",
                    )
                    adopted = True

            if adopted:
                continue

            # 1.4 Only when current evidence is absent, scan lineage[1:] for nearest confirmed predecessor
            if model.lineage and len(model.lineage) > 1:
                for pred_ref in model.lineage[1:]:
                    pred_card_key = _request_card_key(pred_ref.request_id, pred_ref.phase)
                    pred_rows = self.store.load_card_delivery_state(pred_card_key)
                    pred_copies = {row["chat_id"]: CardCopy(**row) for row in pred_rows}
                    if chat_id in pred_copies:
                        pred_copy = pred_copies[chat_id]
                        if pred_copy.delivery == "confirmed":
                            adopt_ev = card_adoption_event(
                                target_card_key,
                                chat_id,
                                source=pred_copy,
                                source_request_id=pred_ref.request_id,
                                source_phase=pred_ref.phase,
                            )
                            self.store.record_card_event(run_id, adopt_ev)
                            target_copies[chat_id] = CardCopy(
                                card_key=target_card_key,
                                chat_id=chat_id,
                                message_id=pred_copy.message_id,
                                stage=pred_copy.stage,
                                render_hash=pred_copy.render_hash,
                                delivery="confirmed",
                                operation_id=f"adopt:{pred_copy.operation_id}",
                            )
                            break
                        # Predecessor unknown or failed copies are ignored

        # 2. Pin audience and determine safe chat subset
        recorded_audience = self.store.load_card_audience(target_card_key)
        if not recorded_audience:
            if model.card_delivery_version == 1:
                self.store.record_card_audience(target_card_key, configured_chats)
                safe_chat_subset = list(configured_chats)
            else:
                # Generation 0: only chats where target copy was adopted or already existed
                target_chats = [cid for cid in configured_chats if cid in target_copies]
                if target_chats:
                    self.store.record_card_audience(target_card_key, target_chats)
                safe_chat_subset = target_chats
        else:
            safe_chat_subset = [cid for cid in configured_chats if cid in set(recorded_audience)]

        # 3. Refresh safe chats through lifecycle manager
        if safe_chat_subset:
            stage = model.lifecycle_stage
            rendered = render_funding_workflow_card(model)
            refresh_res = self.lifecycle.refresh(
                run_id,
                target_card_key,
                stage,
                rendered,
                chat_ids=safe_chat_subset,
                edit_replacement_policy="replace_on_target_absence",
            )
        else:
            refresh_res = {
                "edited": (),
                "skipped": (),
                "sent": (),
                "failed": (),
                "ambiguous": (),
            }

        # 4. Map outcomes
        outcomes: dict[int, WorkflowCardSyncOutcome] = {}
        for cid in configured_chats:
            if cid not in safe_chat_subset:
                outcomes[cid] = "blocked"
            elif cid in refresh_res.get("sent", ()):
                outcomes[cid] = "sent"
            elif cid in refresh_res.get("edited", ()):
                outcomes[cid] = "edited"
            elif cid in refresh_res.get("skipped", ()):
                outcomes[cid] = "skipped"
            elif cid in refresh_res.get("failed", ()):
                outcomes[cid] = "failed"
            elif cid in refresh_res.get("ambiguous", ()):
                outcomes[cid] = "unknown"
            else:
                outcomes[cid] = "blocked"

        return FundingWorkflowCardSyncResult(card_key=target_card_key, outcomes=outcomes)
