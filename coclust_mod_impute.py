import numpy as np
from sklearn.utils import check_random_state, check_array
from joblib import Parallel, delayed, effective_n_jobs

from coclust.initialization import random_init
from coclust.coclustering.base_diagonal_coclust import BaseDiagonalCoclust


def summarize_blocks(X, z, wT):
    """get the summary matrix from a contingency matrix X
        with row labels z and column labels w
    Parameters
    ----------
    X: np.array, n x d, contingency table (n rows d, columns)
    z: np.array, n x k, row assignments (n rows, k classes)
    wT: np.array, d x l, transpose of column assignments (d columns, l classes)

    Returns
    -------
    for each block i,j return the sum of values in that block, shape k x l
    """
    return z.T @ X @ wT


def get_block_counts(z, wT):
    """get the count of item in each block of matrix X (n x d)
    Parameters
    ----------
    z: np.array, n x k, row assignments (n rows, k classes)
    wT: np.array, d x l, transpose of column assignments (d columns, l classes)

    Returns
    -------
    for each block i,j return the number of items in that block, shape k x l
    """
    return z.sum(axis=0)[:, np.newaxis] * wT.sum(axis=0)


def _impute_block_representative(X, Z, W, z, w, r_nan, c_nan):
    s = summarize_blocks(X, Z, W)
    bc = get_block_counts(Z, W)
    bc[bc == 0] = 1  # avoid divide by 0
    block_rep_vals = s / bc
    X[r_nan, c_nan] = block_rep_vals[z[r_nan].ravel(), w[c_nan].ravel()]
    return X


def _compute_modularity_matrix(X):
    # Compute the modularity matrix
    row_sums = X.sum(axis=1)[:, np.newaxis]
    col_sums = X.sum(axis=0)[np.newaxis, :]
    N = float(X.sum())
    indep = row_sums @ col_sums / N

    # B is a numpy matrix
    B = X - indep
    return B, N


def _fit_single(X, n_clusters, impute_fn, r_na, c_na, random_state, init, max_iter, tol, y=None):
    """Perform one run of co-clustering by direct maximization of graph
    modularity.

    Parameters
    ----------
    X : numpy array or scipy sparse matrix, shape=(n_samples, n_features)
        Matrix to be analyzed
    """
    if init is None:
        W = random_init(n_clusters, X.shape[1], random_state)
    else:
        W = np.array(init, dtype=float)

    w = np.argmax(W, axis=1)[:, np.newaxis]

    Z = np.zeros((X.shape[0], n_clusters))

    z_labels = np.arange(n_clusters)
    w_labels = z_labels

    B, N = _compute_modularity_matrix(X)

    modularities = []

    # Loop
    m_begin = float("-inf")
    change = True
    iteration = 0
    while change:
        change = False

        # Reassign rows
        BW = B.dot(W)
        z = np.argmax(BW, axis=1)[:, np.newaxis]
        Z = (z == z_labels)*1
        
        # Update missing values in X using BW
        X = impute_fn(X, Z, W, z, w, r_na, c_na)
        B, N = _compute_modularity_matrix(X)

        # Reassign columns
        BtZ = (B.T).dot(Z)
        w = np.argmax(BtZ, axis=1)[:, np.newaxis]
        W = (w == w_labels)*1
        # for idx, k in enumerate(np.argmax(BtZ, axis=1)):
        #     W[idx, :] = 0
        #     W[idx, k] = 1

        # Update missing values in X using BtZ
        X = impute_fn(X, Z, W, z, w, r_na, c_na)
        B, N = _compute_modularity_matrix(X)

        k_times_k = (Z.T).dot(BW)
        m_end = np.trace(k_times_k)
        iteration += 1
        if (np.abs(m_end - m_begin) > tol and
                iteration < max_iter):
            modularities.append(m_end/N)
            m_begin = m_end
            change = True

    row_labels_ = np.argmax(Z, axis=1).tolist()
    column_labels_ = np.argmax(W, axis=1).tolist()
    modularity = m_end / N
    nb_iterations = iteration
    return row_labels_,  column_labels_, modularity, modularities, nb_iterations, X


