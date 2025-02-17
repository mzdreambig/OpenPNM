import logging
import numpy as np
from pathlib import Path
from openpnm.utils import flat_list


__all__ = [
    '_convert_data',
    '_update_network',
    '_parse_filename',
    '_parse_args',
    '_is_transient',
]


logger = logging.getLogger(__name__)


def _convert_data(project):
    # Convert arrays of 1's and/or 0's to booleans
    for obj in project:
        for item in obj.keys():
            ra = obj[item]
            if (ra.dtype == int) and (ra.max() <= 1) and (ra.min() >= 0):
                obj.update({item: ra.astype(bool)})
    return project


def _update_network(network, net):
    # Infer Np and Nt from length of given prop arrays in file
    for el in ["pore", "throat"]:
        N = [np.shape(net[i])[0] for i in net.keys() if i.startswith(el)]
        if N:
            N = np.array(N)
            if np.all(N == N[0]):
                if (network._count(el) == N[0]) or (network._count(el) == 0):
                    network.update({el + ".all": np.ones((N[0],), dtype=bool)})
                    net.pop(el + ".all", None)
                else:
                    raise Exception(
                        f"Length of {el} data in file does not match network"
                    )
            else:
                raise Exception(f"{el} data in file have inconsistent lengths")
    # Add data on dummy net to actual network
    for item in net.keys():
        # Try to infer array types and change if necessary
        # Chcek for booleans disguised and 1's and 0's
        if (net[item].dtype is int) and (net[item].max() == 1):
            net[item] = net[item].astype(bool)
        # Write data to network object
        network.update({item: net[item]})

    return network


def _parse_filename(filename, ext=""):
    p = Path(filename)
    p = p.resolve()
    if ext == "":
        ext = p.suffix
    # If extension not part of filename
    ext = "." + (ext.strip("."))
    if p.suffix != ext:
        p = p.with_suffix(ext)
    return p


def _parse_args(network, phases):
    # Convert network to a list, even if empty
    if network is None:
        network = []
    else:
        network = flat_list(network)
    # Ensure phases is a list, even if empty
    phases = flat_list(phases)
    # Get handle to project object
    if len(network) == 0:
        if len(phases) == 0:
            raise Exception("Must specify one of network or phase")
        project = phases[0].project
    else:
        project = network[0].project
    return (project, network, phases)


def _is_transient(phases):
    raise Exception("this function does not work any more!")
