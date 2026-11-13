"""Mock IT slot availability for local tests. It returns only the slots it was given; UC-04 never invents a slot."""

from sub_agent.ports import AdapterError, SlotOffer


class MockSlotAvailability:
    real_side_effects = False

    def __init__(self, slots: list[SlotOffer] | None = None, fail_with: AdapterError | None = None):
        self.slots = list(slots or [])
        self.fail_with = fail_with
        self.calls: list[str] = []

    def available_slots(self, *, cohort_ref: str) -> list[SlotOffer]:
        self.calls.append(cohort_ref)
        if self.fail_with is not None:
            raise self.fail_with
        return [s.model_copy() for s in self.slots]
