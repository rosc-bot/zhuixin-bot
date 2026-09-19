import pytest

from services.library_service import LibraryService


@pytest.mark.asyncio
async def test_non_contiguous_collected_episodes_are_not_marked_complete():
    result = await LibraryService.evaluate_item_status(
        {
            "media_type": "TV",
            "season": 1,
            "episodes": [1, 3],
            "total_episodes": 3,
            "tmdb_id": None,
        },
        target_episodes=[],
        follow_mode="FULL",
    )

    assert result["action_type"] == "scout"
    assert result["missing_episodes"] == [2]


@pytest.mark.asyncio
async def test_contiguous_collected_episodes_are_marked_complete():
    result = await LibraryService.evaluate_item_status(
        {
            "media_type": "TV",
            "season": 1,
            "episodes": [1, 2, 3],
            "total_episodes": 3,
            "tmdb_id": None,
        },
        target_episodes=[],
        follow_mode="FULL",
    )

    assert result["action_type"] == "completed"
    assert result["missing_episodes"] == []
