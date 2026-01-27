from __future__ import annotations

from typing import Protocol, Any


class ExecutionBroker(Protocol):
    """
    Trading actions and account state.
    """

    def get_account(self) -> Any:
        ...

    def list_open_orders(self) -> list[Any]:
        ...

    def list_positions(self) -> list[Any]:
        ...

    def submit_order(self, order_request: Any) -> Any:
        ...

    def cancel_order(self, order_id: str) -> None:
        ...
