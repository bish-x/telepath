from telepath.features.base import FeatureRegistry


class FakeFeature:
    name = "fake"

    def can_handle(self, event):
        return event == "match"

    async def handle(self, event, context):
        context.append(event)
        return "handled"


async def test_feature_registry_routes_to_first_matching_feature():
    context = []
    registry = FeatureRegistry([FakeFeature()])

    assert await registry.dispatch("match", context) == "handled"
    assert context == ["match"]
    assert await registry.dispatch("miss", context) is None
