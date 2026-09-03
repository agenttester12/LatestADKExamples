"""Control tools observed by the AskHR WxO run manager."""

from ibm_watsonx_orchestrate.agent_builder.tools import tool


@tool
async def stage_card(card: dict) -> dict:
    """Stage a complete AskHR ChatBlock card from a tool result.

    Pass the returned card unchanged. AskHR observes this call in the WxO run
    stream, validates the card, and delivers it after the run completes.

    Args:
        card: Complete ChatBlock with a stable ``cardId``.
    Returns:
        Acknowledgement containing the observed card ID.
    """
    card_id = card.get("cardId") if isinstance(card, dict) else None
    return {"ok": True, "cardId": card_id}


@tool
async def report_action(action_type: str) -> dict:
    """Report one successfully completed AskHR transaction.

    Args:
        action_type: Exact machine-readable action type returned by the business tool.
    Returns:
        Acknowledgement; AskHR records the call from the WxO run stream.
    """
    return {"ok": True}
