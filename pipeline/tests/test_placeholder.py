from hazier import config


def test_placeholder():
    assert len(config.ZONES) == 8
    assert config.ZONES[0].id == config.DEFAULT_ZONE_ID
