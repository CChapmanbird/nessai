# -*- coding: utf-8 -*-
"""
Proposals specifically for use with the importance based nested sampler.
"""
import logging
import os
from typing import Callable, Optional, Tuple, Union

import numpy as np
from scipy.special import logsumexp
from scipy.stats import entropy

from nessai.plot import plot_1d_comparison, plot_histogram, plot_live_points

from .base import Proposal
from .. import config
from ..flowmodel import CombinedFlowModel, update_config
from ..livepoint import (
    get_dtype,
    live_points_to_array,
    numpy_array_to_live_points
)
from ..model import Model
from ..utils.rescaling import (
    gaussian_cdf_with_log_j,
    inv_gaussian_cdf_with_log_j,
    logit,
    sigmoid,
)
from ..utils.structures import get_subset_arrays, isfinite_struct


logger = logging.getLogger(__name__)


class ImportanceFlowProposal(Proposal):
    """Flow-based proposal for importance-based nested sampling.

    Parameters
    ----------
    model : :obj:`nessai.model.Model`
        User-defined model.
    clip : bool
        If true the samples generated by flow will be clipped to [0, 1] before
        being mapped back from the unit-hypercube. This is only needed when
        the mapping cannot be defined outside of [0, 1]. In cases where it
        can, these points will be rejected when the prior bounds are checked.
    reweight_draws : bool
        If true then the weights used to compute the meta proposal are based
        on the number of samples accepted rather than the total number of
        samples drawn. This feature is experimental and may change or be
        removed in the future.
    """
    def __init__(
        self,
        model: Model,
        output: str,
        initial_draws: int,
        reparam: str = 'logit',
        plot_training: bool = False,
        weighted_kl: bool = True,
        weights_include_likelihood: bool = False,
        reset_flows: bool = False,
        flow_config: dict = None,
        combined_proposal: bool = True,
        clip: bool = False,
        beta: Optional[float] = None,
        reweight_draws: bool = False,
    ) -> None:
        self.level_count = -1
        self.draw_count = 0
        self._initialised = False
        self.beta = 1.0 if beta is None else beta

        self.model = model
        self.output = output
        self.flow_config = flow_config
        self.plot_training = plot_training
        self.reset_flows = reset_flows
        self.reparam = reparam
        self.weighted_kl = weighted_kl
        self.clip = clip

        self.reweight_draws = reweight_draws
        if self.reweight_draws:
            logger.warning(
                'Reweight draws is experimental produce biased results!'
            )
        self.initial_draws = initial_draws
        self.initial_log_q = np.log(self.initial_draws)
        self.n_draws = {'initial': initial_draws}
        self.n_requested = {'initial': initial_draws}
        self.levels = {'initial': None}
        self.level_entropy = []

        logger.debug(f'Initial q: {np.exp(self.initial_log_q)}')

        self.combined_proposal = combined_proposal
        self.weights_include_likelihood = weights_include_likelihood

        self.dtype = get_dtype(self.model.names)
        self.update_annealing(beta)

    @property
    def total_samples_requested(self) -> float:
        """Return the total number of samples requested"""
        return np.sum(np.fromiter(self.n_requested.values(), int))

    @property
    def total_samples_drawn(self) -> float:
        """Return the total number of samples requested"""
        return np.sum(np.fromiter(self.n_draws.values(), int))

    @property
    def normalisation_constant(self) -> float:
        """Normalisation constant for the meta proposal.

        Value depends on :code:`reweight_draws`.
        """
        if self.reweight_draws:
            return self.total_samples_requested
        else:
            return self.total_samples_drawn

    @property
    def unnormalised_weights(self) -> dict:
        """Unnormalised weights.

        Value depends on :code:`reweight_draws`
        """
        if self.reweight_draws:
            return self.n_requested
        else:
            return self.n_draws

    @property
    def poolsize(self) -> np.ndarray:
        """Returns an array of the pool size for each flow.

        Does not include the value for the initial draws from the prior.
        """
        return np.fromiter(self.unnormalised_weights.values(), int)[1:]

    @property
    def flow_config(self) -> dict:
        """Return the configuration for the flow"""
        return self._flow_config

    @property
    def n_proposals(self) -> int:
        """Current number of proposals in the meta proposal"""
        return len(self.n_draws)

    @flow_config.setter
    def flow_config(self, config: dict) -> None:
        """Set configuration (includes checking defaults)"""
        if config is None:
            config = dict(model_config=dict())
        config['model_config']['n_inputs'] = self.model.dims
        self._flow_config = update_config(config)

    @staticmethod
    def _check_fields():
        """Check that the logQ and logW fields have been added."""
        if 'logQ' not in config.NON_SAMPLING_PARAMETERS:
            raise RuntimeError(
                'logQ field missing in non-sampling parameters.'
            )
        if 'logW' not in config.NON_SAMPLING_PARAMETERS:
            raise RuntimeError(
                'logW field missing in the non-sampling parameters.'
            )

    def initialise(self):
        """Initialise the proposal"""
        self._check_fields()
        if self.initialised:
            return
        self.flow = CombinedFlowModel(
            config=self.flow_config, output=self.output
        )
        self.flow.initialise()
        return super().initialise()

    def to_prime(self, x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Convert samples from the unit hypercube to samples in x'-space"""
        x = np.atleast_2d(x)
        if self.reparam == 'logit':
            x_prime, log_j = logit(x.copy())
            log_j = log_j.sum(axis=1)
        elif self.reparam == 'gaussian_cdf':
            logger.debug('Rescaling with inverse Gaussian CDF')
            x_prime, log_j = inv_gaussian_cdf_with_log_j(x.copy())
            log_j = log_j.sum(axis=1)
        elif self.reparam is None:
            x_prime = x.copy()
            log_j = np.zeros(x.shape[0])
        else:
            raise ValueError(self.reparam)
        return x_prime, log_j

    def from_prime(self, x_prime: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Convert samples the x'-space to samples in the unit hypercube."""
        x_prime = np.atleast_2d(x_prime)
        if self.reparam == 'logit':
            x, log_j = sigmoid(x_prime.copy())
            log_j = log_j.sum(axis=1)
        elif self.reparam == 'gaussian_cdf':
            logger.debug('Rescaling with Gaussian CDF')
            x, log_j = gaussian_cdf_with_log_j(x_prime.copy())
            log_j = log_j.sum(axis=1)
        elif self.reparam is None:
            x = x_prime.copy()
            log_j = np.zeros(x.shape[0])
        else:
            raise ValueError(self.reparam)
        return x, log_j

    def rescale(self, x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """Convert from live points."""
        x_hypercube = self.model.to_unit_hypercube(x)
        x_array = live_points_to_array(x_hypercube, self.model.names)
        x_prime, log_j = self.to_prime(x_array)
        return x_prime, log_j

    def inverse_rescale(self, x_prime: np.ndarray) -> np.ndarray:
        x_array, log_j = self.from_prime(x_prime)
        if self.clip:
            x_array = np.clip(x_array, 0.0, 1.0)
        x_hypercube = numpy_array_to_live_points(x_array, self.model.names)
        x = self.model.from_unit_hypercube(x_hypercube)
        return x, log_j

    def update_annealing(self, beta):
        """Update the annealing value."""
        if not beta:
            logger.debug('Nothing to update')
            return
        if not (0. < beta < 1.0):
            raise ValueError('Annealing must be between 0 and 1')
        self.beta = beta

    def train(
        self,
        samples: np.ndarray,
        plot: bool = False,
        output: Union[str, None] = None,
        beta: float = None,
        weights: np.ndarray = None,
        **kwargs
    ) -> None:
        """Train the proposal with a set of samples.

        Parameters
        ----------
        samples :  numpy.ndarray
            Array of samples for training.
        plot : bool
            Flag to enable or disable plotting.
        output : Union[str, None]
            Output directory to use instead of default output. If None the
            default that was set when the class what initialised is used.
        kwargs :
            Key-word arguments passed to \
                :py:meth:`nessai.flowmodel.FlowModel.train`.
        """
        self.level_count += 1
        self.n_draws[self.level_count] = 0
        self.n_requested[self.level_count] = 0
        output = self.output if output is None else output
        level_output = os.path.join(
            output, f'level_{self.level_count}', ''
        )

        if not os.path.exists(level_output):
            os.makedirs(level_output, exist_ok=True)

        training_data = samples.copy()
        x_prime, _ = self.rescale(training_data)

        if plot:
            plot_live_points(
                training_data,
                filename=os.path.join(level_output, 'training_data.png')
            )
            plot_1d_comparison(
                x_prime,
                convert_to_live_points=True,
                filename=os.path.join(level_output, 'prime_training_data.png'),
            )

        logger.debug(
            f'Training data min and max: {x_prime.min()}, {x_prime.max()}'
        )

        if beta:
            self.update_annealing(beta)

        if self.weighted_kl or weights:
            logger.debug('Using weights in training')
            if weights is not None:
                weights = weights / np.sum(weights)
            else:
                if self.weights_include_likelihood:
                    log_weights = (
                        training_data['logW']
                        + self.beta * training_data['logL']
                    )
                else:
                    log_weights = training_data['logW'].copy()
                log_weights -= logsumexp(log_weights)
                weights = np.exp(log_weights)
            if plot:
                plot_histogram(
                    weights, filename=level_output + 'training_weights.png'
                )
        else:
            weights = None

        self.flow.add_new_flow(reset=self.reset_flows)
        assert len(self.flow.models) == (self.level_count + 1)
        self.flow.train(
            x_prime,
            weights=weights,
            output=level_output,
            plot=plot or self.plot_training,
            **kwargs,
        )

        if plot:
            test_samples_prime, log_prob = self.flow.sample_and_log_prob(2000)
            test_samples, log_j_inv = self.inverse_rescale(test_samples_prime)
            log_prob -= log_j_inv
            test_samples['logQ'] = log_prob
            plot_live_points(
                test_samples,
                filename=os.path.join(level_output, 'generated_samples.png')
            )

    def _compute_log_Q_combined(self, x_prime, log_q_j, n, log_j):
        if np.isnan(x_prime).any():
            logger.warning('NaNs in samples when computing log_Q')
        if not np.isfinite(x_prime).all():
            logger.warning(
                'Infinite values in the samples when computing log_Q'
            )

        exclude_last = log_q_j is not None
        if exclude_last and len(self.flow.models) == 1:
            log_Q = log_q_j + np.log(n) + log_j
        else:
            log_q_all = self.flow.log_prob_all(
                x_prime, exclude_last=exclude_last
            )
            m = log_q_all.shape[1]
            assert log_q_all.shape[0] == x_prime.shape[0]
            if exclude_last:
                log_q = np.concatenate([
                    log_q_all + np.log(self.poolsize[:m]),
                    log_q_j[:, np.newaxis] + np.log(n)
                ], axis=1)
                assert log_q_all.shape[1] == (len(self.flow.models) - 1)
            else:
                log_q = log_q_all + np.log(self.poolsize)
                assert log_q_all.shape[1] == len(self.flow.models)
            log_q += log_j[:, np.newaxis]

            logger.debug(f'log_q is nan: {np.isnan(log_q).any()}')
            logger.debug(f'Initial log g: {self.initial_log_q:.2f}')
            logger.debug(
                f'Mean log q for each each flow: {log_q.mean(axis=0)}'
            )
            # Could move Jacobian here
            log_Q = logsumexp(log_q, axis=1)

        if np.isnan(log_Q).any():
            raise ValueError('There is a NaN in log q before initial!')
        log_Q = np.logaddexp(self.initial_log_q, log_Q)
        if np.isnan(log_Q).any():
            raise ValueError('There is a NaN in log g!')
        return log_Q

    def _compute_log_Q_independent(self, x, log_q, n, log_j):
        log_Q = log_q + log_j + np.log(n)
        return log_Q

    def compute_log_Q(
        self,
        x: np.ndarray,
        log_q: np.ndarray = None,
        n: int = None,
        log_j=None,
    ) -> np.ndarray:
        """Compute the log meta proposal (log Q) for an array of points.

        Parameters
        ----------
        x : np.ndarray
            Array of samples in the unit hypercube.
        """
        if self.combined_proposal:
            return self._compute_log_Q_combined(x, log_q, n, log_j)
        else:
            return self._compute_log_Q_independent(x, log_q, n, log_j)

    def draw(
        self,
        n: int,
        logL_min=None,
        flow_number=None
    ) -> np.ndarray:
        """Draw n new points.

        Parameters
        ----------
        n : int
            Number of points to draw.

        Returns
        -------
        np.ndarray :
            Array of new points.
        """
        if flow_number is None:
            flow_number = self.level_count
        if logL_min:
            _p = 1.2
            n = int(n)
            n_draw = int(_p * n)
        else:
            n_draw = int(1.01 * n)
        logger.debug(f'Drawing {n} points')
        samples = np.zeros(0, dtype=self.dtype)
        self.n_requested[self.level_count] += n
        n_accepted = 0
        while n_accepted < n and n_draw > 0:
            logger.debug(f'Drawing batch of {n_draw} samples')
            x_prime, log_q = self.flow.sample_and_log_prob(N=n_draw)
            x, log_j_inv = self.inverse_rescale(x_prime)
            # Rescaling can sometimes produce infs that don't appear in samples
            x_check, log_j = self.rescale(x)
            # Probably don't need all these checks.
            acc = (
                self.model.in_bounds(x)
                & isfinite_struct(x)
                & np.isfinite(x_check).all(axis=1)
                & np.isfinite(x_prime).all(axis=1)
                & np.isfinite(log_j)
                & np.isfinite(log_j_inv)
                & np.isfinite(log_q)
            )
            logger.debug(f'Rejected {n_draw - acc.size} points')
            if not np.any(acc):
                continue
            x, x_prime, log_j, log_q = \
                get_subset_arrays(acc, x, x_prime, log_j, log_q)

            x['logQ'] = self.compute_log_Q(
                x_prime, log_q=log_q, n=n, log_j=log_j
            )
            x['logP'] = self.model.log_prior(x)
            x['logW'] = - x['logQ']
            accept = (
                np.isfinite(x['logP'])
                & np.isfinite(x['logW'])
            )
            if not np.any(accept):
                continue

            x = x[accept]

            if logL_min is not None:
                x['logl'] = self.model.batch_evaluate_log_likelihood(x)

            samples = np.concatenate([samples, x])

            if logL_min is not None:
                m = (x['logL'] >= logL_min).sum()
                n_accepted += m
                logger.debug(f'Total accepted: {samples.size}')
                logger.debug(f'Accepted above min logL: {n_accepted}')
            else:
                n_accepted += x.size
                logger.debug(f'Accepted: {n_accepted}')

        if logL_min is None:
            samples = samples[:n]
        else:
            possible_idx = np.cumsum(samples['logL'] >= logL_min)
            idx = np.argmax(possible_idx >= n)
            samples = samples[:(idx + 1)]
            assert len(samples) >= n
            logger.debug(
                f"Accepted {(samples['logL'] >= logL_min).sum()} "
                f'with logL greater than {logL_min}'
            )

        self.n_draws[self.level_count] += samples.size

        if logL_min is not None:
            logger.debug('Recomputing log g')
            prime_samples, log_j = self.rescale(samples)
            samples['logQ'] = self.compute_log_Q(
                prime_samples, log_j=log_j,
            )
            samples['logW'] = -samples['logQ']

        entr = entropy(np.exp(samples['logQ']))
        logger.info(f'Proposal self entropy: {entr:.3}')
        self.level_entropy.append(entr)

        self.draw_count += 1
        logger.debug(f'Returning {samples.size} samples')
        return samples

    def update_samples(self, samples: np.ndarray) -> None:
        """Update log W and log Q in place for a set of samples.

        Parameters
        ----------
        samples : numpy.ndarray
            Array of samples to update.
        """
        if self.level_count < 0:
            raise RuntimeError(
                'Cannot update samples unless a level has been constructed!'
            )
        if self.level_count not in self.n_draws:
            raise RuntimeError(
                'Must draw samples from the new level before updating any '
                'existing samples!'
            )
        x, log_j = self.rescale(samples.copy())
        log_prob_fn = self.get_proposal_log_prob(self.level_count)
        log_q = log_prob_fn(x)
        new_log_Q = (
            log_q
            + log_j
            + np.log(self.unnormalised_weights[self.level_count])
            )
        samples['logQ'] = np.logaddexp(samples['logQ'], new_log_Q)
        samples['logW'] = - samples['logQ']

    def _log_prior(self, x: np.ndarray) -> np.ndarray:
        """Helper function that returns the prior in the unit hyper-cube."""
        return np.zeros(x.shape[0])

    def get_proposal_log_prob(self, it: int) -> Callable:
        """Get a pointer to the function for ith proposal."""
        if it == -1:
            return self._log_prior
        elif it < len(self.flow.models):
            return lambda x: self.flow.log_prob_ith(x, it)
        else:
            raise ValueError

    def compute_kl_between_proposals(
        self,
        x: np.ndarray,
        p_it: Optional[int] = None,
        q_it: Optional[int] = None,
    ) -> float:
        """Compute the KL divergence between two proposals.

        Samples should be drawn from p. If proposals aren't specified the
        current and previous proposals are used.
        """
        x_prime, log_j = self.rescale(x)
        if p_it is None:
            p_it = self.flow.n_models - 1

        if q_it is None:
            q_it = self.flow.n_models - 2

        if p_it == q_it:
            raise ValueError('p and q must be different')
        elif p_it < -1 or q_it < -1:
            raise ValueError(f'Invalid p_it or q_it: {p_it}, {q_it}')

        p_f = self.get_proposal_log_prob(p_it)
        q_f = self.get_proposal_log_prob(q_it)

        log_p = p_f(x_prime)
        log_q = q_f(x_prime)

        if p_it > -1:
            log_p += log_j
        if q_it > -1:
            log_q += log_j

        log_p -= logsumexp(log_p)
        log_q -= logsumexp(log_q)

        kl = np.mean(log_p - log_q)
        logger.info(f'KL between {p_it} and {q_it} is: {kl:.3}')
        return kl

    def draw_from_flows(
        self, n: int, weights=None
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Draw n points from all flows (g).

        Parameters
        ----------
        n : int
            Number of points
        """
        logger.debug(
            f'Drawing {n} samples from the combination of all the proposals'
        )
        if weights is None:
            weights = np.fromiter(self.unnormalised_weights.values(), float)
        weights /= np.sum(weights)
        if not len(weights) == self.n_proposals:
            ValueError('Size of weights does not match the number of levels')
        logger.debug(f'Proposal weights: {weights}')
        a = np.random.choice(weights.size, size=n, p=weights)
        counts = np.bincount(a).astype(int)
        logger.debug(f'Expected counts: {counts}')
        proposal_id = np.arange(weights.size) - 1
        prime_samples = np.empty([n, self.model.dims])
        sample_its = np.empty(n, dtype=config.IT_DTYPE)
        count = 0
        # Draw from prior
        for id, m in zip(proposal_id, counts):
            if m == 0:
                continue
            logger.debug(f'Drawing {m} samples from the {id}th proposal.')
            if id == -1:
                prime_samples[count:(count + m)] = \
                    self.to_prime(np.random.rand(m, self.model.dims))[0]
            else:
                prime_samples[count:(count + m)] = \
                    self.flow.sample_ith(id, N=m)
            sample_its[count:(count + m)] = id
            count += m

        samples, log_j = self.inverse_rescale(prime_samples)
        samples['it'] = sample_its
        finite = (
            np.isfinite(log_j)
            & isfinite_struct(samples)
            & np.isfinite(prime_samples).all(axis=1)
        )
        samples, prime_samples, log_j = \
            get_subset_arrays(finite, samples, prime_samples, log_j)

        log_q = np.zeros((samples.size, self.n_proposals))
        # Minus because log_j is compute from the inverse
        logger.debug('Computing log_q')
        log_q[:, 1:] = \
            self.flow.log_prob_all(prime_samples) - log_j[:, np.newaxis]

        # -inf is okay since this is just zero, so only remove +inf or NaN
        finite = (
            ~np.isnan(log_q).all(axis=1)
            & ~np.isposinf(log_q).all(axis=1)
        )
        samples, log_q = get_subset_arrays(finite, samples, log_q)

        logger.debug(
            f'Mean g for each each flow: {np.exp(log_q).mean(axis=0)}'
        )

        samples['logP'] = self.model.log_prior(samples)
        samples, log_q = get_subset_arrays(
            np.isfinite(samples['logP']), samples, log_q
        )
        counts = np.bincount(samples['it'] + 1).astype(int)
        logger.debug(f'Actual counts: {counts}')

        return samples, log_q, counts

    def resume(self, model, flow_config, weights_path=None):
        """Resume the proposal"""
        super().resume(model)
        self.flow_config = flow_config
        self.initialise()
        self.flow.setup_from_input_dict(self.flow_config)
        if weights_path:
            self.flow.update_weights_path(weights_path)
        self.flow.load_all_weights()

    def __getstate__(self):
        obj = super().__getstate__()
        del obj['_flow_config']
        return obj
