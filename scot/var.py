# Released under The MIT License (MIT)
# http://opensource.org/licenses/MIT
# Copyright (c) 2013-2016 SCoT Development Team

"""Vector autoregressive (VAR) model implementation."""

from __future__ import print_function

import numpy as np
import scipy as sp

from .varbase import VARBase, _construct_var_eqns
from .datatools import cat_trials, atleast_3d
from . import xvschema as xv
from .parallel import parallel_loop
from . import config


class VAR(VARBase):
    """Builtin VAR implementation.

    This class provides least squares VAR model fitting with optional ridge
    regression.
    
    Parameters    
    ----------
    model_order : int
        Autoregressive model order.
    delta : float, optional
        Ridge penalty parameter.
    xvschema : func, optional
        Function that creates training and test sets for cross-validation. The
        function takes two parameters: the current cross-validation run (int)
        and the number of trials (int). It returns a tuple of two arrays: the
        training set and the testing set.
    n_jobs : int | None, optional
        Number of jobs to run in parallel for various tasks (e.g. whiteness
        testing). If set to None, joblib is not used at all. Note that the main
        script must be guarded with `if __name__ == '__main__':` when using
        parallelization.
    verbose : bool | None, optional
        Whether to print information to stdout. The default is None, which
        means the verbosity setting from the global configuration is used.
    """
    def __init__(self, model_order, delta=0, xvschema=xv.multitrial, n_jobs=1,
                 verbose=None):
        super(VAR, self).__init__(model_order=model_order, n_jobs=n_jobs,
                         verbose=verbose)
        self.delta = delta
        self.xvschema = xvschema

    def fit(self, data, method='lstsq'):
        """Fit VAR model to data.
        
        Parameters
        ----------
        data : array, shape (trials, channels, samples) or (channels, samples)
            Epoched or continuous data set.
            
        Returns
        -------
        self : :class:`VAR`
            The :class:`VAR` object to facilitate method chaining (see usage
            example).
        """
        data = atleast_3d(data)

        if method == 'lstsq':
            fit_func = self._fit_lstsq
        
        elif method == 'vm':
            fit_func = self._fit_vm

        else:
            raise NotImplementedError(method)

        return fit_func(data)

    def optimize_order(self, data, min_p=1, max_p=None):
        """Determine optimal model order by minimizing the mean squared
        generalization error.

        Parameters
        ----------
        data : array, shape (n_trials, n_channels, n_samples)
            Epoched data set on which to optimize the model order. At least two
            trials are required.
        min_p : int
            Minimal model order to check.
        max_p : int
            Maximum model order to check
        """
        data = np.asarray(data)
        if data.shape[0] < 2:
            raise ValueError("At least two trials are required.")

        msge, prange = [], []

        par, func = parallel_loop(_get_msge_with_gradient, n_jobs=self.n_jobs,
                                  verbose=self.verbose)
        if self.n_jobs is None:
            npar = 1
        elif self.n_jobs < 0:
                npar = 4  # is this a sane default?
        else:
            npar = self.n_jobs

        p = min_p
        while True:
            result = par(func(data, self.delta, self.xvschema, 1, p_)
                         for p_ in range(p, p + npar))
            j, k = zip(*result)
            prange.extend(range(p, p + npar))
            msge.extend(j)
            p += npar
            if max_p is None:
                if len(msge) >= 2 and msge[-1] > msge[-2]:
                    break
            else:
                if prange[-1] >= max_p:
                    i = prange.index(max_p) + 1
                    prange = prange[:i]
                    msge = msge[:i]
                    break
        self.p = prange[np.argmin(msge)]
        return zip(prange, msge)

    def optimize_delta_bisection(self, data, skipstep=1, verbose=None):
        """Find optimal ridge penalty with bisection search.
        
        Parameters
        ----------
        data : array, shape (n_trials, n_channels, n_samples)
            Epoched data set. At least two trials are required.
        skipstep : int, optional
            Speed up calculation by skipping samples during cost function
            calculation.
            
        Returns
        -------
        self : :class:`VAR`
            The :class:`VAR` object to facilitate method chaining (see usage
            example).
        """
        data = atleast_3d(data)
        if data.shape[0] < 2:
            raise ValueError("At least two trials are required.")

        if verbose is None:
            verbose = config.getboolean('scot', 'verbose')

        maxsteps = 10
        maxdelta = 1e50

        a = -10
        b = 10

        trform = lambda x: np.sqrt(np.exp(x))

        msge = _get_msge_with_gradient_func(data.shape, self.p)

        ja, ka = msge(data, trform(a), self.xvschema, skipstep, self.p)
        jb, kb = msge(data, trform(b), self.xvschema, skipstep, self.p)

        # before starting the real bisection, assure the interval contains 0
        while np.sign(ka) == np.sign(kb):
            if verbose:
                print('Bisection initial interval (%f,%f) does not contain 0. '
                      'New interval: (%f,%f)' % (a, b, a * 2, b * 2))
            a *= 2
            b *= 2
            ja, ka = msge(data, trform(a), self.xvschema, skipstep, self.p)
            jb, kb = msge(data, trform(b), self.xvschema, skipstep, self.p)

            if trform(b) >= maxdelta:
                if verbose:
                    print('Bisection: could not find initial interval.')
                    print(' ********* Delta set to zero! ************ ')
                return 0

        nsteps = 0

        while nsteps < maxsteps:
            # point where the line between a and b crosses zero
            # this is not very stable!
            #c = a + (b-a) * np.abs(ka) / np.abs(kb-ka)
            c = (a + b) / 2
            j, k = msge(data, trform(c), self.xvschema, skipstep, self.p)
            if np.sign(k) == np.sign(ka):
                a, ka = c, k
            else:
                b, kb = c, k

            nsteps += 1
            tmp = trform([a, b, a + (b - a) * np.abs(ka) / np.abs(kb - ka)])
            if verbose:
                print('%d Bisection Interval: %f - %f, (projected: %f)' %
                      (nsteps, tmp[0], tmp[1], tmp[2]))

        self.delta = trform(a + (b - a) * np.abs(ka) / np.abs(kb - ka))
        if verbose:
            print('Final point: %f' % self.delta)
        return self
        
    optimize = optimize_delta_bisection

    def _construct_eqns_rls(self, data):
        """Construct VAR equation system with RLS constraint.
        """
        return _construct_var_eqns(data, self.p, self.delta)

    def _fit_lstsq(self, data):
        """
            Fit VAR model to data using linear least squares method
        """

        if self.delta == 0 or self.delta is None:
            # ordinary least squares
            x, y = self._construct_eqns(data)
        else:
            # regularized least squares (ridge regression)
            x, y = self._construct_eqns_rls(data)

        b, res, rank, s = sp.linalg.lstsq(x, y)

        self.coef = b.transpose()

        self.residuals = data - self.predict(data)
        self.rescov = sp.cov(cat_trials(self.residuals[:, :, self.p:]))

        return self

    def _fit_vm(self, data):
        """
            Compute MVAR coefficients with Viera-Morf algorithm

            Adapted from https://github.com/dokato/connectivipy with permission
        """

        pmax = self.p

        assert pmax > 0, "pmax > 0"

        n_trials, n_chans, n_points = data.shape
        cov_func = self.mean_cov

        f, b = data.copy(), data.copy()

        pef = cov_func(data, norm=False)
        peb =pef.copy()

        arf = np.zeros((n_chans, n_chans, pmax))
        arb = np.zeros((n_chans, n_chans, pmax))

        for k in range(0, pmax):
            D = cov_func(f[:, :, k + 1:n_points], 
                         b[:, :, 0:n_points - k - 1], 
                         norm=False)
            arf[:, :, k] = np.dot(D, np.linalg.inv(peb))
            arb[:, :, k] = np.dot(D.T, np.linalg.inv(pef))

            tmp = f[:, :, k+1:] - np.dot(b[:, :, :n_points - k - 1].transpose(0, 2, 1),
                                         arf[:, :, k].T).transpose(0, 2, 1)
            
            b[:, :, :n_points-k-1] = b[::, :, :n_points-k-1] - np.dot(f[:, :, k+1:].transpose(0, 2, 1), 
                                                                      arb[:, :, k].T).transpose(0, 2, 1)
            f[:, :, k+1:] = tmp

            for i in range(k):
                tmpp = arf[:, :, i] - np.dot(arf[:, :, k], arb[:, :, k-i-1])
                arb[:, :, k-i-1] = arb[:, :, k-i-1] - np.dot(arb[:, :, k], arf[:, :, i])
                arf[:, :, i] = tmpp
            peb = cov_func(b[:, :, :n_points-k-1], norm=False)
            pef = cov_func(f[:, :, k+1:], norm=False)

        self.coef = arf.reshape(n_chans, n_chans * pmax)
        self.residuals = data - self.predict(data)
        self.rescov = sp.cov(cat_trials(self.residuals[:, :, self.p:]))

        return self

    def mean_cov(self, x, y=[], p=0, norm=True):
        """
            ADAPTED FROM [1]. Wrapper to multichannel case of new covariance *nconv*

            Args:
                *x* : numpy.array
                    data of shape (n_trials, n_channels, n_points)
                *y* : numpy.array
                    data array, optional, if not given the autocovariance is returned
                *p* = 0 : int
                    window shift of input data. Can be negative
                *norm* = True: bool
                    Whether to divide result by length of *x*

            Returns:
                *mcov* : numpy.array
                    covariance matrix
        """

        n_trials = len(x)
        
        for trial in range(n_trials):
            if trial == 0:
                if not len(y):
                    mcov = self.ncov(x[trial, ...], p=p, norm=norm)
                else:
                    mcov = self.ncov(x[trial, ...], y[trial, ...], p=p, norm=norm)
                continue
            if not len(y):
                mcov += self.ncov(x[trial, ...], p=p, norm=norm)
            else:
                mcov += self.ncov(x[trial, ...], y[trial, ...], p=p, norm=norm)

        return mcov / n_trials

    def ncov(self, x, y=[], p=0, norm=True):

        """
            New covariance
        """

        n_chans, n_points = x.shape

        cov = np.zeros((n_chans, n_chans, abs(p) + 1))

        if len(y) == 0:
            y = x

        if p >= 0:
            for r in range(p+1):
                cov[:, :, r] = np.dot(x[:, :n_points - r], y[:, r:].T)
        else:
            for r in range(abs(p) + 1):
                idxs = np.arange(-r, x.shape[1] - r)
                zy = y.take(idxs, axis=1, mode='wrap')
                cov[:, :, r] = np.dot(x[:, :n_points-r], zy[:, :n_points-r].T)

        if norm:
            kv = cov / (n_points - 1)
        else:
            kv = cov

        if p == 0:
            kv = np.squeeze(kv)

        return kv