class CoclustModImpute(BaseDiagonalCoclust):
    """Co-clustering by direct maximization of graph modularity.

    Parameters
    ----------
    n_clusters : int, optional, default: 2
        Number of co-clusters to form

    init : numpy array or scipy sparse matrix, \
        shape (n_features, n_clusters), optional, default: None
        Initial column labels

    max_iter : int, optional, default: 20
        Maximum number of iterations

    n_init : int, optional, default: 1
        Number of time the algorithm will be run with different
        initializations. The final results will be the best output of `n_init`
        consecutive runs in terms of modularity.

    random_state : integer or numpy.RandomState, optional
        The generator used to initialize the centers. If an integer is
        given, it fixes the seed. Defaults to the global numpy random
        number generator.

    tol : float, default: 1e-9
        Relative tolerance with regards to modularity to declare convergence

    Attributes
    ----------
    row_labels_ : array-like, shape (n_rows,)
        Bicluster label of each row

    column_labels_ : array-like, shape (n_cols,)
        Bicluster label of each column

    modularity : float
        Final value of the modularity

    modularities : list
        Record of all computed modularity values for all iterations

    References
    ----------
    * Ailem M., Role F., Nadif M., Co-clustering Document-term Matrices by \
    Direct Maximization of Graph Modularity. CIKM 2015: 1807-1810
    """

    def __init__(self, n_clusters=2, init=None, max_iter=20, n_init=1,
                 tol=1e-9, random_state=None, n_jobs=1):
        self.n_clusters = n_clusters
        self.init = init
        self.max_iter = max_iter
        self.n_init = n_init
        self.tol = tol
        self.random_state = random_state
        self.n_jobs = n_jobs
        # to remove except for self.modularity = -np.inf!!!
        self.row_labels_ = None
        self.column_labels_ = None
        self.modularity = -np.inf
        self.modularities = []

    def fit(self, X, impute_fn, initial_vals='zero', y=None):
        """Perform co-clustering by direct maximization of graph modularity.

        Parameters
        ----------
        X : numpy array or scipy sparse matrix, shape=(n_samples, n_features)
            Matrix to be analyzed
        """

        random_state = check_random_state(self.random_state)

        X_ = X.astype(float)

        r_nan, c_nan = np.where(np.isnan(X_))

        if isinstance(initial_vals, np.ndarray):
            X_[r_nan, c_nan] = initial_vals
        elif initial_vals == 'zero':
            X_[r_nan, c_nan] = 0.
        elif initial_vals == 'rand':
            np.random.seed(self.random_state)
            X_[r_nan, c_nan] = np.random.rand(r_nan.shape[0]) * np.nanmax(X_)

        check_array(X_, accept_sparse=True, dtype="numeric", order=None,
                    copy=False, force_all_finite=True, ensure_2d=True,
                    allow_nd=False, ensure_min_samples=self.n_clusters,
                    ensure_min_features=self.n_clusters, estimator=None)

#         if type(X_) == np.ndarray:
#             X_ = np.matrix(X_)

        modularity = self.modularity
        modularities = []
        row_labels = None
        column_labels = None
        seeds = random_state.randint(np.iinfo(np.int32).max, size=self.n_init)
        if effective_n_jobs(self.n_jobs) == 1 or True:
            for seed in seeds:
                new_row_labels,  new_column_labels, new_modularity, new_modularities, new_nb_iterations, new_X_ = _fit_single(
                    X_, self.n_clusters, impute_fn, r_nan, c_nan, seed, self.init, self.max_iter, self.tol, y)
                if np.isnan(new_modularity):
                    raise ValueError(
                        "matrix may contain unexpected NaN values")
                # remember attributes corresponding to the best modularity
                if (new_modularity > modularity):
                    modularity = new_modularity
                    modularities = new_modularities
                    row_labels = new_row_labels
                    column_labels = new_column_labels
                    X_ = new_X_
        else:
            results = Parallel(n_jobs=self.n_jobs, verbose=0)(
                delayed(_fit_single)(X_, self.n_clusters, impute_fn, r_nan, c_nan,
                                     seed, self.init, self.max_iter, self.tol, y)
                for seed in seeds)
            (list_of_row_labels,  list_of_column_labels, list_of_modularity,
             list_of_modularities, list_of_nb_iterations, list_of_imputed_X) = zip(*results)
            best = np.argmax(list_of_modularity)
            row_labels = list_of_row_labels[best]
            column_labels = list_of_column_labels[best]
            modularity = list_of_modularity[best]
            modularities = list_of_modularities[best]
            n_iter = list_of_nb_iterations[best]
            X_ = list_of_imputed_X[best]

        # update instance variables
        self.modularity = modularity
        self.modularities = modularities
        self.row_labels_ = row_labels
        self.column_labels_ = column_labels
        self.X_ = X_

        return self
