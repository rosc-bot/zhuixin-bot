"""Regression tests preventing duplicate service implementations from drifting."""


def test_legacy_library_module_reexports_canonical_service():
    import library_service
    from services.library_service import LibraryService

    assert library_service.LibraryService is LibraryService


def test_legacy_cloud_inventory_module_reexports_canonical_service():
    import cloud_inventory_service
    from services.cloud_inventory_service import CloudInventoryService

    assert cloud_inventory_service.CloudInventoryService is CloudInventoryService


def test_legacy_watchlist_helpers_reexport_canonical_functions():
    import watchlist_incremental_logic as legacy
    from services import watchlist_incremental_logic as canonical

    assert legacy.canonical_episode_keys is canonical.canonical_episode_keys
    assert legacy.title_scoped_text is canonical.title_scoped_text
    assert legacy.title_scoped_urls is canonical.title_scoped_urls