class VAR_VM(VARBase):
    """
        VAR implementation with Viera-Morf method

        This class provides VAR model fitting with the Viera-Morf algorithm

        References:
            [1] ConnectiviPy - python module for connectivity analysis
                https://github.com/dokato/connectivipy
    """

    def __init__(self, model_order, xvschema=xv.multitrial, n_jobs=1,
                 verbose=None):
        super(VAR_VM, self).__init__(model_order=model_order, n_jobs=n_jobs,
                                  verbose=verbose)
        self.xvschema = xvschema

    def mean_cov(self, x, y=[], p=0, norm=True):
        """
            ADAPTED FROM [1]. Wrapper to multichannel case of new covariance *nconv*

            Args:
                *x* : numpy.array
                    data of shape (n_trials, n_channels, n_points)
                *y* : numpy.array
                    data array, optional, if not given the autocovariance is returned
                *p* = 0 : int
                    window shift of input data. Can be negative
                *norm* = True: bool
                    Whether to divide result by length of *x*

            Returns:
                *mcov* : numpy.array
                    covariance matrix
        """

        n_trials = len(x)
        
        for trial in range(n_trials):
            if trial == 0:
                if not len(y):
                    mcov = self.ncov(x[trial, ...], p=p, norm=norm)
                else:
                    mcov = self.ncov(x[trial, ...], y[trial, ...], p=p, norm=norm)
                continue
            if not len(y):
                mcov += self.ncov(x[trial, ...], p=p, norm=norm)
            else:
                mcov += self.ncov(x[trial, ...], y[trial, ...], p=p, norm=norm)

        return mcov / n_trials

    def ncov(self, x, y=[], p=0, norm=True):

        """
            New covariance
        """

        n_chans, n_points = x.shape

        cov = np.zeros((n_chans, n_chans, abs(p) + 1))

        if len(y) == 0:
            y = x

        if p >= 0:
            for r in range(p+1):
                cov[:, :, r] = np.dot(x[:, :n_points - r], y[:, r:].T)
        else:
            for r in range(abs(p) + 1):
                idxs = np.arange(-r, x.shape[1] - r)
                zy = y.take(idxs, axis=1, mode='wrap')
                cov[:, :, r] = np.dot(x[:, :n_points-r], zy[:, :n_points-r].T)

        if norm:
            kv = cov / (n_points - 1)
        else:
            kv = cov

        if p == 0:
            kv = np.squeeze(kv)

        return kv

    def fit(self, data):
        """
            Compute MVAR coefficients with Viera-Morf algorithm
        """

        pmax = self.p

        data = atleast_3d(data)

        assert pmax > 0, "pmax > 0"

        n_trials, n_chans, n_points = data.shape
        cov_func = self.mean_cov

        f, b = data.copy(), data.copy()

        pef = cov_func(data, norm=False)
        peb =pef.copy()

        arf = np.zeros((n_chans, n_chans, pmax))
        arb = np.zeros((n_chans, n_chans, pmax))

        for k in range(0, pmax):
            D = cov_func(f[:, :, k + 1:n_points], 
                         b[:, :, 0:n_points - k - 1], 
                         norm=False)
            arf[:, :, k] = np.dot(D, np.linalg.inv(peb))
            arb[:, :, k] = np.dot(D.T, np.linalg.inv(pef))

            tmp = f[:, :, k+1:] - np.dot(b[:, :, :n_points - k - 1].transpose(0, 2, 1),
                                         arf[:, :, k].T).transpose(0, 2, 1)
            
            b[:, :, :n_points-k-1] = b[::, :, :n_points-k-1] - np.dot(f[:, :, k+1:].transpose(0, 2, 1), 
                                                                      arb[:, :, k].T).transpose(0, 2, 1)
            f[:, :, k+1:] = tmp

            for i in range(k):
                tmpp = arf[:, :, i] - np.dot(arf[:, :, k], arb[:, :, k-i-1])
                arb[:, :, k-i-1] = arb[:, :, k-i-1] - np.dot(arb[:, :, k], arf[:, :, i])
                arf[:, :, i] = tmpp
            peb = cov_func(b[:, :, :n_points-k-1], norm=False)
            pef = cov_func(f[:, :, k+1:], norm=False)

        self.coef = arf.reshape(n_chans, n_chans * pmax)
        self.residuals = data - self.predict(data)
        self.rescov = sp.cov(cat_trials(self.residuals[:, :, self.p:]))

        return self

