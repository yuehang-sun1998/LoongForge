import numpy as np
from loongforge.embodied.eval.servers.loongforge_policy import GenericPredictActionPolicy


def test_chunk_metadata_does_not_change_actions():
    class Model:
        def predict_action(self, images, instructions, state=None, dataset_stats=None, **kwargs):
            full = np.arange(224, dtype=np.float32).reshape(1, 32, 7)
            if kwargs.get("capture_full_chunk"):
                self._jev_full_chunk = full.copy()
            return full[:, :10]

    policy = GenericPredictActionPolicy(Model(), {})
    for i in range(12):
        result = policy.predict_action([], ["test"], include_chunk_context=True)
        context = result["chunk_context"]
        assert context["current_index_zero_based"] == i % 10
        assert context["remaining_including_current"] == 10 - i % 10
        assert context["full_model_chunk"].shape == (32, 7)
        np.testing.assert_array_equal(result["actions"][0], np.arange(7) + (i % 10) * 7)
    policy.reset("default")
    assert not policy._jev_chunks
