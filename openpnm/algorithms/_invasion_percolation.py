import logging
import warnings
import heapq as hq
import numpy as np
from collections import namedtuple
from openpnm.utils import SettingsAttr, Docorator
from openpnm.topotools import find_clusters
from openpnm.algorithms import GenericAlgorithm
logger = logging.getLogger(__name__)
docstr = Docorator()

__all__ = ['InvasionPercolation']


@docstr.get_sections(base='IPSettings',
                     sections=['Parameters', 'Other Parameters'])
@docstr.dedent
class IPSettings:
    r"""

    Parameters
    ----------
    %(GenericAlgorithmSettings.parameters)s
    pore_volume : str
        The dictionary key for the pore volume array
    throat_volume : str
        The dictionary key for the throat volume array
    entry_pressure : str
        The dictionary key for the throat capillary pressure

    """
    phase = ''
    pore_volume = 'pore.volume'
    throat_volume = 'throat.volume'
    entry_pressure = 'throat.entry_pressure'


class InvasionPercolation(GenericAlgorithm):
    r"""
    A classic invasion percolation algorithm optimized for speed with numba

    Parameters
    ----------
    network : GenericNetwork
        The Network upon which the invasion will occur

    Notes
    ----
    This algorithm uses a `binary
    heap <https://en.wikipedia.org/wiki/Binary_heap>`_ to store a list of all
    accessible throats, sorted according to entry pressure.  This means that
    item [0] in the heap is the most easily invaded throat that is currently
    accessible by the invading fluid, so looking up which throat to invade
    next is computationally trivial. In order to keep the list sorted,
    adding new throats to the list takes more time; however, the heap data
    structure is very efficient at this.

    """
    def __init__(self, phase, settings=None, **kwargs):
        self.settings = SettingsAttr(IPSettings, settings)
        super().__init__(settings=self.settings, **kwargs)
        self.settings['phase'] = phase.name
        self['pore.invasion_sequence'] = -1
        self['throat.invasion_sequence'] = -1
        self['pore.inlets'] = False

    def set_inlets(self, pores=[], mode='add'):
        r"""
        Specifies from which pores the invasion process starts

        Parameters
        ----------
        pores : array_like
            The list of inlet pores from which the Phase can enter the Network
        mode : str
            Controls how the given inlets are added.  Options are:

            ============ ======================================================
            mode         description
            ============ ======================================================
            'add'        Sets the given ``pores`` to inlets, while keeping any
                         already defined inlets
            'overwrite'  Removes all existing inlets, then adds the given
                         ``pores``
            ============ =======================================================

        """
        mode = self._parse_mode(mode=mode,
                                allowed=['add', 'overwrite'],
                                single=True)
        if mode == 'overwrite':
            self['pore.inlets'] = False
            self['pore.invasion_sequence'] = -1
        self['pore.invasion_sequence'][pores] = 0
        self['pore.inlets'] = True

    def run(self, n_steps=None):
        r"""
        Performs the algorithm for the given number of steps

        Parameters
        ----------
        n_steps : int
            The number of throats to invade during the run.  This can be
            used to incrementally invading the network, allowing for
            simulations to occur between each call to ``run``.

        """

        # Setup arrays and info
        phase = self.project[self.settings['phase']]
        self['throat.entry_pressure'] = phase[self.settings['entry_pressure']]
        # Generated indices into t_entry giving a sorted list
        self['throat.sorted'] = np.argsort(self['throat.entry_pressure'], axis=0)
        self['throat.order'] = 0
        self['throat.order'][self['throat.sorted']] = np.arange(0, self.Nt)

        # Perform initial analysis on input pores
        pores = self['pore.invasion_sequence'] == 0
        Ts = self.project.network.find_neighbor_throats(pores=pores)
        self.queue = []
        for T in self['throat.order'][Ts]:
            hq.heappush(self.queue, T)

        if n_steps is None:
            n_steps = np.inf

        if len(self.queue) == 0:
            logger.warn('queue is empty, this network is fully invaded')
            return

        # Create incidence matrix to get neighbor throats later in _run method
        incidence_matrix = self.network.create_incidence_matrix(fmt='csr')
        t_inv, p_inv, p_inv_t = InvasionPercolation._run_accelerated(
            queue=self.queue,
            t_sorted=self['throat.sorted'],
            t_order=self['throat.order'],
            t_inv=self['throat.invasion_sequence'],
            p_inv=self['pore.invasion_sequence'],
            p_inv_t=np.zeros_like(self['pore.invasion_sequence']),
            conns=self.project.network['throat.conns'],
            idx=incidence_matrix.indices,
            indptr=incidence_matrix.indptr,
            n_steps=n_steps
        )

        self['throat.invasion_sequence'] = t_inv
        self['pore.invasion_sequence'] = p_inv
        self['throat.invasion_pressure'] = self['throat.entry_pressure']
        self['pore.invasion_pressure'] = self['throat.entry_pressure'][p_inv_t]
        self['pore.invasion_pressure'][self['pore.invasion_sequence']==0] = 0.0

    def results(self, Snwp=None):
        r"""
        Returns the phase configuration at the specified non-wetting phase
        (invading phase) saturation.

        Parameters
        ----------
        Snwp : scalar, between 0 and 1
            The network saturation for which the phase configuration is
            desired.

        Returns
        -------
        Two dictionary containing arrays that describe the pore and throat
        distribution at the given saturation.  Specifically, these are:

        **'pore.occupancy'** : 1 indicates the pores is invaded and 0
        otherwise.

        **'throat.occupancy'** : Same as described above but for throats.

        """
        if Snwp is None:
            Np = self['pore.invasion_sequence']
            Nt = self['throat.invasion_sequence']
            data = {'pore.invasion_sequence': Np,
                    'throat.invasion_sequence': Nt}
        else:
            net = self.project.network
            P12 = net['throat.conns']
            # Fetch void volume for pores and throats
            Vp = net[self.settings['pore_volume']]
            Vt = net[self.settings['throat_volume']]
            # Fetch the order of filling
            Np = self['pore.invasion_sequence']
            Nt = self['throat.invasion_sequence']
            # Create Nt-long mask of which pores were filled when throat was filled
            Pinv = (Np[P12].T == Nt).T
            # If a pore and throat filled together, find combined volume
            Vinv = np.vstack(((Pinv*Vp[P12]).T, Vt)).T
            Vinv = np.sum(Vinv, axis=1)
            # Convert to cumulative volume filled as each throat is invaded
            x = np.argsort(Nt)  # Find order throats were invaded
            Vinv_cum = np.cumsum(Vinv[x])
            # Normalized cumulative volume filled into saturation
            S = Vinv_cum/(Vp.sum() + Vt.sum())
            # Find throat invasion step where Snwp was reached
            try:
                N = np.where(S < Snwp)[0][-1]
            except Exception:
                N = -np.inf
            data = {'pore.occupancy': Np <= N, 'throat.occupancy': Nt <= N}
        return data

    def apply_trapping(self, outlets):
        """
        Apply trapping based on algorithm described by Y. Masson [1].
        It is applied as a post-process and runs the percolation algorithm
        in reverse assessing the occupancy of pore neighbors. Consider the
        following scenario when running standard IP without trapping,
        three situations can happen after each invasion step:

            * The number of defending clusters stays the same and clusters
              can shrink
            * A cluster of size one is suppressed
            * A cluster is split into multiple clusters

        In reverse the following opposite situations can happen:

            * The number of defending clusters stays the same and clusters
              can grow
            * A cluster of size one is created
            * Mutliple clusters merge into one cluster

        With trapping the reversed rules are adjusted so that only clusters
        that do not connect to a sink can grow and merge. At the point that a
        neighbor connected to a sink is touched the trapped cluster stops
        growing as this is the point of trapping in forward invasion time.

        Logger info displays the invasion sequence and pore index and a message
        with condition number based on the modified trapping rules and the
        assignment of the pore to a given cluster.

        Initially all invaded pores are given cluster label -1
        Outlets / Sinks are given -2
        New clusters that grow into fully trapped clusters are either
        identified at the point of breakthrough or grow from nothing if the
        full invasion sequence is run, they are assigned numbers from 0 up.

        Ref:
        [1] Masson, Y., 2016. A fast two-step algorithm for invasion
        percolation with trapping. Computers & Geosciences, 90, pp.41-48

        Parameters
        ----------
        outlets : list or array of pore indices for defending fluid to escape
        through

        Returns
        -------
        Creates a throat array called 'pore.clusters' in the Algorithm
        dictionary. Any positive number is a trapped cluster

        Also creates 2 boolean arrays Np and Nt long called '<element>.trapped'

        """
        # First see if network is fully invaded
        net = self.project.network
        invaded_ps = self['pore.invasion_sequence'] > -1
        if ~np.all(invaded_ps):
            # Put defending phase into clusters
            clusters = find_clusters(network=net, mask=~invaded_ps)
            # Identify clusters that are connected to an outlet and set to -2
            # -1 is the invaded fluid
            # -2 is the defender fluid able to escape
            # All others now trapped clusters which grow as invasion is reversed
            out_clusters = np.unique(clusters[outlets])
            for c in out_clusters:
                if c >= 0:
                    clusters[clusters == c] = -2
        else:
            # Go from end
            clusters = np.ones(net.Np, dtype=int)*-1
            clusters[outlets] = -2

        # Turn into a list for indexing
        inv_seq = np.vstack((self['pore.invasion_sequence'].astype(int),
                             np.arange(0, net.Np, dtype=int))).T
        # Reverse sort list
        inv_seq = inv_seq[inv_seq[:, 0].argsort()][::-1]
        next_cluster_num = np.max(clusters)+1
        # For all the steps after the inlets are set up to break-through
        # Reverse the sequence and assess the neighbors cluster state
        stopped_clusters = np.zeros(net.Np, dtype=bool)
        all_neighbors = net.find_neighbor_pores(net.pores(), flatten=False,
                                                include_input=True)
        for un_seq, pore in inv_seq:
            if pore not in outlets and un_seq > 0:  # Skip inlets and outlets
                nc = clusters[all_neighbors[pore]]  # Neighboring clusters
                unique_ns = np.unique(nc[nc != -1])  # Unique Neighbors
                seq_pore = "S:"+str(un_seq)+" P:"+str(pore)
                if np.all(nc == -1):
                    # This is the start of a new trapped cluster
                    clusters[pore] = next_cluster_num
                    next_cluster_num += 1
                    msg = (seq_pore+" C:1 new cluster number: "
                           + str(clusters[pore]))
                    logger.info(msg)
                elif len(unique_ns) == 1:
                    # Grow the only connected neighboring cluster
                    if not stopped_clusters[unique_ns[0]]:
                        clusters[pore] = unique_ns[0]
                        msg = (seq_pore+" C:2 joins cluster number: "
                               + str(clusters[pore]))
                        logger.info(msg)
                    else:
                        clusters[pore] = -2
                elif -2 in unique_ns:
                    # We have reached a sink neighbor, stop growing cluster
                    msg = (seq_pore+" C:3 joins sink cluster")
                    logger.info(msg)
                    clusters[pore] = -2
                    # Stop growth and merging
                    stopped_clusters[unique_ns[unique_ns > -1]] = True
                else:
                    # We might be able to do some merging
                    # Check if any stopped clusters are neighbors
                    if np.any(stopped_clusters[unique_ns]):
                        msg = (seq_pore+" C:4 joins sink cluster")
                        logger.info(msg)
                        clusters[pore] = -2
                        # Stop growing all neighboring clusters
                        stopped_clusters[unique_ns] = True
                    else:
                        # Merge multiple un-stopped trapped clusters
                        new_num = unique_ns[0]
                        clusters[pore] = new_num
                        for c in unique_ns:
                            clusters[clusters == c] = new_num
                            msg = (seq_pore + " C:5 merge clusters: "
                                   + str(c) + " into "+str(new_num))
                            logger.info(msg)

        # And now return clusters
        self['pore.clusters'] = clusters
        logger.info("Number of trapped clusters"
                    + str(np.sum(np.unique(clusters) >= 0)))
        self['pore.trapped'] = self['pore.clusters'] > -1
        trapped_ts = net.find_neighbor_throats(self['pore.trapped'])
        self['throat.trapped'] = np.zeros([net.Nt], dtype=bool)
        self['throat.trapped'][trapped_ts] = True
        self['pore.invasion_sequence'][self['pore.trapped']] = -1
        self['throat.invasion_sequence'][self['throat.trapped']] = -1

    def get_intrusion_data(self):
        r"""
        Get the percolation data as the invader volume or number fraction vs
        the capillary pressure.

        """
        if 'pore.invasion_pressure' not in self.props():
            logger.error('Algorithm must be run first.')
            return None
        net = self.project.network
        pvols = net[self.settings['pore_volume']]
        tvols = net[self.settings['throat_volume']]
        tot_vol = np.sum(pvols) + np.sum(tvols)
        # Normalize
        pvols /= tot_vol
        tvols /= tot_vol
        # Remove trapped volume
        pvols[self['pore.invasion_sequence'] == -1] = 0.0
        tvols[self['throat.invasion_sequence'] == -1] = 0.0
        pseq = self['pore.invasion_sequence']
        tseq = self['throat.invasion_sequence']
        tPc = self['throat.invasion_pressure']
        pPc = self['pore.invasion_pressure']
        # Change the entry pressure for trapped pores and throats to be 0
        pPc[self['pore.invasion_sequence'] == -1] = 0.0
        tPc[self['throat.invasion_sequence'] == -1] = 0.0
        vols = np.concatenate((pvols, tvols))
        seqs = np.concatenate((pseq, tseq))
        Pcs = np.concatenate((pPc, tPc))
        data = np.rec.fromarrays([seqs, vols, Pcs], formats=['i', 'f', 'f'],
                                 names=['seq', 'vol', 'Pc'])
        data.sort(axis=0, order='seq')
        sat = np.cumsum(data.vol)
        pc_curve = namedtuple('pc_curve', ('Pcap', 'S_tot'))
        data = pc_curve(data.Pc, sat)
        return data

    def plot_intrusion_curve(self, ax=None, num_markers=25):
        r"""
        Plot the percolation curve as the invader volume or number fraction vs
        the capillary capillary pressure.
        """
        import matplotlib.pyplot as plt

        data = self.get_intrusion_data()
        if data is None:
            raise Exception("You must run the algorithm first.")
        if ax is None:
            fig, ax = plt.subplots()
        markevery = max(data.Pcap.size // num_markers, 1)
        ax.semilogx(data.Pcap, data.S_tot, markevery=markevery)
        plt.ylabel('invading phase saturation')
        plt.xlabel('capillary pressure')
        plt.grid(True)

    def _run_accelerated(queue, t_sorted, t_order, t_inv, p_inv, p_inv_t,
                         conns, idx, indptr, n_steps):
        r"""
        Numba-jitted run method for InvasionPercolation class.

        Notes
        -----
        ``idx`` and ``indptr`` are properties are the network's incidence
        matrix, and are used to quickly find neighbor throats.

        Numba doesn't like foreign data types (i.e. GenericNetwork), and so
        ``find_neighbor_throats`` method cannot be called in a jitted method.

        Nested wrapper is for performance issues (reduced OpenPNM import)
        time due to local numba import

        """
        from numba import njit

        @njit
        def wrapper(queue, t_sorted, t_order, t_inv, p_inv, p_inv_t, conns,
                    idx, indptr, n_steps):
            count = 1
            while (len(queue) > 0) and (count < (n_steps + 1)):
                # Find throat at the top of the queue
                t = hq.heappop(queue)
                # Extract actual throat number
                t_next = t_sorted[t]
                t_inv[t_next] = count
                # If throat is duplicated
                while len(queue) > 0 and queue[0] == t:
                    # Note: Preventing duplicate entries below might save some time
                    t = hq.heappop(queue)
                # Find pores connected to newly invaded throat
                Ps = conns[t_next]
                # Remove already invaded pores from Ps
                Ps = Ps[p_inv[Ps] < 0]
                if len(Ps) > 0:
                    p_inv[Ps] = count
                    p_inv_t[Ps] = t_next
                    for i in Ps:
                        Ts = idx[indptr[i]:indptr[i+1]]
                        Ts = Ts[t_inv[Ts] < 0]
                    for i in set(Ts):   # set(Ts) to exclude repeated neighbor throats
                        hq.heappush(queue, t_order[i])
                count += 1
            return t_inv, p_inv, p_inv_t

        return wrapper(queue, t_sorted, t_order, t_inv, p_inv, p_inv_t, conns,
                       idx, indptr, n_steps)


def find_trapped_clusters(ip, outlets, bins=50):
    from openpnm.topotools import find_clusters
    net = ip.network
    outlets = net.to_mask(pores=outlets)
    if bins is None:  # Use all steps, very slow
        steps = np.arange(ip['pore.invasion_sequence'].max())[-1::-1]
    elif isinstance(bins, int):  # Generate even spaced steps
        steps = np.linspace(ip['pore.invasion_sequence'].max(), 0, bins)
    else:  # Used given steps
        steps = bins
    # Initialize arrays for tracking what is trapped
    p_trapped = -np.ones(net.Np, dtype=int)
    t_trapped = -np.ones(net.Nt, dtype=int)
    for s in steps:
        t_mask = ip['throat.invasion_sequence'] >= s
        p_mask = ip['pore.invasion_sequence'] >= s
        p_cluster, t_cluster = find_clusters(network=net, mask=t_mask)
        hits = np.unique(p_cluster[outlets])
        hits = hits[hits > -1]
        if len(hits) > 0:
            temp = np.isin(p_cluster, hits, invert=True)*p_mask
            p_trapped[temp] = int(s)
            temp = np.isin(t_cluster, hits, invert=True)*t_mask
            t_trapped[temp] = int(s)
        else:
            p_trapped[p_mask] = int(s)
            t_trapped[t_mask] = int(s)
    return p_trapped, t_trapped


if __name__ == '__main__':
    import openpnm as op
    import matplotlib.pyplot as plt
    from scipy.stats import rankdata
    pn = op.network.Cubic(shape=[25, 25, 1], spacing=1e-4)
    pn.add_model_collection(op.models.collections.geometry.spheres_and_cylinders)
    pn.regenerate_models()
    water = op.phase.Water(network=pn, name='h2o')
    water.add_model_collection(op.models.collections.physics.standard)
    water.regenerate_models()
    ip = InvasionPercolation(network=pn, phase=water)
    ip.set_inlets(pn.pores('left'))
    ip.run()
    outlets = pn.pores('right')
    bins = None
    p, t = find_trapped_clusters(ip, outlets, bins)
    ip['pore.invasion_sequence'][p >= 0] = -1
    ip['throat.invasion_sequence'][t >= 0] = -1

    # ip.plot_intrusion_curve()
    # %%
    fig, ax = plt.subplots(1, 1)
    ax.set_facecolor('grey')
    ax = op.topotools.plot_coordinates(network=pn, c='w', ax=ax)
    ax = op.topotools.plot_connections(network=pn,
                                       throats=ip['throat.invasion_sequence'] > 0,
                                       color_by=ip['throat.invasion_sequence'][t <= 0],
                                       ax=ax)