def _msge_with_gradient_underdetermined(data, delta, xvschema, skipstep, p):
    """Calculate mean squared generalization error and its gradient for
    underdetermined equation system.
    """
    t, m, l = data.shape
    d = None
    j, k = 0, 0
    nt = np.ceil(t / skipstep)
    for trainset, testset in xvschema(t, skipstep):

        a, b = _construct_var_eqns(atleast_3d(data[trainset, :, :]), p)
        c, d = _construct_var_eqns(atleast_3d(data[testset, :, :]), p)

        e = sp.linalg.inv(np.eye(a.shape[0]) * delta ** 2 + a.dot(a.T))

        cc = c.transpose().dot(c)

        be = b.transpose().dot(e)
        bee = be.dot(e)
        bea = be.dot(a)
        beea = bee.dot(a)
        beacc = bea.dot(cc)
        dc = d.transpose().dot(c)

        j += np.sum(beacc * bea - 2 * bea * dc) + np.sum(d ** 2)
        k += np.sum(beea * dc - beacc * beea) * 4 * delta

    return j / (nt * d.size), k / (nt * d.size)


def _msge_with_gradient_overdetermined(data, delta, xvschema, skipstep, p):
    """Calculate mean squared generalization error and its gradient for
    overdetermined equation system.
    """
    t, m, l = data.shape
    d = None
    l, k = 0, 0
    nt = np.ceil(t / skipstep)
    for trainset, testset in xvschema(t, skipstep):

        a, b = _construct_var_eqns(atleast_3d(data[trainset, :, :]), p)
        c, d = _construct_var_eqns(atleast_3d(data[testset, :, :]), p)

        e = sp.linalg.inv(np.eye(a.shape[1]) * delta ** 2 + a.T.dot(a))

        ba = b.transpose().dot(a)
        dc = d.transpose().dot(c)
        bae = ba.dot(e)
        baee = bae.dot(e)
        baecc = bae.dot(c.transpose().dot(c))

        l += np.sum(baecc * bae - 2 * bae * dc) + np.sum(d ** 2)
        k += np.sum(baee * dc - baecc * baee) * 4 * delta

    return l / (nt * d.size), k / (nt * d.size)


def _get_msge_with_gradient_func(shape, p):
    """Select which function to use for MSGE calculation (over- or
    underdetermined).
    """
    t, m, l = shape

    n = (l - p) * t
    underdetermined = n < m * p

    if underdetermined:
        return _msge_with_gradient_underdetermined
    else:
        return _msge_with_gradient_overdetermined


def _get_msge_with_gradient(data, delta, xvschema, skipstep, p):
    """Calculate mean squared generalization error and its gradient,
    automatically selecting the best function.
    """
    t, m, l = data.shape

    n = (l - p) * t
    underdetermined = n < m * p

    if underdetermined:
        return _msge_with_gradient_underdetermined(data, delta, xvschema,
                                                   skipstep, p)
    else:
        return _msge_with_gradient_overdetermined(data, delta, xvschema,
                                                  skipstep, p)
