import time
import h5py
import numpy as np
from tqdm import tqdm
from typing import List, Optional
from sklearn import preprocessing
import MDAnalysis
from MDAnalysis.analysis import distances, rms, align
from concurrent.futures import ProcessPoolExecutor

from mdtools.analysis.order_parameters import fraction_of_contacts
from mdtools.writers import (
    write_contact_map,
    write_fraction_of_contacts,
    write_rmsd,
    write_point_cloud,
)


def aminoacid_int_encoding(pdb_file):
    u = MDAnalysis.Universe(pdb_file)
    resnames = [r.resname for r in u.atoms.residues]
    le = preprocessing.LabelEncoder()
    labels = le.fit_transform(resnames)
    return resnames, labels


def aminoacid_int_to_onehot(labels):
    total_aa = np.max(labels) + 1
    onehot = np.zeros((len(labels), total_aa))
    for i, label in enumerate(labels):
        onehot[i][label] = 1
    return onehot


def write_h5(
    save_file: str,
    rmsds: List[float],
    fncs: List[float],
    rows: List[np.ndarray],
    cols: List[np.ndarray],
    vals: Optional[List[np.ndarray]],
    point_clouds: np.ndarray,
):
    """
    Saves data to h5 file. All data is optional, can save any
    combination of data input arrays.

    Parameters
    ----------
    save_file : str
        Path of output h5 file used to save datasets.
    rmsds : List[float]
        Stores rmsd data.
    fncs : List[float]
        Stores fraction of native contact data.
    rows: List[np.ndarray]
        rows[i] represents the row indices of a contact map where a 1 exists.
    cols: List[np.ndarray]
        cols[i] represents the column indices of a contact map where a 1 exists
    vals: Optional[List[np.ndarray]]
        Values associated with the contact map row,col entries.
    point_clouds : np.ndarray
        XYZ coordinate data in the shape: (N, 3, num_residues)
    """
    with h5py.File(save_file, "w", swmr=False) as h5_file:
        write_contact_map(h5_file, rows, cols, vals)
        write_rmsd(h5_file, rmsds)
        write_fraction_of_contacts(h5_file, fncs)
        write_point_cloud(h5_file, point_clouds)


def traj_to_dset(
    topology: str,
    ref_topology: str,
    traj_file: str,
    save_file: Optional[str] = None,
    cutoff: float = 8.0,
    selection: str = "protein and name CA",
    skip_every: int = 1,
    verbose: bool = False,
    print_every: int = 10,
):
    """
    Implementation for generating machine learning datasets
    from raw molecular dynamics trajectory data. This function
    uses MDAnalysis to load the trajectory file and given a
    custom atom selection computes contact matrices, RMSD to
    reference state, fraction of reference contacts and the
    point cloud (xyz coordinates) of each frame in the trajectory.
    Parameters
    ----------
    topology : str
        Path to topology file: CHARMM/XPLOR PSF topology file,
        PDB file or Gromacs GRO file.
    ref_topology : str
        Path to reference topology file for aligning trajectory:
        CHARMM/XPLOR PSF topology file, PDB file or Gromacs GRO file.
    traj_file : str
        Trajectory file (in CHARMM/NAMD/LAMMPS DCD, Gromacs XTC/TRR,
        or generic. Stores coordinate information for the trajectory.
    cutoff : float
        Angstrom cutoff distance to compute contact maps.
    save_file : Optional[str]
        Path to output h5 dataset file name.
    selection : str
        Selection set of atoms in the protein.
    skip_every : int
        Only colelct data every `skip_every` frames.
    verbose: bool
        If true, prints verbose output.
    print_every: int
        Prints update every `print_every` frame.
    Returns
    -------
    Tuple[List] : rmsds, fncs, rows, cols
        Lists containing data to be written to HDF5.
    """

    # start timer
    start_time = time.time()

    # Load simulation and reference structures
    sim = MDAnalysis.Universe(topology, traj_file)
    ref = MDAnalysis.Universe(ref_topology)

    if verbose:
        print("Traj length: ", len(sim.trajectory))

    # Align trajectory to compute accurate RMSD and point cloud
    align.AlignTraj(sim, ref, select="protein", in_memory=True).run()

    if verbose:
        print(f"Finish aligning after: {time.time() - start_time} seconds")

    # Atom selection for reference
    atoms = sim.select_atoms(selection)
    # Get atomic coordinates of reference atoms
    ref_positions = ref.select_atoms(selection).positions.copy()
    # Get box dimensions
    box = sim.atoms.dimensions
    # Get contact map of reference atoms
    ref_cm = distances.contact_matrix(ref_positions, cutoff, returntype="sparse")

    rmsds, fncs, rows, cols, vals, point_clouds = [], [], [], [], [], []

    for i, _ in enumerate(sim.trajectory[::skip_every]):

        # Point cloud positions of selected atoms in frame i
        positions = atoms.positions

        # Compute contact map of current frame (scipy lil_matrix form)
        cm = distances.contact_matrix(positions, cutoff, box=box, returntype="sparse")
        coo = cm.tocoo()
        rows.append(coo.row.astype("int16"))
        cols.append(coo.col.astype("int16"))
        vals.append(distances.self_distance_array(positions, box=box))

        # Compute and store fraction of native contacts
        fncs.append(fraction_of_contacts(cm, ref_cm))

        # Compute and store RMSD to reference state
        rmsds.append(
            rms.rmsd(positions, ref_positions, center=True, superposition=True)
        )

        # Store reference atoms point cloud of current frame
        point_clouds.append(positions.copy())

        if verbose:
            if i % print_every == 0:
                msg = f"Frame {i}/{len(sim.trajectory)}"
                msg += f"\trmsd: {rmsds[i]}"
                msg += f"\tfnc: {fncs[i]}"
                msg += f"\tshape: {positions.shape}"
                print(msg)

    point_clouds = np.transpose(point_clouds, [0, 2, 1])

    if save_file:
        write_h5(save_file, rmsds, fncs, rows, cols, vals, point_clouds)

    if verbose:
        print(f"Duration {time.time() - start_time}s")

    return rmsds, fncs, rows, cols, point_clouds


def _worker(kwargs):
    """Helper function for parallel data collection."""
    return traj_to_dset(**kwargs)


def parallel_preprocess(
    topology_files: List[str],
    traj_files: List[str],
    ref_topology: str,
    save_files: List[str],
    cutoff: float = 8.0,
    selection: str = "protein and name CA",
    print_every: int = 1000,
    num_workers: int = 10,
):

    kwargs = [
        {
            "topology": topology,
            "ref_topology": ref_topology,
            "traj_file": traj_file,
            "save_file": save_file,
            "cutoff": cutoff,
            "selection": selection,
            "verbose": not bool(i % num_workers),
            "print_every": print_every,
        }
        for i, (topology, traj_file, save_file) in enumerate(
            zip(topology_files, traj_files, save_files)
        )
    ]

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        for _ in tqdm(executor.map(_worker, kwargs)):
            pass
