from services.agent_gateway_v3.app.services.turn_assembler import TurnAssembler


def test_turn_assembler_deduplicates_text_and_preserves_raw_usage() -> None:
    assembler = TurnAssembler()
    assembler.add_text("hello")
    assembler.add_text(" world")
    assembler.add_text("hello world")
    raw_usage = {
        "provider": "openai_agents_api",
        "raw_turn_usage_reports": [{"turn_id": "turn-1", "usage": None}],
    }
    assembler.add_event({"type": "middleware.usage.raw", "usage": raw_usage})

    assert assembler.reply_text() == "hello world"
    assert assembler.usage is raw_usage


def test_turn_assembler_appends_overlapping_deltas_once() -> None:
    assembler = TurnAssembler()
    assert assembler.add_text("Good morning") == "Good morning"
    assert assembler.add_text("morning, everyone") == ", everyone"
    assert assembler.reply_text() == "Good morning, everyone"
