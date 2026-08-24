import bot.dedup as dedup


async def test_first_sighting_is_not_duplicate(db_pool):
    assert await dedup.is_duplicate(db_pool, 12345) is False


async def test_second_sighting_is_duplicate(db_pool):
    await dedup.is_duplicate(db_pool, 12345)

    assert await dedup.is_duplicate(db_pool, 12345) is True
