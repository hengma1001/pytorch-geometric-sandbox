from pathlib import Path
from mdgraph.dataset import ContactMapDataset


TEST_DATA_PATH = Path(__file__).parent / "data/BBA-subset-100.h5"


def test_init():
    dataset = ContactMapDataset(TEST_DATA_PATH, "contact_map", ["rmsd"], 5)
    sample = dataset[0]
    assert sample["X"].num_features == 5
    assert sample["X"].num_nodes == 28